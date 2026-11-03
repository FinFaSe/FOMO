"""WebSocket stream of the watchlist: provisional alerts a second or two after the block.

One connection to the RPC's WebSocket endpoint (SOLANA_WS_URL; by default SOLANA_RPC_URL with `wss://`) and one
`logsSubscribe` (mentions = wallet, commitment `confirmed`) per watchlist wallet that has had its first sync. A
notification carries only the signature: the transaction is fetched at `confirmed` (retried for a few seconds, since
the node that serves getTransaction often lags the one that notified), normalized and alerted provisionally
(tracker.alerts.provisional_alerts). FINALIZE_AFTER seconds later it is stored as finalized evidence and the same
alert is completed. The polling watcher keeps running as the fallback for anything the stream misses
(disconnections, provider limits), so nothing depends on the stream being up.

TRACKER_STREAM=0 disables it. The endpoint URL may carry a provider key: only its host is ever logged or shown.
"""

import asyncio
import itertools
import json
import logging
import os
import socket
import threading
import time
import uuid
from urllib.parse import urlsplit

from sqlalchemy import select

from .adapters.solana import ProviderError
from .alerts import Notifier, deliver_pending, generate_alerts, provisional_alerts
from .db import PendingSignature, SyncState, Wallet, WorkerHeartbeat, now_iso
from .watch import _price, process_signature

log = logging.getLogger("tracker.stream")
FINALIZE_AFTER = 15.0  # seconds; finalization takes ~13 s after the block
CONFIRMED_RETRIES = (0.3, 0.5, 0.8, 1.2, 2.0, 2.0, 3.0)  # waits before each new attempt, ~10 s in total
RESUBSCRIBE_SECONDS = 15.0  # watchlist changes are picked up this often
MAX_IN_FLIGHT = 8


def ws_url(rpc_url: str | None = None) -> str:
    explicit = os.getenv("SOLANA_WS_URL")
    if explicit:
        return explicit
    endpoint = rpc_url or os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    return "wss://" + endpoint.removeprefix("https://")


def enabled() -> bool:
    return os.getenv("TRACKER_STREAM", "1") != "0"


