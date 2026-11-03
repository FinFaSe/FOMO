"""Provisional alerts from `confirmed` transactions, completed once finalized (SYNTHETIC RPC and prices)."""

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from tracker.alerts import alert_text, provisional_alerts, settle_provisional
from tracker.db import Alert, PendingSignature
from tracker.watch import push_signatures

from .conftest import WALLET, b58
from .test_alerts import MINT, alerts, arrive, watch_wallet, watcher
from .test_incremental_sync import client  # noqa: F401  (fixture)
from .test_normalize import pump_buy


def confirmed_buy(rpc, seconds_ago=3, n=0):
    """A buy the cluster confirmed but has not finalized yet."""
    sig = arrive(rpc, pump_buy(mint=MINT, block_time=int(time.time()) - seconds_ago), n=n)
    rpc.pending.add(sig)
    return sig


def provisional(c, rpc, sig, wallets=(WALLET,), **kwargs):
    with c.app.state.sessions() as s:
        return [a.key for a in provisional_alerts(s, rpc, sig, list(wallets), prices=c.app.state.prices, **kwargs)]


def age_alerts(c, seconds):
    with c.app.state.sessions() as s:
        old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        s.execute(update(Alert).values(created_at=old))
        s.commit()


def test_a_confirmed_buy_alerts_before_it_is_finalized_and_is_completed_once_finalized(client):  # noqa: F811
    c, rpc = client
    watch_wallet(c)
    sig = confirmed_buy(rpc)
    assert provisional(c, rpc, sig, detected_at="2026-11-03T18:00:00+00:00") == [f"buy:{WALLET}:{sig}:0"]
    [a] = alerts(c)["items"]
    assert a["commitment"] == "confirmed" and a["finalized_at"] is None and a["signature"] == sig
    assert a["details"]["usd"] == "150" and a["details"]["usd_basis"] == "latest_sol_usd"  # 1 SOL × latest close
    assert a["details"]["market_cap_status"] == "unknown" and a["detected_at"] == "2026-11-03T18:00:00+00:00"
    with c.app.state.sessions() as s:
        assert "confirmada, sin finalizar" in alert_text(s.get(Alert, a["id"])) and "≈USD 150.00" in alert_text(
            s.get(Alert, a["id"]))
    assert provisional(c, rpc, sig) == []  # idempotent
    # Not finalized yet: the watcher stores nothing and raises nothing new.
    assert watcher(c, rpc).run_once()["alerts"] == 0 and len(alerts(c)["items"]) == 1
    rpc.pending.discard(sig)
    report = watcher(c, rpc).run_once()
    assert report["new_transactions"] == 1 and report["alerts"] == 0  # the same alert, completed
    [done] = alerts(c)["items"]
    assert done["id"] == a["id"] and done["commitment"] == "finalized" and done["finalized_at"]
    assert done["details"]["market_cap_status"] == "verified" and done["details"]["provisional"]["usd"] == "150"
    assert done["detected_at"] == "2026-11-03T18:00:00+00:00"  # detection time of the first sighting


def test_only_recent_successful_trades_of_watchlist_wallets_alert_provisionally(client):  # noqa: F811
    c, rpc = client
    sig = confirmed_buy(rpc)
    assert provisional(c, rpc, sig) == []  # not on the watchlist
    watch_wallet(c)
    old = confirmed_buy(rpc, seconds_ago=3600, n=1)
    assert provisional(c, rpc, old) == []  # older than the alert window
    failed = confirmed_buy(rpc, n=2)
    rpc.payloads[failed]["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    assert provisional(c, rpc, failed) == []
    assert provisional(c, rpc, sig, wallets=(b58(4321),)) == []  # another wallet's subscription


def test_provisional_alerts_that_never_finalize_are_dropped(client):  # noqa: F811
    c, rpc = client
    watch_wallet(c)
    unknown, failed, finalized = (confirmed_buy(rpc, n=i) for i in range(3))
    for sig in (unknown, failed, finalized):
        provisional(c, rpc, sig)
    rpc.statuses = {failed: {"err": {"InstructionError": [0, "Custom"]}, "confirmationStatus": "finalized"},
                    finalized: {"err": None, "confirmationStatus": "finalized"}}
    with c.app.state.sessions() as s:
        assert settle_provisional(s, rpc) == {"finalized": 0, "queued": 0, "dropped": 0}  # too recent to settle
    age_alerts(c, 120)
    with c.app.state.sessions() as s:
        assert settle_provisional(s, rpc) == {"finalized": 0, "queued": 1, "dropped": 1}
        assert s.get(PendingSignature, finalized).source == "provisional"
    age_alerts(c, 400)
    with c.app.state.sessions() as s:
        assert settle_provisional(s, rpc)["dropped"] == 1  # unknown to the cluster for 5 minutes
        states = dict(s.execute(select(Alert.signature, Alert.commitment)).all())
    assert states == {unknown: "dropped", failed: "dropped", finalized: "confirmed"}
    rpc.pending.discard(finalized)
    watcher(c, rpc).run_once()  # the queued signature is stored and the alert completed
    assert {a["signature"]: a["commitment"] for a in alerts(c)["items"]}[finalized] == "finalized"


def test_a_late_finalization_still_completes_the_alert(client):  # noqa: F811
    c, rpc = client
    watch_wallet(c)
    sig = confirmed_buy(rpc)
    provisional(c, rpc, sig)
    rpc.pending.discard(sig)
    c.post(f"/api/wallets/{WALLET}/sync", json={})  # stored by a sync job: its alert scan completes the alert
    assert alerts(c)["items"][0]["commitment"] == "finalized"
    # Safety net when the scan missed it (e.g. finalized after the alert window): completed from the stored trade.
    with c.app.state.sessions() as s:
        s.execute(update(Alert).values(commitment="confirmed", finalized_at=None))
        s.commit()
    age_alerts(c, 120)
    with c.app.state.sessions() as s:
        assert settle_provisional(s, rpc)["finalized"] == 1
    assert alerts(c)["items"][0]["commitment"] == "finalized"


def test_the_polling_fallback_alerts_provisionally_and_completes_on_a_later_round(client):  # noqa: F811
    c, rpc = client
    watch_wallet(c)
    sig = confirmed_buy(rpc)
    w = watcher(c, rpc)
    first = w.run_once()
    assert first["provisional_alerts"] == 1 and first["new_transactions"] == 0
    assert alerts(c)["items"][0]["commitment"] == "confirmed"
    assert alerts(c)["items"][0]["details"]["detected_by"] == "watcher"
    assert w.run_once()["provisional_alerts"] == 0  # the confirmed version is not fetched again
    rpc.pending.discard(sig)
    final = w.run_once()
    assert final["new_transactions"] == 1 and final["alerts"] == 0
    [a] = alerts(c)["items"]
    assert a["commitment"] == "finalized"


def test_pushed_signatures_alert_provisionally_until_finalized(client):  # noqa: F811
    c, rpc = client
    watch_wallet(c)
    sig = confirmed_buy(rpc)
    with c.app.state.sessions() as s:
        report = push_signatures(s, rpc, c.app.state.prices, [sig], "helius")
    assert report["pending"] == [sig] and report["provisional_alerts"] == 1  # wallets found in the transaction
    assert alerts(c)["items"][0]["details"]["detected_by"] == "webhook:helius"
    rpc.pending.discard(sig)
    watcher(c, rpc).run_once()  # retries the pending signature
    assert alerts(c)["items"][0]["commitment"] == "finalized"
