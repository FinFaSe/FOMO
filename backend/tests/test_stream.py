"""WebSocket stream against a local SYNTHETIC JSON-RPC WebSocket server (logsSubscribe / logsNotification)."""

import asyncio
import json
import threading
import time

import pytest
import websockets

from tracker.adapters.solana import ProviderError
from tracker.db import PendingSignature
from tracker.stream import Stream, ws_url

from .conftest import WALLET, b58
from .test_alerts import alerts, watch_wallet
from .test_fast_alerts import confirmed_buy
from .test_incremental_sync import client  # noqa: F401  (fixture)


class FakeCluster:
    """Accepts logsSubscribe/logsUnsubscribe and pushes logsNotification to the subscribed wallets."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.subs, self.connections = {}, []
        self.ids = iter(range(100, 10_000))
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(5)

    async def _start(self):
        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def _handler(self, ws):
        self.connections.append(ws)
        async for raw in ws:
            msg = json.loads(raw)
            if msg["method"] == "logsSubscribe":
                sub = next(self.ids)
                self.subs[msg["params"][0]["mentions"][0]] = (sub, ws)
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": sub}))
            elif msg["method"] == "logsUnsubscribe":
                self.subs = {w: v for w, v in self.subs.items() if v[0] != msg["params"][0]}
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": True}))

    def notify(self, wallet, signature, err=None):
        sub, ws = self.subs[wallet]
        body = {"jsonrpc": "2.0", "method": "logsNotification", "params": {"subscription": sub, "result": {
            "context": {"slot": 1}, "value": {"signature": signature, "err": err, "logs": []}}}}
        asyncio.run_coroutine_threadsafe(ws.send(json.dumps(body)), self.loop).result(5)

    def drop_connections(self):
        for ws in list(self.connections):
            asyncio.run_coroutine_threadsafe(ws.close(), self.loop).result(5)
        self.connections.clear()
        self.subs.clear()

    def close(self):
        async def shutdown():
            self.server.close()
            await self.server.wait_closed()

        asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


def until(condition, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def cluster():
    fake = FakeCluster()
    yield fake
    fake.close()


@pytest.fixture
def streaming(client, cluster):  # noqa: F811
    """The stream running against the fake cluster; stopped (and its thread joined) after the test."""
    c, rpc = client
    stream = Stream(c.app.state.sessions, rpc, c.app.state.prices, url=cluster.url, stream_id="stream:test",
                    finalize_after=0.3, retries=(0.1, 0.1, 0.2), resubscribe=0.3)
    stop = threading.Event()
    thread = threading.Thread(target=stream.run_forever, args=(stop,), daemon=True)
    thread.start()
    yield c, rpc, cluster, stream
    stop.set()
    thread.join(10)


def test_a_notified_buy_alerts_provisionally_then_is_stored_finalized(streaming):
    c, rpc, cluster, stream = streaming
    watch_wallet(c)
    assert until(lambda: WALLET in cluster.subs), "the watchlist wallet was not subscribed"
    sig = confirmed_buy(rpc)
    rpc.pending.discard(sig)  # finalized by the time the stream stores it
    cluster.notify(WALLET, sig)
    assert until(lambda: alerts(c)["items"]), "no provisional alert"
    assert alerts(c)["items"][0]["signature"] == sig and alerts(c)["items"][0]["details"]["detected_by"] == "stream"
    assert until(lambda: alerts(c)["items"][0]["commitment"] == "finalized"), "not completed once finalized"
    assert alerts(c)["items"][0]["details"]["detected_by"] == "stream"  # kept when completed
    assert len(alerts(c)["items"]) == 1 and stream.stats["provisional_alerts"] == 1
    worker = c.get("/api/worker").json()
    assert worker["stream_connected"] and worker["streams"][0]["info"]["subscriptions"] == 1
    assert not worker["workers"]  # the stream is not counted as a sync worker


def test_a_transaction_served_late_at_confirmed_is_retried_and_failures_ignored(streaming):
    c, rpc, cluster, stream = streaming
    watch_wallet(c)
    assert until(lambda: WALLET in cluster.subs)
    sig = confirmed_buy(rpc)
    original, calls = rpc.confirmed_transaction, []

    def late(signature):
        calls.append(signature)
        if len(calls) < 3:
            raise ProviderError("Transacción no disponible en confirmed", 409)
        return original(signature)

    rpc.confirmed_transaction = late
    cluster.notify(WALLET, b58(31, 64), err={"InstructionError": [0, "Custom"]})  # failed: not fetched
    cluster.notify(WALLET, sig)
    assert until(lambda: alerts(c)["items"]) and len(calls) == 3
    assert alerts(c)["items"][0]["commitment"] == "confirmed"  # not finalized yet: queued for the watcher

    def queued():
        with c.app.state.sessions() as s:
            return s.get(PendingSignature, sig) is not None

    assert until(queued)


def test_watchlist_changes_and_reconnections_resubscribe(streaming):
    c, rpc, cluster, stream = streaming
    watch_wallet(c)
    assert until(lambda: WALLET in cluster.subs)
    c.patch(f"/api/wallets/{WALLET}", json={"favorite": False})
    assert until(lambda: WALLET not in cluster.subs), "not unsubscribed after leaving the watchlist"
    c.patch(f"/api/wallets/{WALLET}", json={"favorite": True})
    assert until(lambda: WALLET in cluster.subs)
    cluster.drop_connections()
    assert until(lambda: stream.stats["reconnects"] >= 1 and WALLET in cluster.subs, timeout=10)


def test_ws_url_follows_the_rpc_url_and_can_be_set(monkeypatch):
    monkeypatch.delenv("SOLANA_WS_URL", raising=False)
    assert ws_url("https://rpc.example.org/path?api-key=k") == "wss://rpc.example.org/path?api-key=k"
    monkeypatch.setenv("SOLANA_WS_URL", "wss://ws.example.org")
    assert ws_url("https://rpc.example.org") == "wss://ws.example.org"


def test_an_alert_in_flight_survives_a_disconnection(streaming):
    c, rpc, cluster, stream = streaming
    watch_wallet(c)
    assert until(lambda: WALLET in cluster.subs)
    sig = confirmed_buy(rpc)
    original, calls = rpc.confirmed_transaction, []

    def slow(signature):
        calls.append(signature)
        if len(calls) < 3:  # still being retried when the connection drops
            raise ProviderError("Transacción no disponible en confirmed", 409)
        return original(signature)

    rpc.confirmed_transaction = slow
    cluster.notify(WALLET, sig)
    assert until(lambda: calls)
    cluster.drop_connections()
    assert until(lambda: alerts(c)["items"]), "the alert in flight was lost with the connection"
