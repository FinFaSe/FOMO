"""SYNTHETIC accounting fixtures, never loaded into a runtime database."""

from datetime import timedelta
from decimal import Decimal

import pytest

from tracker.analytics import analyze, reconstruct, wilson
from tracker.schemas import Filters

from .conftest import AS_OF, TOKEN, WALLET, b58


def test_multiple_entries_partial_exit_and_rebuy_cost_basis(fill):
    events = [fill("buy", 10, 100, fees=2), fill("buy", 10, 200, fees=2),
              fill("sell", 5, 100, fees=1), fill("buy", 5, 125, fees=1),
              fill("sell", 20, 800, fees=3), fill("buy", 4, 40)]
    positions = reconstruct(events, AS_OF)
    first, reopened = positions
    # 304 initial basis, 76 allocated on first sale, then 126 added to 228 = 354.
    assert first.allocated_cost == Decimal(430)
    assert first.proceeds == Decimal(896)
    assert first.pnl == Decimal(466)
    assert first.quantity == 0
    assert first.evaluable
    assert reopened.basis == 40
    assert reopened.closed_at is None


def test_open_partial_sale_not_an_independent_win(fill):
    events = [fill("buy", 10, 100), fill("sell", 1, 100)]
    r = analyze(WALLET, events, Filters(), AS_OF)
    assert r["metrics"]["evaluable_positions"] == 0
    assert r["metrics"]["open_positions"] == 1
    assert r["positions"][0]["realized_pnl_usd"] == "90"
    assert r["positions"][0]["remaining_cost_usd"] == "90"
    assert r["metrics"]["win_rate"] is None


@pytest.mark.parametrize("kind", ["transfer_in", "transfer_out"])
def test_transfers_are_not_sales_or_free_cost_inventory(fill, kind):
    events = [fill("buy", 10, 100)]
    if kind == "transfer_in":
        events.extend([fill(kind, 5, None), fill("sell", 15, 1000)])
    else:
        events.append(fill(kind, 10, None))
    p = reconstruct(events, AS_OF)[0]
    assert not p.evaluable
    assert "transfer_unknown_cost_or_disposal" in p.issues


@pytest.mark.parametrize("missing", ["gross", "fee"])
def test_missing_historical_price_or_fee_remains_unknown(fill, missing):
    p = reconstruct([fill("buy", 10, None if missing == "gross" else 100,
                          fees=None if missing == "fee" else 0), fill("sell", 10, 900)], AS_OF)[0]
    assert p.pnl is None
    assert not p.evaluable


def test_sell_without_opening_inventory_is_excluded(fill):
    p = reconstruct([fill("sell", 10, 600)], AS_OF)[0]
    assert not p.evaluable
    assert "missing_opening_inventory" in p.issues


def test_500_percent_is_six_times_after_all_fees(fill):
    events = [fill("buy", 1, 99, fees=1), fill("sell", 1, 601, fees=1)]
    r = analyze(WALLET, events, Filters(), AS_OF, True)
    assert r["metrics"]["big_winners"] == 1
    assert r["positions"][0]["realized_roi_pct"] == "500"


def test_market_cap_growth_not_used_as_trader_roi(fill):
    r = analyze(WALLET, [fill("buy", 1, 100, cap="1000"),
                        fill("sell", 1, 80, cap="10000000")], Filters(), AS_OF)
    assert r["metrics"]["big_winners"] == 0
    assert r["metrics"]["net_realized_pnl_usd"] == "-20"


def test_dead_token_with_realized_total_loss_is_not_dropped(fill):
    r = analyze(WALLET, [fill("buy", 1, 100), fill("sell", 1, 0)], Filters(), AS_OF)
    assert r["metrics"]["evaluable_positions"] == 1
    assert r["metrics"]["losses"] == 1
    assert r["metrics"]["median_roi_pct"] == "-100"


def test_cannot_infer_a_writeoff_from_low_liquidity(fill):
    r = analyze(WALLET, [fill("buy", 1, 100)], Filters(), AS_OF)
    assert r["metrics"]["net_realized_pnl_usd"] is None
    assert r["positions"][0]["unrealized_pnl_usd"] is None


def test_evidence_mismatch_excludes_position(fill):
    events = [fill("buy", 1, 100, evidence={"movement": "mismatch"}), fill("sell", 1, 600)]
    assert not reconstruct(events, AS_OF)[0].evaluable


def test_same_timestamp_order_is_not_invented(fill):
    a, b = fill("buy", 1, 100), fill("sell", 1, 600)
    b.timestamp = a.timestamp
    assert "ambiguous_transaction_order" in reconstruct([b, a], AS_OF)[0].issues


