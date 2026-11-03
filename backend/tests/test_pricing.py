"""USD valuation, entry market cap and ranking eligibility with SYNTHETIC RPC and SYNTHETIC prices."""

import json
import time
from decimal import Decimal

import httpx
import pytest

from tracker.adapters.prices import CoinbaseCandles
from tracker.adapters.solana import ProviderError
from tracker.pricing import MAX_CANDLES, _windows

from .conftest import WALLET, b58
from .test_chain_trades import serve
from .test_incremental_sync import client  # noqa: F401  (fixture)
from .test_normalize import RAYDIUM, SOL, USDC, balance, pump_buy, pump_sell, relayed_usdc_sell, tx

SYNC = f"/api/wallets/{WALLET}/sync"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
DAY = 86400


def trades(c):
    return c.get(f"/api/wallets/{WALLET}/trades?limit=200").json()["items"]


def test_windows_batch_nearby_minutes_and_never_exceed_the_candle_limit():
    hour = 3600
    assert len(_windows([0, hour])) == 1
    assert len(_windows([0, 10 * hour])) == 2
    for start, end in _windows([i * 60 for i in range(0, 2000, 7)]):
        assert (end - start) // 60 <= MAX_CANDLES


def test_usdc_quote_is_valued_at_par_and_fees_use_sol_price(client):  # noqa: F811
    c, rpc = client
    usdc_ata, ata = b58(631), b58(632)
    now = int(time.time())
    serve(rpc, [tx([WALLET, usdc_ata, ata], [SOL, 0, 0], [SOL - 5000, 0, 0],
                   pre_tokens=[balance(1, USDC, WALLET, 500_000_000), balance(2, b58(9001), WALLET, 0)],
                   post_tokens=[balance(1, USDC, WALLET, 400_000_000), balance(2, b58(9001), WALLET, 7_000_000)],
                   programs=[RAYDIUM], block_time=now - 600)])
    c.post(SYNC, json={})
    [t] = trades(c)
    assert t["usd"]["quote_method"] == "stablecoin_par" and t["usd"]["gross_usd"] == "100"
    assert Decimal(t["usd"]["fees_usd"]) == Decimal("0.000005") * 150
    # Not a pump.fun mint: the market cap is not invented.
    assert t["usd"]["market_cap_usd"] is None and t["evidence"]["market_cap"] == "unknown"


def test_nearest_candle_fallback_and_unpriced_without_candles(client):  # noqa: F811
    c, rpc = client
    now = int(time.time()) // 60 * 60
    prices = c.app.state.prices
    prices.gaps = {now - 3600}  # the buy's exact minute has no ticks
    prices.gaps |= {now - 1800 + o for o in range(-600, 660, 60)}  # nothing near the sell
    serve(rpc, [pump_buy(block_time=now - 3600 + 10), pump_sell(block_time=now - 1800 + 10)])
    report = c.post(SYNC, json={}).json()
    assert report["pricing"]["priced"] == 1 and report["pricing"]["unpriced"] == 1
    sell, buy = trades(c)
    assert buy["usd"]["candle_offset_seconds"] == -60 and buy["evidence"]["usd"] == "verified"
    assert sell["usd"]["gross_usd"] is None and sell["evidence"]["usd"] == "unpriced"
    analysis = c.get(f"/api/wallets/{WALLET}/analysis").json()
    assert "incomplete_position_coverage" in analysis["exclusion_reasons"]
    # Unpriced trades are retried on the next call; priced ones are not re-requested.
    prices.gaps = set()
    retry = c.post(f"/api/wallets/{WALLET}/price").json()
    assert retry["priced"] == 1 and retry["price_requests"] == 1
    assert c.post(f"/api/wallets/{WALLET}/price").json()["price_requests"] == 0


def test_market_cap_needs_proof_of_pump_origin_and_is_backfilled(client):  # noqa: F811
    c, rpc = client
    now = int(time.time())
    mint = b58(9100)
    serve(rpc, [pump_buy(mint=mint, program=PUMPSWAP, block_time=now - 7200)])
    c.post(SYNC, json={})
    [swap] = trades(c)
    assert swap["venues"] == ["pumpswap"] and swap["usd"]["market_cap_usd"] is None
    # A stored bonding-curve trade of the same mint proves its pump.fun origin (fixed 1e9 supply).
    curve_trade = pump_buy(mint=mint, block_time=now - 600)
    curve_trade["transaction"]["signatures"] = [b58(9999, 64)]
    rpc.payloads[b58(9999, 64)] = curve_trade
    rpc.history.insert(0, {"signature": b58(9999, 64), "blockTime": now - 600, "err": None})
    report = c.post(SYNC, json={}).json()
    assert report["pricing"]["market_caps"] == 2
    assert all(t["evidence"]["market_cap"] == "verified" for t in trades(c))


