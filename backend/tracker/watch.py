"""Near-real-time watchlist ingestion: polling watcher and pushed signatures (provider webhooks).

The watcher asks, every few seconds, only for signatures newer than the last one it handled for each watchlist
wallet (`until=`), stores those transactions and their trades without touching the sync cursors (the incremental
sync later finds them already stored and does not download them again), values them and generates alerts.
Listings and transactions use `finalized`, so detection latency is the finalization lag plus the polling interval.
Providers that push transactions (Helius, QuickNode Streams, …) can call the signed webhook instead.
"""

import logging
import os
import socket
import threading
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .adapters.solana import ProviderError
from .alerts import Notifier, deliver_pending, generate_alerts
from .backtest import capture_quotes
from .db import Observation, PendingSignature, RawTransaction, SyncState, Wallet, WorkerHeartbeat, now_iso
from .pricing import price_trades
from .service import record_observation, store_transaction

log = logging.getLogger("tracker.watch")
PAGE = 50
MAX_PENDING_ATTEMPTS = 20


def _store(session, rpc, signature: str) -> RawTransaction:
    try:
        raw = store_transaction(session, rpc, signature)
        session.commit()
        return raw
    except IntegrityError:  # stored concurrently by a sync job
        session.rollback()
        return session.get(RawTransaction, signature)


def _involved(raw: RawTransaction) -> set[str]:
    keys = raw.payload["transaction"]["message"]["accountKeys"]
    meta = raw.payload.get("meta") or {}
    found = {k["pubkey"] if isinstance(k, dict) else k for k in keys}
    for field in ("preTokenBalances", "postTokenBalances"):
        found |= {b["owner"] for b in meta.get(field) or [] if b.get("owner")}
    return found


def process_signature(session, rpc, signature: str, wallets: list[str] | None = None) -> list[str]:
    """Store one transaction for the tracked wallets it involves. Raises ProviderError if not finalized yet."""
    raw = _store(session, rpc, signature)
    involved = _involved(raw)
    candidates = [w for w in wallets if w in involved] if wallets is not None else list(
        session.scalars(select(Wallet.address).where(Wallet.address.in_(sorted(involved)))))
    touched = []
    for wallet in candidates:
        if session.scalar(select(Observation.id).where(Observation.wallet == wallet,
                                                       Observation.signature == signature)):
            continue
        try:
            record_observation(session, wallet, raw)
            session.commit()
            touched.append(wallet)
        except IntegrityError:
            session.rollback()
    return touched


def push_signatures(session, rpc, prices, signatures: list[str], source: str) -> dict:
    """Webhook path: process now, keep what is not finalized yet for the watcher to retry."""
    report = {"processed": 0, "wallets": [], "pending": [], "alerts": 0}
    touched = set()
    for signature in signatures:
        try:
            touched |= set(process_signature(session, rpc, signature))
            report["processed"] += 1
        except ProviderError as exc:
            session.rollback()
            row = session.get(PendingSignature, signature) or PendingSignature(signature=signature, source=source)
            row.last_error = exc.message
            session.add(row)
            session.commit()
            report["pending"].append(signature)
    _price(session, prices, touched)
    report["wallets"] = sorted(touched)
    report["alerts"] = generate_alerts(session)["created"]
    return report


def _price(session, prices, wallets):
    for wallet in wallets:
        try:
            price_trades(session, wallet, prices, max_requests=2)
        except ProviderError:
            session.rollback()  # alerts still go out; USD values arrive with the next valuation


def retry_pending(session, rpc, prices) -> int:
    done, touched = 0, set()
    for row in list(session.scalars(select(PendingSignature).order_by(PendingSignature.created_at).limit(20))):
        try:
            touched |= set(process_signature(session, rpc, row.signature))
            session.delete(row)
            done += 1
        except ProviderError as exc:
            session.rollback()
            row.attempts += 1
            row.last_error = exc.message
            if row.attempts >= MAX_PENDING_ATTEMPTS:
                session.delete(row)
        session.commit()
    _price(session, prices, touched)
    return done


class Watcher:
    def __init__(self, sessions, rpc, prices, *, quotes=None, notifier: Notifier | None = None,
                 interval: float | None = None, watcher_id: str | None = None):
        self.sessions, self.rpc, self.prices, self.quotes = sessions, rpc, prices, quotes
        self.notifier = notifier or Notifier()
        self.interval = interval if interval is not None else float(os.getenv("TRACKER_WATCH_SECONDS", "20"))
        self.id = watcher_id or f"watcher:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

    def run_once(self) -> dict:
        report = {"wallets": 0, "new_transactions": 0, "alerts": 0, "retried": 0, "errors": []}
        with self.sessions() as session:
            touched = set()
            favorites = list(session.scalars(select(Wallet).where(Wallet.favorite.is_(True))))
            for wallet in favorites:
                state = session.get(SyncState, wallet.address)
                if state is None or state.head_signature is None:
                    continue  # needs its first sync (the worker schedules it for watchlist wallets)
                report["wallets"] += 1
                try:
                    page = self.rpc.signatures(wallet.address, until=state.watch_head or state.head_signature,
                                               limit=PAGE)
                except ProviderError as exc:
                    report["errors"].append(exc.message)
                    continue
                for item in reversed(page):  # oldest first
                    try:
                        if process_signature(session, self.rpc, item["signature"], [wallet.address]):
                            touched.add(wallet.address)
                            report["new_transactions"] += 1
                    except ProviderError as exc:
                        session.rollback()
                        report["errors"].append(exc.message)
                        break  # retry from here next round
                    state.watch_head = item["signature"]
                    session.commit()
            _price(session, self.prices, touched)
            report["retried"] = retry_pending(session, self.rpc, self.prices)
            report["alerts"] = generate_alerts(session)["created"]
            deliver_pending(session, self.notifier)
            # Prospective record: quotes at each alert and 5/60 minutes later, fetched when due.
            report["quotes"] = capture_quotes(session, self.quotes)["captured"]
            row = session.get(WorkerHeartbeat, self.id)
            if row is None:
                session.add(WorkerHeartbeat(id=self.id, info={"kind": "watcher", "interval_seconds": self.interval,
                                                              "watching": report["wallets"]}))
            else:
                row.heartbeat_at = now_iso()
                row.info = {"kind": "watcher", "interval_seconds": self.interval, "watching": report["wallets"]}
            session.commit()
        return report

    def run_forever(self, stop: threading.Event):
        log.info("watcher %s started (every %ss)", self.id, self.interval)
        while not stop.is_set():
            try:
                report = self.run_once()
                if report["new_transactions"] or report["alerts"]:
                    log.info("watcher: %s new transactions, %s alerts", report["new_transactions"], report["alerts"])
            except Exception:
                log.exception("watcher loop error")
            stop.wait(self.interval)
