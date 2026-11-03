"""Watchlist alerts, watcher, pushed signatures and delivery (SYNTHETIC RPC, prices and webhooks)."""

import json
import time

import httpx

from tracker.adapters.solana import ProviderError
from tracker.alerts import Notifier
from tracker.db import SyncState, WalletFunding
from tracker.watch import Watcher

from .conftest import WALLET, b58
from .test_discovery import curve_trade
from .test_incremental_sync import client  # noqa: F401  (fixture)
from .test_normalize import pump_buy

SYNC = f"/api/wallets/{WALLET}/sync"
MINT = b58(5501)


def arrive(rpc, payload, owner=None, n=0):
    """A new transaction appears at the top of `owner`'s history (default: the shared history)."""
    sig = b58(900_000 + n + int(payload["blockTime"]) % 100_000, 64)
    payload["transaction"]["signatures"] = [sig]
    rpc.payloads[sig] = payload
    history = rpc.histories.setdefault(owner, []) if owner else rpc.history
    history.insert(0, {"signature": sig, "blockTime": payload["blockTime"], "err": None})
    return sig


def watcher(c, rpc, **kwargs):
    return Watcher(c.app.state.sessions, rpc, c.app.state.prices, interval=20, watcher_id="watcher:test", **kwargs)


def watch_wallet(c):
    c.patch(f"/api/wallets/{WALLET}", json={"favorite": True})
    c.post(SYNC, json={})  # first sync sets head (the old synthetic transaction: too old to alert)


def alerts(c, **params):
    return c.get("/api/alerts", params=params).json()


def test_watcher_alerts_on_a_new_buy_without_moving_sync_cursors(client):  # noqa: F811
    c, rpc = client
    watch_wallet(c)
    assert alerts(c)["items"] == []
    sig = arrive(rpc, pump_buy(mint=MINT, block_time=int(time.time()) - 30))
    w = watcher(c, rpc)
    report = w.run_once()
    assert report["new_transactions"] == 1 and report["alerts"] == 1
    [a] = alerts(c)["items"]
    assert (a["kind"], a["wallet"], a["mint"], a["signature"]) == ("buy", WALLET, MINT, sig)
    assert a["details"]["usd"] == "150" and a["details"]["market_cap_status"] == "verified"
    assert a["commitment"] == "finalized" and a["details"]["detected_by"] == "finalized"
    assert 25 <= a["alert_seconds"] <= 120 and a["detection_seconds"] <= a["alert_seconds"]
    assert w.run_once()["alerts"] == 0
    with c.app.state.sessions() as s:
        state = s.get(SyncState, WALLET)
        assert state.watch_head == sig and state.head_signature != sig  # sync cursors untouched
    fetched = len(rpc.fetches)
    sync = c.post(SYNC, json={}).json()
    assert sync["rpc_transaction_fetches"] == 0 and len(rpc.fetches) == fetched  # stored by the watcher
    assert len(alerts(c)["items"]) == 1
    assert c.get("/api/worker").json()["watcher_alive"]


def test_only_recent_trades_of_watchlist_wallets_alert(client):  # noqa: F811
    c, rpc = client
    c.post(SYNC, json={})  # not on the watchlist
    arrive(rpc, pump_buy(mint=MINT, block_time=int(time.time()) - 30))
    c.post(SYNC, json={})
    assert alerts(c)["items"] == []
    c.patch(f"/api/wallets/{WALLET}", json={"favorite": True})
    arrive(rpc, pump_buy(mint=b58(5502), block_time=int(time.time()) - 3600), n=1)  # an hour old
    watcher(c, rpc).run_once()
    assert alerts(c)["items"] == []


def test_convergence_counts_linked_wallets_once(client):  # noqa: F811
    c, rpc = client
    now = int(time.time())
    a, b, cc, d, e = (b58(5600 + i) for i in range(5))
    for i, wallet in enumerate((a, b, cc, d, e)):
        c.post("/api/wallets", json={"address": wallet, "label": f"W{i}"})
        c.patch(f"/api/wallets/{wallet}", json={"favorite": True})
    with c.app.state.sessions() as s:  # d shares a (personal) funder with cc: one entity
        for wallet in (cc, d):
            s.merge(WalletFunding(address=wallet, state="done", funder=b58(5699), funder_kind="wallet"))
        s.commit()
    for n, wallet in enumerate((a, b, cc, d, e)):
        arrive(rpc, curve_trade(wallet, MINT, "buy", 1, 30_000_000, now - 300 + n * 30, (now + n) * 2), owner=wallet)
        c.post(f"/api/wallets/{wallet}/sync", json={})
    found = alerts(c, kind="convergence")["items"]
    levels = sorted(x["details"]["entities"] for x in found)
    assert levels == [3, 4]  # a+b+cc reach 3; d is cc's entity (no new level); e makes 4
    top = max(found, key=lambda x: x["details"]["entities"])
    assert [p["label"] for p in top["details"]["participants"]] == ["W0", "W1", "W2", "W4"]
    assert len(alerts(c, kind="buy")["items"]) == 5