def test_analysis_window_longer_than_the_scan_is_incomplete(client):  # noqa: F811
    c, rpc = client
    now = int(time.time())
    serve(rpc, [pump_buy(mint=b58(9200), block_time=now - 30 * DAY),
                pump_buy(block_time=now - 7200), pump_sell(block_time=now - 3600)])
    report = c.post(SYNC, json={"days": 5}).json()
    url = f"/api/wallets/{WALLET}/analysis"
    assert "incomplete_history" in c.get(url).json()["exclusion_reasons"]
    short = c.get(url, params={"analysis_days": 5}).json()
    assert short["history_complete"]
    # Default as_of is the end of the last completed scan, not "now".
    assert short["as_of"] <= report["finished_at"]
    later = c.get(url, params={"analysis_days": 5, "as_of": report["finished_at"]}).json()
    assert not later["history_complete"]


def test_wallet_without_activity_in_the_window_is_complete(client):  # noqa: F811
    c, rpc = client
    serve(rpc, [pump_buy(block_time=int(time.time()) - 30 * DAY)])
    report = c.post(SYNC, json={"days": 5}).json()
    assert report["state"] == "scanned" and report["transactions"] == 0
    assert c.get(f"/api/wallets/{WALLET}/analysis", params={"analysis_days": 5}).json()["history_complete"]


def test_thirty_verified_early_winners_enter_the_ranking(client):  # noqa: F811
    c, rpc = client
    now = int(time.time())
    history = []
    for i in range(30):
        mint, day = b58(7000 + i), now - (i + 1) * DAY
        exit_sol = 7 * SOL if i == 0 else 3 * SOL // 2  # one 7× exit: ROI ≈ +600%
        history += [pump_buy(mint=mint, block_time=day - 3600), pump_sell(mint=mint, sol=exit_sol, block_time=day)]
    serve(rpc, sorted(history, key=lambda t: t["blockTime"]))
    first = c.post(SYNC, json={"max_pages": 1, "page_size": 100}).json()
    assert first["state"] == "scanned" and first["trades"] == 60
    # 30 separate days need 30 price windows; the per-call budget is 20, the rest comes next call.
    assert first["pricing"]["price_requests"] == 20 and "price_request_budget_reached" in first["pricing"]["issues"]
    assert not c.get("/api/rankings").json()["items"]
    assert c.post(f"/api/wallets/{WALLET}/price").json()["unpriced"] == 0
    ranking = c.get("/api/rankings").json()
    assert ranking["total"] == 1
    [item] = ranking["items"]
    assert item["address"] == WALLET and item["score"]["verified_smart_money"] is not None
    assert item["metrics"]["evaluable_positions"] == 30 and item["metrics"]["early_big_winners"] == 1
    analysis = c.get(f"/api/wallets/{WALLET}/analysis").json()
    assert analysis["eligible"] and analysis["evidence_level"] == "verified"


def test_coinbase_adapter_parameters_parsing_and_errors():
    seen = {}

    def transport(request):
        seen.update(path=request.url.path, params=dict(request.url.params))
        rows = [[1800000060, 149.5, 151.25, 150.0, 150.75, 10.0],
                [1800000000, 148.0, 150.0, 149.0, 150.0, 12.0],
                [1799999940, 1, 1, 1, 1, 1]]  # outside the window: discarded
        return httpx.Response(200, text=json.dumps(rows))

    prices = CoinbaseCandles(httpx.Client(transport=httpx.MockTransport(transport)))
    candles = prices.minute_candles(1800000000, 1800000120)
    assert seen["path"] == "/products/SOL-USD/candles" and seen["params"]["granularity"] == "60"
    assert [c["minute"] for c in candles] == [1800000060, 1800000000]
    assert candles[0]["close"] == Decimal("150.75") and isinstance(candles[0]["low"], Decimal)
    with pytest.raises(ValueError):
        prices.minute_candles(0, (MAX_CANDLES + 1) * 60)
    failing = CoinbaseCandles(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with pytest.raises(ProviderError):
        failing.minute_candles(1800000000, 1800000060)


def test_price_source_failure_keeps_the_sync_result(client):  # noqa: F811
    c, rpc = client

    def down(start, end):
        raise ProviderError("Synthetic price source unavailable", 503)

    c.app.state.prices.minute_candles = down
    serve(rpc, [pump_buy(block_time=int(time.time()) - 600)])
    report = c.post(SYNC, json={}).json()
    assert report["state"] == "scanned" and report["trades"] == 1
    assert report["pricing"] == {"error": "Synthetic price source unavailable"}


def test_a_sol_refund_set_aside_lowers_the_fees(client):  # noqa: F811
    c, rpc = client
    serve(rpc, [relayed_usdc_sell(1_488_440, block_time=int(time.time()) - 600)])
    c.post(SYNC, json={})
    [sell] = trades(c)
    assert sell["other_sol"] == "-0.00148844" and sell["evidence"]["usd"] == "verified"
    assert sell["usd"]["gross_usd"] == "21.575044" and Decimal(sell["usd"]["fees_usd"]) == Decimal("-0.00148844") * 150
