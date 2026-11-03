"""Copy-trading backtests: historical reconstruction and prospective tracking (SYNTHETIC data)."""

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import update

from tracker.adapters.solana import ProviderError
from tracker.db import AlertQuote
from tracker.watch import Watcher

from .conftest import WALLET, b58
from .test_alerts import arrive
from .test_discovery import curve_trade
from .test_incremental_sync import client  # noqa: F401  (fixture)
from .test_normalize import pump_buy, pump_sell

LEADER, MARKET = b58(5801), b58(5802)
MINT = b58(5803)
T0 = int(time.time()) - 7200
SLIP = Decimal("0.01")
SIZE, TX = Decimal("0.5"), Decimal("0.0001")


def trade(c, rpc, wallet, kind, sol, tokens, at, n):
    arrive(rpc, curve_trade(wallet, MINT, kind, sol, tokens, at, (at + n) * 2), owner=wallet, n=n)


def scenario(c, rpc):
    """Leader buys at 1e-7 SOL/token and sells at 4e-7; the market trades in between and after."""
    for wallet, label in ((LEADER, "LEADER"), (MARKET, "MARKET")):
        c.post("/api/wallets", json={"address": wallet, "label": label})
    trade(c, rpc, LEADER, "buy", 1, 10_000_000, T0, 1)            # 1e-7
    trade(c, rpc, MARKET, "buy", 1, 8_000_000, T0 + 10, 2)        # 1.25e-7
    trade(c, rpc, MARKET, "buy", 1, 5_000_000, T0 + 40, 3)        # 2e-7
    trade(c, rpc, LEADER, "sell", 4, 10_000_000, T0 + 600, 4)     # 4e-7
    trade(c, rpc, MARKET, "sell", Decimal("4.55"), 13_000_000, T0 + 620, 5)  # 3.5e-7
    for wallet in (LEADER, MARKET):
        c.post(f"/api/wallets/{wallet}/sync", json={})


def run(c, **body):
    r = c.post("/api/backtest/historical", json={"wallets": [LEADER], **body})
    assert r.status_code == 200, r.text
    return {x["delay_seconds"]: x for x in r.json()["results"]}, r.json()


def close(a, b):
    return abs(Decimal(a) - Decimal(b)) < Decimal("1e-18")


def roi(buy_price, sell_price):
    qty = SIZE / (buy_price * (1 + SLIP))
    proceeds = qty * sell_price * (1 - SLIP) - TX
    return (proceeds - (SIZE + TX)) / (SIZE + TX) * 100


def test_fills_use_the_first_price_observed_after_the_delay(client):  # noqa: F811
    c, rpc = client
    scenario(c, rpc)
    results, payload = run(c, delays=[0, 5, 30, 300])
    assert payload["leader_trades"] == 2 and payload["observations"] == 5
    ideal, five, thirty, late = results[0], results[5], results[30], results[300]
    # Delay 0 is the leader's own prices (an unreachable ideal).
    assert close(ideal["roi_pct"], roi(Decimal("1e-7"), Decimal("4e-7")))
    # 5 s: buys at the market's 1.25e-7 (t+10), sells at the market's 3.5e-7 (t+620).
    assert close(five["roi_pct"], roi(Decimal("1.25e-7"), Decimal("3.5e-7")))
    assert Decimal(five["leader_roi_pct"]) > Decimal(five["roi_pct"]) and Decimal(five["copy_gap_pct"]) < 0
    # 30 s: buys at 2e-7 (t+40); nothing is observed after t+630, so the exit is unfillable and the
    # position stays open, marked at the last observed price.
    assert thirty["unfillable_sells"] == 1 and thirty["closed_positions"] == 0 and thirty["open_positions"] == 1
    held = SIZE / (Decimal("2e-7") * (1 + SLIP))
    assert close(thirty["open_mark_value_sol"], held * Decimal("3.5e-7"))
    # 300 s: the first price after t+300 is the leader's own exit at the top (4e-7).
    assert late["filled_buys"] == 1 and late["positions"] == [] and late["open_positions"] == 1


def test_no_price_within_the_staleness_window_means_no_trade(client):  # noqa: F811
    c, rpc = client
    scenario(c, rpc)
    results, _ = run(c, delays=[5], max_staleness_seconds=2)  # next observation is 5 s after t+5
    assert results[5]["unfillable_buys"] == 1 and results[5]["filled_buys"] == 0
    assert results[5]["roi_pct"] is None


def test_partial_sells_are_mirrored_proportionally(client):  # noqa: F811
    c, rpc = client
    for wallet in (LEADER, MARKET):
        c.post("/api/wallets", json={"address": wallet})
    trade(c, rpc, LEADER, "buy", 1, 10_000_000, T0, 1)
    trade(c, rpc, MARKET, "buy", 1, 10_000_000, T0 + 5, 2)
    trade(c, rpc, LEADER, "sell", 1, 5_000_000, T0 + 100, 3)   # half of the leader's position
    trade(c, rpc, MARKET, "buy", 1, 5_000_000, T0 + 105, 4)
    trade(c, rpc, LEADER, "sell", 2, 5_000_000, T0 + 200, 5)   # the rest
    trade(c, rpc, MARKET, "buy", 1, 2_000_000, T0 + 205, 6)
    for wallet in (LEADER, MARKET):
        c.post(f"/api/wallets/{wallet}/sync", json={})
    results, _ = run(c, delays=[5])
    [position] = results[5]["positions"]
    sells = [f for f in position["fills"] if f["kind"] == "sell"]
    assert [Decimal(f["fraction"]) for f in sells] == [Decimal("0.5"), Decimal(1)]
    assert results[5]["mirrored_sells"] == 2 and results[5]["closed_positions"] == 1


