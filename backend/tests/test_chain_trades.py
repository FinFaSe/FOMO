"""End-to-end with a SYNTHETIC RPC: ingest → normalized trades → SOL positions, rebuildable offline."""

import time
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.orm import Session

from tracker.db import SyncState, Trade

from .conftest import TOKEN, WALLET, b58
from .test_incremental_sync import client  # noqa: F401  (fixture)
from .test_normalize import pump_buy, pump_sell

SYNC = f"/api/wallets/{WALLET}/sync"


def serve(rpc, payloads):
    """Replace the synthetic history with the given transactions (oldest first)."""
    rpc.history = []
    for n, payload in enumerate(payloads):
        sig = b58(5000 + n, 64)
        payload["transaction"]["signatures"] = [sig]
        rpc.payloads[sig] = payload
        rpc.history.insert(0, {"signature": sig, "blockTime": payload["blockTime"], "err": None})


def round_trip(rpc):
    now = int(time.time())
    serve(rpc, [pump_buy(block_time=now - 7200), pump_sell(block_time=now - 3600)])


def test_sync_normalizes_swaps_and_rebuilds_sol_positions(client):  # noqa: F811
    c, rpc = client
    round_trip(rpc)
    report = c.post(SYNC, json={}).json()
    assert report["state"] == "scanned" and report["trades"] == 2
    trades = c.get(f"/api/wallets/{WALLET}/trades").json()
    assert trades["total"] == 2
    sell, buy = trades["items"]  # newest first
    assert (buy["kind"], buy["quote_asset"], buy["quote_amount"]) == ("buy", "SOL", "1")
    assert (sell["kind"], sell["quote_amount"], sell["venues"]) == ("sell", "1.5", ["pump.fun"])
    assert buy["evidence"]["trade"] == "verified" and buy["evidence"]["usd"] == "unknown"
    assert c.get(f"/api/wallets/{WALLET}/trades?mint={TOKEN}").json()["total"] == 2
    assert c.get(f"/api/wallets/{WALLET}/trades?mint=nope").status_code == 422

    positions = c.get(f"/api/wallets/{WALLET}/chain-positions").json()
    assert positions["denomination"] == "SOL"
    [p] = positions["positions"]
    fee = Decimal("0.000105")
    assert p["status"] == "closed" and p["evaluable"] and p["issues"] == []
    assert Decimal(p["realized_cost_sol"]) == 1 + fee
    assert Decimal(p["realized_proceeds_sol"]) == Decimal("1.5") - fee
    assert Decimal(p["realized_pnl_sol"]) == Decimal("0.5") - 2 * fee
    assert Decimal(p["avg_entry_price_sol"]) == Decimal(1) / Decimal(3_500_000)
    summary = positions["summary"]
    assert summary["wins"] == 1 and summary["win_rate"] == 1 and summary["evaluable_positions"] == 1
    # Chain trades never feed the validated USD ranking.
    assert c.get("/api/rankings").json()["total"] == 0
    assert c.get(f"/api/wallets/{WALLET}/analysis").json()["positions"] == []


def test_renormalize_uses_stored_evidence_only(client):  # noqa: F811
    c, rpc = client
    round_trip(rpc)
    c.post(SYNC, json={})
    fetched = len(rpc.fetches)
    rebuilt = c.post(f"/api/wallets/{WALLET}/normalize").json()
    assert rebuilt == {"transactions": 2, "trades": 2, "normalizer": "balance-delta-v1", "rpc_calls": 0}
    assert len(rpc.fetches) == fetched
    assert c.get(f"/api/wallets/{WALLET}/trades").json()["total"] == 2


def test_wallets_scanned_before_normalization_are_rebuilt_on_next_sync(client):  # noqa: F811
    c, rpc = client
    round_trip(rpc)
    c.post(SYNC, json={})
    with Session(c.app.state.engine) as s:  # simulate a database from before this normalizer existed
        s.execute(delete(Trade))
        s.get(SyncState, WALLET).normalizer = None
        s.commit()
    fetched = len(rpc.fetches)
    report = c.post(SYNC, json={}).json()
    assert report["renormalized"]["trades"] == 2 and len(rpc.fetches) == fetched
    assert c.get(f"/api/wallets/{WALLET}/trades").json()["total"] == 2


def test_sell_without_known_entry_is_not_evaluable(client):  # noqa: F811
    c, rpc = client
    serve(rpc, [pump_sell(block_time=int(time.time()) - 60)])
    c.post(SYNC, json={})
    [p] = c.get(f"/api/wallets/{WALLET}/chain-positions").json()["positions"]
    assert not p["evaluable"] and "missing_opening_inventory" in p["issues"]
