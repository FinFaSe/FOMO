"""Price history: on-chain price points and pool candles (SYNTHETIC sources, no network)."""

import json
import time
from decimal import Decimal

import httpx

from tracker.adapters.geckoterminal import WSOL, GeckoTerminal
from tracker.db import PoolCandle, PriceHistory, PricePoint
from tracker.history import enqueue_history, fetch_history
from tracker.jobs import Worker

from .conftest import b58
from .test_backtest import LEADER, MARKET, MINT, T0, trade
from .test_discovery import ALICE, BOB, add_and_scan, winner
from .test_discovery import T0 as LAUNCH
from .test_incremental_sync import client  # noqa: F401  (fixture)

POOL = b58(6301)


class SyntheticHistory:
    """SYNTHETIC GeckoTerminal: one SOL pool per mint and candles served newest first in pages."""

    source = "https://synthetic-ohlcv.example.org"

    def __init__(self, page=1000):
        self.series, self.page, self.calls, self.pools = {}, page, [], {}

    def best_pool(self, mint):
        self.calls.append(("pools", mint))
        return self.pools.get(mint, {"pool": POOL, "quote_mint": WSOL, "dex": "pumpswap"})

    def candles(self, pool, timeframe, before, limit=1000):
        self.calls.append(("candles", timeframe, before))
        rows = sorted((c for c in self.series.get((pool, timeframe), []) if c["ts"] < before), key=lambda c: c["ts"])
        return rows[-min(limit, self.page):]


def candle(ts, low, high, close=None):
    low, high = Decimal(low), Decimal(high)
    return {"ts": ts, "open": low, "high": high, "low": low, "close": Decimal(close) if close else high, "volume": None}