def test_inbound_webhook_secret_processing_and_deferred_signatures(client, monkeypatch):  # noqa: F811
    c, rpc = client
    watch_wallet(c)
    sig = arrive(rpc, pump_buy(mint=MINT, block_time=int(time.time()) - 20))
    hook = "/api/hooks/transactions"
    assert c.post(hook, json=[{"signature": sig}]).status_code == 404  # not configured
    monkeypatch.setenv("TRACKER_HOOK_SECRET", "synthetic-hook-secret")
    monkeypatch.setenv("TRACKER_ACCESS_TOKEN", "synthetic-dashboard-token")
    assert c.post(hook, json=[{"signature": sig}], headers={"X-Tracker-Hook-Secret": "wrong"}).status_code == 401
    headers = {"X-Tracker-Hook-Secret": "synthetic-hook-secret"}  # no dashboard token needed
    assert c.post(hook, json={"signatures": ["nope"]}, headers=headers).status_code == 422
    r = c.post(hook, json=[{"transaction": {"signatures": [sig]}}], headers=headers).json()
    assert r["processed"] == 1 and r["wallets"] == [WALLET] and r["alerts"] == 1
    # Not finalized yet: kept and retried by the watcher.
    late = arrive(rpc, pump_buy(mint=b58(5503), block_time=int(time.time()) - 5), n=7)
    original = rpc.transaction

    def not_yet(signature):
        if signature == late:
            raise ProviderError("Transacción no disponible en finalized", 409)
        return original(signature)

    rpc.transaction = not_yet
    assert c.post(hook, json=[late], headers=headers).json()["pending"] == [late]
    rpc.transaction = original
    report = watcher(c, rpc).run_once()
    assert report["retried"] == 1
    monkeypatch.delenv("TRACKER_ACCESS_TOKEN")
    assert {a["signature"] for a in alerts(c)["items"]} == {sig, late}


def test_delivery_formats_failures_and_secret_url(client, monkeypatch):  # noqa: F811
    c, rpc = client
    sent = []

    def ok(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200)

    url = "https://hooks.example.org/synthetic/SECRET-PATH"
    monkeypatch.setenv("TRACKER_ALERT_WEBHOOK_URL", url)
    watch_wallet(c)
    arrive(rpc, pump_buy(mint=MINT, block_time=int(time.time()) - 30))
    notifier = Notifier(fmt="discord", client=httpx.Client(transport=httpx.MockTransport(ok)))
    watcher(c, rpc, notifier=notifier).run_once()
    assert len(sent) == 1 and sent[0]["content"].startswith("[COMPRA]")
    [a] = alerts(c)["items"]
    assert a["delivery"]["state"] == "sent"
    config = c.get("/api/config").text
    assert "SECRET-PATH" not in config and json.loads(config)["alerts"]["webhook_configured"]
    # Bodies per format.
    from tracker.db import Alert
    with c.app.state.sessions() as s:
        alert = s.get(Alert, a["id"])
        assert set(Notifier(url=url, fmt="slack").body(alert)) == {"text"}
        assert Notifier(url=url, fmt="telegram", chat_id="42").body(alert)["chat_id"] == "42"
        assert not Notifier(url=url, fmt="telegram", chat_id="").configured
        assert Notifier(url=url, fmt="json").body(alert)["alert"]["id"] == a["id"]
    # Failures are retried up to three times, then marked failed.
    arrive(rpc, pump_buy(mint=b58(5504), block_time=int(time.time()) - 10), n=3)
    failing = Notifier(fmt="slack", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    w = watcher(c, rpc, notifier=failing)
    for _ in range(3):
        w.run_once()
    latest = alerts(c)["items"][0]
    assert latest["delivery"]["state"] == "failed" and latest["delivery"]["attempts"] == 3


def test_alerts_api_unread_filters_and_no_duplicates_after_rebuild(client):  # noqa: F811
    c, rpc = client
    watch_wallet(c)
    arrive(rpc, pump_buy(mint=MINT, block_time=int(time.time()) - 30))
    w = watcher(c, rpc)
    w.run_once()
    listing = alerts(c)
    assert listing["unread"] == 1 and listing["settings"]["convergence_min"] == 3
    assert c.get("/api/alerts?kind=nope").status_code == 422
    assert alerts(c, since_id=listing["items"][0]["id"])["items"] == []
    assert c.post("/api/alerts/read", json={"ids": [listing["items"][0]["id"]]}).json()["unread"] == 0
    assert alerts(c, unread=True)["items"] == []
    # Rebuilding trades gives them new ids; the alert keys keep generation idempotent.
    c.post(f"/api/wallets/{WALLET}/normalize")
    assert w.run_once()["alerts"] == 0 and len(alerts(c)["items"]) == 1
    assert c.post("/api/alerts/read", json={"all": True}).status_code == 200