def test_request_validation(client):  # noqa: F811
    c, _ = client
    assert c.post("/api/backtest/historical", json={"wallets": ["nope"]}).status_code == 422
    assert c.post("/api/backtest/historical", json={"delays": [-1]}).status_code == 422
    assert c.post("/api/backtest/historical", json={"size_sol": "0"}).status_code == 422
    # No wallets selected: the watchlist, else every tracked wallet (the fixture tracks one).
    assert c.post("/api/backtest/historical", json={}).json()["wallets"] == [WALLET]


def test_prospective_tracking_records_quotes_at_and_after_alerts(client):  # noqa: F811
    c, rpc = client
    quotes = c.app.state.quotes
    c.patch(f"/api/wallets/{WALLET}", json={"favorite": True})
    c.post(f"/api/wallets/{WALLET}/sync", json={})
    mint = b58(5900)
    arrive(rpc, pump_buy(mint=mint, block_time=int(time.time()) - 20), n=11)

    def quote(price):
        quotes.data[mint] = {"price_usd": Decimal(price), "liquidity_usd": Decimal(20000), "market_cap_usd": None,
                             "fdv_usd": None, "pair_address": "p", "dex_id": "pumpswap", "symbol": "SYN"}

    quote("0.00010")
    watcher = Watcher(c.app.state.sessions, rpc, c.app.state.prices, quotes=quotes, interval=20,
                      watcher_id="watcher:test")
    report = watcher.run_once()
    assert report["alerts"] == 1 and report["quotes"] == 1  # the quote at the alert itself
    with c.app.state.sessions() as s:
        rows = sorted(s.query(AlertQuote).all(), key=lambda q: q.offset_seconds)
        assert [(q.offset_seconds, q.status) for q in rows] == [(0, "done"), (300, "pending"), (3600, "pending")]
        # Time passes: make the later quotes due, at new market prices.
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        s.execute(update(AlertQuote).where(AlertQuote.offset_seconds > 0).values(due_at=past))
        s.commit()
    quote("0.00015")
    assert watcher.run_once()["quotes"] == 2
    r = c.get("/api/backtest/prospective").json()
    entry = Decimal("0.00010") * (1 + SLIP)
    expected = (Decimal("0.00015") * (1 - SLIP) / entry - 1) * 100
    assert r["buy_alerts_with_entry_quote"] == 1
    assert close(r["horizons"]["5m"]["median_pct"], expected) and r["horizons"]["60m"]["hit_rate"] == 1
    assert r["wallets"][0]["wallet"] == WALLET and r["horizons"]["exit_on_leader_sell"]["n"] == 0
    # The leader sells: the follower exits at the quote recorded with that sell alert.
    arrive(rpc, pump_sell(mint=mint, block_time=int(time.time()) - 5), n=21)
    quote("0.00030")
    watcher.run_once()
    exit_return = c.get("/api/backtest/prospective").json()["horizons"]["exit_on_leader_sell"]
    assert exit_return["n"] == 1
    assert close(exit_return["median_pct"], (Decimal("0.00030") * (1 - SLIP) / entry - 1) * 100)


def test_quote_capture_handles_missing_pairs_and_a_failing_source(client):  # noqa: F811
    c, rpc = client
    quotes = c.app.state.quotes
    c.patch(f"/api/wallets/{WALLET}", json={"favorite": True})
    c.post(f"/api/wallets/{WALLET}/sync", json={})
    arrive(rpc, pump_buy(mint=b58(5901), block_time=int(time.time()) - 20), n=12)
    watcher = Watcher(c.app.state.sessions, rpc, c.app.state.prices, quotes=quotes, interval=20,
                      watcher_id="watcher:test")
    original = quotes.quotes
    quotes.quotes = lambda mints: (_ for _ in ()).throw(ProviderError("Synthetic quotes down", 503))
    for _ in range(5):
        watcher.run_once()
    with c.app.state.sessions() as s:
        first = s.query(AlertQuote).filter(AlertQuote.offset_seconds == 0).one()
        assert first.status == "failed" and first.attempts == 5
    quotes.quotes = original
    arrive(rpc, pump_buy(mint=b58(5902), block_time=int(time.time()) - 10), n=13)
    watcher.run_once()  # no pair for this mint in the synthetic source
    with c.app.state.sessions() as s:
        statuses = {q.status for q in s.query(AlertQuote).filter(AlertQuote.mint == b58(5902),
                                                                  AlertQuote.offset_seconds == 0)}
        assert statuses == {"missing"}