def test_geckoterminal_adapter_picks_the_sol_pool_and_parses_candles():
    mint = b58(6302)
    seen = []

    def transport(request):
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/pools"):
            pools = [{"attributes": {"address": "usdcpool", "reserve_in_usd": "900000"},
                      "relationships": {"base_token": {"data": {"id": f"solana_{mint}"}},
                                        "quote_token": {"data": {"id": "solana_EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}},
                                        "dex": {"data": {"id": "raydium"}}}},
                     {"attributes": {"address": "solpool", "reserve_in_usd": "50000"},
                      "relationships": {"base_token": {"data": {"id": f"solana_{mint}"}},
                                        "quote_token": {"data": {"id": f"solana_{WSOL}"}},
                                        "dex": {"data": {"id": "pumpswap"}}}},
                     {"attributes": {"address": "other", "reserve_in_usd": "9999999"},
                      "relationships": {"base_token": {"data": {"id": "solana_X"}}}}]
            return httpx.Response(200, text=json.dumps({"data": pools}))
        rows = [[1_800_000_120, "1", "3", "0.5", "2", "10"], [1_800_000_060, 1, 2, 1, 2, 5],
                [1_800_000_061, 1, 1, 1, 1, 1], [1_800_000_200, 1, 1, 1, 1, 1], ["bad"]]
        return httpx.Response(200, text=json.dumps({"data": {"attributes": {"ohlcv_list": rows}}}))

    gt = GeckoTerminal(httpx.Client(transport=httpx.MockTransport(transport)), spacing=0)
    pool = gt.best_pool(mint)
    assert pool["pool"] == "solpool" and pool["quote_mint"] == WSOL and pool["dex"] == "pumpswap"
    candles = gt.candles("solpool", "minute", 1_800_000_180)
    assert [c["ts"] for c in candles] == [1_800_000_060, 1_800_000_120]  # sorted; misaligned/late/bad dropped
    assert candles[1]["high"] == Decimal(3) and isinstance(candles[1]["low"], Decimal)
    path, params = seen[-1]
    assert path.endswith("/pools/solpool/ohlcv/minute") and params["currency"] == "token"
    assert params["before_timestamp"] == "1800000180"


def test_history_download_resumes_and_chooses_the_timeframe(client):  # noqa: F811
    c, _ = client
    now = int(time.time()) // 60 * 60
    source = SyntheticHistory(page=2)
    source.series[(POOL, "minute")] = [candle(now - 600 + i * 60, "1", "2") for i in range(10)]
    with c.app.state.sessions() as s:
        row = enqueue_history(s, MINT, now - 600, now, "test")
        assert row.timeframe == "minute" and enqueue_history(s, MINT, now - 300, now - 60, "test").id == row.id
        assert enqueue_history(s, MINT, now - 10 * 86400, now, "test").timeframe == "hour"
        assert enqueue_history(s, MINT, now - 400 * 86400, now - 300 * 86400, "test") is None  # beyond free history
        fetch_history(s, source, row, max_requests=2)
        assert row.state == "queued" and row.candles == 4  # budget spent: resumes from the cursor
        fetch_history(s, source, row, max_requests=10)
        assert row.state == "done" and row.candles == 10 and row.pool == POOL
        assert s.query(PoolCandle).count() == 10
    source.pools[b58(6303)] = None
    with c.app.state.sessions() as s:
        missing = enqueue_history(s, b58(6303), now - 600, now, "test")
        assert fetch_history(s, source, missing).state == "failed" and missing.error == "no_pool_found"


def test_discovery_keeps_every_traders_prices_and_queues_candles(client):  # noqa: F811
    c, rpc = client
    mint = b58(6401)
    winner(rpc, mint)
    add_and_scan(c, mint)
    with c.app.state.sessions() as s:
        points = s.query(PricePoint).filter(PricePoint.mint == mint).all()
        assert {p.side for p in points} == {"buy", "sell"} and len(points) == 5  # Alice's sell included
        histories = sorted((h.timeframe, h.requested_by) for h in s.query(PriceHistory).filter(PriceHistory.mint == mint))
        assert histories == [("hour", "discovery"), ("minute", "discovery")]


def test_pool_candles_set_the_seed_peak(client):  # noqa: F811
    c, rpc = client
    mint = b58(6402)
    winner(rpc, mint)
    add_and_scan(c, mint)
    # The token ran far above anything in the scanned trades: an hourly candle high of 1e-5 SOL per token.
    source = SyntheticHistory()
    hour = LAUNCH // 3600 * 3600
    source.series[(POOL, "hour")] = [candle(hour + 3600 * 5, "1e-6", "1e-5")]
    with c.app.state.sessions() as s:
        row = s.query(PriceHistory).filter(PriceHistory.mint == mint, PriceHistory.timeframe == "hour").one()
        fetch_history(s, source, row, c.app.state.prices)
    seed = next(x for x in c.get("/api/discovery/seeds").json()["items"] if x["mint"] == mint)
    outcome = seed["outcome"]
    assert outcome["peak_basis"] == "pool_ohlcv" and outcome["pool_peak_cap_status"] == "verified"
    assert Decimal(outcome["peak_cap_lower_bound_usd"]) == Decimal("1e-5") * 150 * 10 ** 9  # SOL $150, 1e9 supply
    # Candidate hits now use the real peak: Bob ($15k entry) reaches 10× too.
    bob = next(i for i in c.get("/api/discovery/candidates").json()["items"] if i["wallet"] == BOB)
    assert bob["hits"] == 1 and ALICE in {i["wallet"] for i in c.get("/api/discovery/candidates").json()["items"]}


def test_backtest_uses_exact_chain_prices_then_pessimistic_candles(client):  # noqa: F811
    c, rpc = client
    for wallet in (LEADER, MARKET):
        c.post("/api/wallets", json={"address": wallet})
    trade(c, rpc, LEADER, "buy", 1, 10_000_000, T0, 1)          # 1e-7
    trade(c, rpc, MARKET, "buy", 1, 8_000_000, T0 + 10, 2)      # 1.25e-7: exact for a 5 s delay
    trade(c, rpc, LEADER, "sell", 4, 10_000_000, T0 + 3600, 3)  # 4e-7
    for wallet in (LEADER, MARKET):
        c.post(f"/api/wallets/{wallet}/sync", json={})
    minute = (T0 + 120) // 60 * 60
    with c.app.state.sessions() as s:
        for ts, low, high in ((minute, "1.4e-7", "1.6e-7"), ((T0 + 3605) // 60 * 60, "3.1e-7", "3.5e-7"),
                              ((T0 + 3720) // 60 * 60, "3.0e-7", "3.8e-7")):
            s.merge(PoolCandle(pool=POOL, timeframe="minute", ts=ts, mint=MINT, quote_mint=WSOL, open=low, high=high,
                               low=low, close=high, source="synthetic"))
        s.commit()
    body = {"wallets": [LEADER], "delays": [5, 120]}
    results = {r["delay_seconds"]: r for r in c.post("/api/backtest/historical", json=body).json()["results"]}
    five, late = results[5], results[120]
    buy5 = next(f for f in five["positions"][0]["fills"] if f["kind"] == "buy")
    assert buy5["source"] == "chain" and Decimal(buy5["price_sol"]) == Decimal("1.25e-7") * Decimal("1.01")
    # 120 s: no on-chain price within a minute; the candle covering t+120 fills at its high (buy) and,
    # for the exit at t+3720, at its low (sell).
    buy, sell = late["positions"][0]["fills"]
    assert (buy["source"], sell["source"]) == ("candle", "candle")
    assert Decimal(buy["price_sol"]) == Decimal("1.6e-7") * Decimal("1.01") and Decimal(sell["price_sol"]) == Decimal("3.0e-7")
    assert late["fills_candle"] == 2 and five["fills_chain"] >= 1
    off = {r["delay_seconds"]: r for r in c.post("/api/backtest/historical",
                                                   json={**body, "use_candles": False}).json()["results"]}
    assert off[120]["fills_candle"] == 0 and off[120]["filled_buys"] == 0  # nothing within 5 min on-chain


def test_worker_downloads_history_last_and_chart_endpoint(client):  # noqa: F811
    c, rpc = client
    for wallet in (LEADER, MARKET):
        c.post("/api/wallets", json={"address": wallet})
    trade(c, rpc, LEADER, "buy", 1, 10_000_000, T0, 1)
    trade(c, rpc, LEADER, "sell", 4, 10_000_000, T0 + 600, 2)
    c.post(f"/api/wallets/{LEADER}/sync", json={})
    queued = c.post("/api/backtest/prices", json={"wallets": [LEADER]}).json()
    assert queued["mints"] == 1 and queued["requests"][0]["timeframe"] == "minute"
    source = SyntheticHistory()
    source.series[(POOL, "minute")] = [candle((T0 // 60 + i) * 60, "1e-7", "2e-7") for i in range(20)]
    w = Worker(c.app.state.sessions, rpc, c.app.state.prices, history=source, lookup_funding=False,
               worker_id="test-worker", refresh_minutes=0, progress_interval=0)
    ran = w.run_once()
    assert type(ran).__name__ == "PriceHistory" and ran.state == "done"
    data = c.get(f"/api/prices/{MINT}/chart", params={"wallet": LEADER}).json()
    assert data["timeframe"] == "minute" and data["quote"] == "SOL" and len(data["candles"]) == 20
    assert [t["kind"] for t in data["trades"]] == ["buy", "sell"] and data["histories"][0]["state"] == "done"
    assert len(data["points"]) == 2  # on-chain prices: the tracked trades of this mint
    assert c.get("/api/prices/nope/chart").status_code == 422
    assert c.post(f"/api/prices/{MINT}/history", params={"from": 10, "to": 5}).status_code == 422