class Stream:
    def __init__(self, sessions, rpc, prices, *, url: str | None = None, notifier: Notifier | None = None,
                 stream_id: str | None = None, finalize_after: float = FINALIZE_AFTER,
                 retries: tuple[float, ...] = CONFIRMED_RETRIES, resubscribe: float = RESUBSCRIBE_SECONDS):
        self.sessions, self.rpc, self.prices = sessions, rpc, prices
        self.url = url or ws_url(getattr(rpc, "endpoint", None))
        self.host = urlsplit(self.url).hostname
        self.notifier = notifier or Notifier()
        self.id = stream_id or f"stream:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.finalize_after, self.retries, self.resubscribe = finalize_after, retries, resubscribe
        self.stats = {"connected": False, "subscriptions": 0, "notifications": 0, "provisional_alerts": 0,
                      "finalized": 0, "reconnects": 0, "last_notification_at": None, "last_error": None}
        self.tasks: set[asyncio.Task] = set()  # alerts in flight; they survive reconnections

    # Blocking work, run in threads -------------------------------------------------------------------------

    def watchlist(self) -> set[str]:
        """Watchlist wallets with a first sync (their history baseline exists, so old trades never alert)."""
        with self.sessions() as session:
            return set(session.scalars(select(Wallet.address).join(SyncState, SyncState.wallet == Wallet.address)
                                       .where(Wallet.favorite.is_(True), SyncState.head_signature.is_not(None))))

    def alert(self, signature: str, wallet: str, detected_at: str) -> bool | None:
        """One attempt: True when handled, None while the RPC does not serve it at `confirmed` yet."""
        with self.sessions() as session:
            try:
                created = provisional_alerts(session, self.rpc, signature, [wallet], detected_at=detected_at,
                                             prices=self.prices, detected_by="stream")
            except ProviderError as exc:
                session.rollback()
                if exc.status == 409:
                    return None
                self.stats["last_error"] = exc.message
                return False
            if created:
                self.stats["provisional_alerts"] += len(created)
                deliver_pending(session, self.notifier)
            return True

    def finalize(self, signature: str, wallet: str):
        """Store the finalized transaction (completing its alert); if it is not finalized yet, the watcher's
        pending queue retries it."""
        with self.sessions() as session:
            try:
                touched = process_signature(session, self.rpc, signature, [wallet])
            except ProviderError as exc:
                session.rollback()
                row = session.get(PendingSignature, signature) or PendingSignature(signature=signature, source="stream")
                row.last_error = exc.message
                session.add(row)
                session.commit()
                return
            _price(session, self.prices, set(touched))
            report = generate_alerts(session)
            self.stats["finalized"] += 1
            if report["created"] or report.get("finalized"):
                deliver_pending(session, self.notifier)

    def beat(self):
        with self.sessions() as session:
            info = {"kind": "stream", "host": self.host, **self.stats}
            row = session.get(WorkerHeartbeat, self.id)
            if row is None:
                session.add(WorkerHeartbeat(id=self.id, info=info))
            else:
                row.heartbeat_at, row.info = now_iso(), info
            session.commit()

    # Async connection ----------------------------------------------------------------------------------------

    async def _handle(self, signature: str, wallet: str, detected_at: str, slots: asyncio.Semaphore):
        async with slots:
            for wait in (0.0, *self.retries):
                if wait:
                    await asyncio.sleep(wait)
                done = await asyncio.to_thread(self.alert, signature, wallet, detected_at)
                if done is not None:
                    break
        await asyncio.sleep(self.finalize_after)
        await asyncio.to_thread(self.finalize, signature, wallet)

    async def _session(self, ws, stop: threading.Event):
        ids = itertools.count(1)
        requests: dict[int, tuple[str, str]] = {}  # request id -> (action, wallet)
        by_sub: dict[int, str] = {}
        by_wallet: dict[str, int | None] = {}  # None while the subscription is pending

        async def maintain():
            while not stop.is_set():
                want = await asyncio.to_thread(self.watchlist)
                for wallet in sorted(want - set(by_wallet)):
                    request = next(ids)
                    requests[request], by_wallet[wallet] = ("subscribe", wallet), None
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": request, "method": "logsSubscribe",
                                              "params": [{"mentions": [wallet]}, {"commitment": "confirmed"}]}))
                for wallet in sorted(set(by_wallet) - want):
                    sub = by_wallet.pop(wallet)
                    if sub is not None:
                        by_sub.pop(sub, None)
                        request = next(ids)
                        requests[request] = ("unsubscribe", wallet)
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": request, "method": "logsUnsubscribe",
                                                  "params": [sub]}))
                self.stats["subscriptions"] = len(by_sub)
                await asyncio.to_thread(self.beat)
                for _ in range(int(self.resubscribe * 10)):
                    if stop.is_set():
                        break
                    await asyncio.sleep(0.1)
            await ws.close()

        async def read():
            async for raw in ws:
                msg = json.loads(raw)
                if "id" in msg and msg["id"] in requests:
                    action, wallet = requests.pop(msg["id"])
                    if action == "subscribe" and isinstance(msg.get("result"), int):
                        by_sub[msg["result"]], by_wallet[wallet] = wallet, msg["result"]
                        self.stats["subscriptions"] = len(by_sub)
                    elif action == "subscribe":
                        by_wallet.pop(wallet, None)  # retried on the next maintenance round
                        self.stats["last_error"] = str((msg.get("error") or {}).get("message"))[:200]
                elif msg.get("method") == "logsNotification":
                    params = msg.get("params") or {}
                    wallet = by_sub.get(params.get("subscription"))
                    value = (params.get("result") or {}).get("value") or {}
                    self.stats["notifications"] += 1
                    self.stats["last_notification_at"] = now_iso()
                    if wallet and value.get("signature") and value.get("err") is None:
                        task = asyncio.create_task(self._handle(value["signature"], wallet, now_iso(), self.slots))
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)

        maintainer = asyncio.create_task(maintain())
        try:
            await read()
        finally:
            maintainer.cancel()

    async def run(self, stop: threading.Event):
        import websockets

        self.slots = asyncio.Semaphore(MAX_IN_FLIGHT)
        backoff = 1.0
        while not stop.is_set():
            try:
                async with websockets.connect(self.url, open_timeout=20, ping_interval=20, ping_timeout=20) as ws:
                    self.stats["connected"], backoff = True, 1.0
                    log.info("stream: connected to %s", self.host)
                    await self._session(ws, stop)
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
                # Public endpoints (or egress proxies) close long-lived connections; a reconnection takes ~1 s and
                # the watcher covers anything notified meanwhile.
                self.stats["last_error"] = f"{type(exc).__name__}"
                log.warning("stream: %s on %s; reconnecting in %ss", type(exc).__name__, self.host, backoff)
            self.stats["connected"] = False
            if stop.is_set():
                break
            self.stats["reconnects"] += 1
            await asyncio.to_thread(self.beat)
            deadline = time.monotonic() + backoff
            while time.monotonic() < deadline and not stop.is_set():
                await asyncio.sleep(0.1)
            backoff = min(backoff * 2, 60.0)
        for task in list(self.tasks):  # stopping: what is pending is finalized by the watcher's queue later
            task.cancel()

    def run_forever(self, stop: threading.Event):
        log.info("stream %s started (%s)", self.id, self.host)
        while not stop.is_set():
            try:
                asyncio.run(self.run(stop))
            except Exception:  # never let a bug kill the thread: the watcher still covers the watchlist
                log.exception("stream loop error")
                stop.wait(5)
