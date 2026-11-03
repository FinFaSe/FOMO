"""End-to-end with a SYNTHETIC RPC: ingest → normalized trades → SOL positions, rebuildable offline."""

import time
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.orm import Session

from tracker.db import SyncState, Trade
from tracker.normalize import NORMALIZER_VERSION

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
    assert buy["evidence"]["trade"] == "verified"
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


def test_sync_values_trades_in_usd_and_feeds_the_usd_analysis(client):  # noqa: F811
    c, rpc = client
    round_trip(rpc)
    report = c.post(SYNC, json={})
    report = report.json()
    assert report["pricing"]["priced"] == 2 and report["pricing"]["market_caps"] == 2
    sell, buy = c.get(f"/api/wallets/{WALLET}/trades").json()["items"]
    fee = Decimal("0.000105")
    assert buy["evidence"]["usd"] == "verified" and buy["evidence"]["market_cap"] == "verified"
    assert Decimal(buy["usd"]["gross_usd"]) == 150 and Decimal(buy["usd"]["fees_usd"]) == fee * 150
    assert buy["usd"]["sol_usd"] == "150" and buy["usd"]["sol_usd_range"] == ["149", "151"]
    # pump.fun mint: fixed 1e9 supply upper bound × USD price per token.
    assert Decimal(buy["usd"]["market_cap_usd"]) == Decimal(150) / Decimal(3_500_000) * 10 ** 9
    assert buy["usd"]["market_cap_method"] == "pumpfun_fixed_supply_upper_bound"
    assert Decimal(sell["usd"]["gross_usd"]) == Decimal("225")

    analysis = c.get(f"/api/wallets/{WALLET}/analysis").json()
    [p] = analysis["positions"]
    assert p["evaluable"] and p["verified"] and p["market_cap_verified"] and p["early_entry_reported"]
    assert Decimal(p["realized_pnl_usd"]) == Decimal(75) - 2 * fee * 150
    assert analysis["history_complete"] and "incomplete_history" not in analysis["exclusion_reasons"]
    assert "unverified_trades_usd_or_historical_market_cap" not in analysis["exclusion_reasons"]
    # One position is far from the 30 required: still not eligible.
    assert not analysis["eligible"] and "insufficient_evaluable_positions" in analysis["exclusion_reasons"]
    assert c.get("/api/rankings").json()["total"] == 0


def test_renormalize_uses_stored_evidence_only(client):  # noqa: F811
    c, rpc = client
    round_trip(rpc)
    c.post(SYNC, json={})
    fetched = len(rpc.fetches)
    prices = c.app.state.prices
    calls = len(prices.calls)
    rebuilt = c.post(f"/api/wallets/{WALLET}/normalize").json()
    assert rebuilt["trades"] == 2 and rebuilt["normalizer"] == NORMALIZER_VERSION and rebuilt["rpc_calls"] == 0
    # Rebuilt trades are re-valued from cached candles: no RPC and no price request.
    assert rebuilt["pricing"]["priced"] == 2 and rebuilt["pricing"]["price_requests"] == 0
    assert len(rpc.fetches) == fetched and len(prices.calls) == calls
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