def test_window_uses_earlier_inventory_but_not_future_sales(fill):
    buy = fill("buy", 1, 100, days_ago=120)
    sell = fill("sell", 1, 700, days_ago=10)
    future = fill("sell", 1, 10000, days_ago=-1)
    r = analyze(WALLET, [buy, sell, future], Filters(), AS_OF)
    assert r["metrics"]["net_realized_pnl_usd"] == "600"
    assert len(r["positions"]) == 1
    earlier = analyze(WALLET, [buy, sell], Filters(), AS_OF - timedelta(days=20))
    assert earlier["metrics"]["evaluable_positions"] == 0


def test_wilson_adjustment_and_effective_token_sample(fill):
    events = []
    for i in range(30):
        events.extend([fill("buy", 1, 100), fill("sell", 1, 700)])
    r = analyze(WALLET, events, Filters(), AS_OF, True)
    assert r["metrics"]["win_rate"] == 1
    assert r["metrics"]["effective_sample_tokens"] == 1
    assert r["metrics"]["win_rate_ci95"][0] < 0.3
    assert not r["eligible"]
    low, high = wilson(18, 30)
    assert 0.42 < low < 0.43
    assert 0.75 < high < 0.76


def test_qualified_synthetic_history_and_exact_thresholds(fill):
    events = []
    for i in range(30):
        token = b58(i + 1000)
        events.extend([fill("buy", 1, 100, token=token, cap="250000"),
                       fill("sell", 1, 600 if i < 27 else 50, token=token)])
    r = analyze(WALLET, events, Filters(), AS_OF, True)
    assert r["eligible"]
    assert r["metrics"]["evaluable_positions"] == 30
    assert r["score"]["verified_smart_money"] <= 100
    assert r["score"]["components"]["replicability"] == 0
    assert not analyze(WALLET, events, Filters(), AS_OF, False)["eligible"]


def test_any_late_buy_or_unverified_historical_cap_blocks_early_claim(fill):
    events = [fill("buy", 1, 10, cap="5000"), fill("buy", 1, 90, cap="250001"),
              fill("sell", 2, 900)]
    r = analyze(WALLET, events, Filters(), AS_OF)
    assert r["metrics"]["early_big_winners"] == 0
    events[1].market_cap_usd = None
    assert not reconstruct(events, AS_OF)[0].cap_verified


def test_reported_values_never_promote_to_verified(fill):
    events = [fill("buy", 1, 100, evidence={"usd": "reported"}), fill("sell", 1, 700)]
    r = analyze(WALLET, events, Filters(min_evaluable_positions=1), AS_OF, True)
    assert not r["eligible"]
    assert r["score"]["verified_smart_money"] is None


def test_missing_costs_cannot_hide_a_loser_from_an_otherwise_qualified_history(fill):
    events = []
    for i in range(30):
        token = b58(i + 1000)
        events.extend([fill("buy", 1, 100, token=token), fill("sell", 1, 600, token=token)])
    unknown = b58(9000)
    events.extend([fill("buy", 1, None, token=unknown), fill("sell", 1, 0, token=unknown)])
    r = analyze(WALLET, events, Filters(), AS_OF, True)
    assert r["metrics"]["evaluable_positions"] == 30
    assert r["metrics"]["excluded_positions"] == 1
    assert not r["eligible"]
    assert "incomplete_position_coverage" in r["exclusion_reasons"]


def test_snapshot_is_deterministic_and_includes_provenance(fill):
    events = [fill("buy", 1, 100), fill("sell", 1, 700)]
    a = analyze(WALLET, events, Filters(), AS_OF)
    b = analyze(WALLET, list(reversed(events)), Filters(), AS_OF)
    assert a["snapshot_id"] == b["snapshot_id"]
    events[0].source_url = "https://example.org/other-evidence"
    assert analyze(WALLET, events, Filters(), AS_OF)["snapshot_id"] != a["snapshot_id"]


def test_concentration_penalty_and_zero_sample(fill):
    assert wilson(0, 0) == (None, None)
    empty = analyze(WALLET, [], Filters(), AS_OF)
    assert empty["score"]["verified_smart_money"] is None
    assert empty["metrics"]["win_rate"] is None
    a = fill("buy", 1, 100)
    b = fill("sell", 1, 10000)
    one = analyze(WALLET, [a, b], Filters(), AS_OF)
    assert one["score"]["concentration_penalty_fraction"] == 0.5


def test_token_aggregation_is_conservative_for_repeated_cycles(fill):
    events = [fill("buy", 1, 100, token=TOKEN), fill("sell", 1, 101, token=TOKEN),
              fill("buy", 1, 1000, token=TOKEN), fill("sell", 1, 0, token=TOKEN)]
    m = analyze(WALLET, events, Filters(), AS_OF)["metrics"]
    assert m["win_rate"] == 0.5
    assert m["token_cluster_win_rate"] == 0
