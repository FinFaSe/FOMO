"""Precomputed ranking entries: invalidation by input revision, worker refresh and snapshots. SYNTHETIC data."""

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select, update

from tracker import ranking as ranking_module
from tracker.db import AnalysisSnapshot, Observation, RankingEntry, Trade, Wallet
from tracker.jobs import Worker
from tracker.ranking import DEFAULT_FILTERS, DEFAULT_KEY, _upsert, filters_key, prune
from tracker.schemas import Filters
from tracker.service import renormalize

from .conftest import WALLET, b58
from .test_chain_trades import serve
from .test_incremental_sync import client  # noqa: F401  (fixture)
from .test_normalize import SOL, pump_buy, pump_sell

SYNC = f"/api/wallets/{WALLET}/sync"
DAY = 86400


def thirty_winners(rpc):
    """The SYNTHETIC history of test_pricing: 30 closed pump.fun positions, one of them an early 7× exit."""
    now, history = int(time.time()), []
    for i in range(30):
        mint, day = b58(7000 + i), now - (i + 1) * DAY
        exit_sol = 7 * SOL if i == 0 else 3 * SOL // 2
        history += [pump_buy(mint=mint, block_time=day - 3600), pump_sell(mint=mint, sol=exit_sol, block_time=day)]
    serve(rpc, sorted(history, key=lambda t: t["blockTime"]))


def eligible_wallet(c, rpc):
    thirty_winners(rpc)
    c.post(SYNC, json={"max_pages": 1, "page_size": 100})
    assert c.post(f"/api/wallets/{WALLET}/price").json()["unpriced"] == 0


def revision(c, wallet=WALLET):
    with c.app.state.sessions() as s:
        return s.scalar(select(Wallet.analysis_revision).where(Wallet.address == wallet))


def entry(c, key=DEFAULT_KEY, wallet=WALLET):
    with c.app.state.sessions() as s:
        return s.execute(select(RankingEntry.__table__).where(RankingEntry.wallet == wallet,
                                                             RankingEntry.filters_key == key)).first()


def snapshots(c):
    with c.app.state.sessions() as s:
        return s.scalar(select(func.count()).select_from(AnalysisSnapshot))


def count_analyses(monkeypatch):
    calls = []
    original = ranking_module.wallet_analysis

    def spy(session, wallet, *args, **kwargs):
        calls.append(wallet.address)
        return original(session, wallet, *args, **kwargs)

    monkeypatch.setattr(ranking_module, "wallet_analysis", spy)
    return calls


def test_writes_to_analysis_inputs_bump_the_wallet_revision(client):  # noqa: F811
    c, rpc = client
    start = revision(c)
    serve(rpc, [pump_buy(block_time=int(time.time()) - 600)])
    c.post(SYNC, json={})
    after_sync = revision(c)
    assert after_sync > start  # trades and sync state
    c.patch(f"/api/wallets/{WALLET}", json={"favorite": True})
    assert revision(c) == after_sync  # the watchlist flag is not an analysis input
    with c.app.state.sessions() as s:  # pricing rewrites trade details in place
        t = s.scalars(select(Trade)).first()
        t.details = {**t.details, "issues": ["synthetic_change"]}
        s.commit()
    assert revision(c) == after_sync + 1
    # A bulk delete that re-creates nothing still invalidates: without observations, renormalize only deletes.
    with c.app.state.sessions() as s:
        s.execute(Observation.__table__.delete())
        s.commit()
        assert revision(c) == after_sync + 1  # Core statements bypass the hook (hence the explicit bump)
        assert renormalize(s, WALLET)["trades"] == 0
    assert revision(c) == after_sync + 2


def test_sync_precomputes_and_the_ranking_is_served_without_recomputing(client, monkeypatch):  # noqa: F811
    c, rpc = client
    eligible_wallet(c, rpc)
    first = c.get("/api/rankings").json()  # pricing changed the trades after the sync: recomputed once
    assert first["total"] == 1 and first["precomputed"]["recomputed_now"] == 1
    calls = count_analyses(monkeypatch)
    again = c.get("/api/rankings").json()
    assert calls == [] and again["precomputed"]["recomputed_now"] == 0
    assert again["items"] == first["items"]
    [item] = again["items"]
    assert item["score"]["verified_smart_money"] is not None and item["metrics"]["evaluable_positions"] == 30
    # The precomputed result is exactly a fresh computation as of the same moment.
    fresh = c.get(f"/api/wallets/{WALLET}/analysis",
                  params={"as_of": item["as_of"].replace("+00:00", "Z")}).json()
    assert fresh["snapshot_id"] == item["snapshot_id"]
    # Every snapshot id the ranking returns resolves, even after the entry is replaced.
    assert c.get(f"/api/snapshots/{item['snapshot_id']}").json()["eligible"]
    c.post(SYNC, json={})  # new as_of: the entry is replaced by the sync itself
    assert calls == [WALLET] and c.get(f"/api/snapshots/{item['snapshot_id']}").status_code == 200
    assert c.get("/api/rankings").json()["precomputed"]["recomputed_now"] == 0


def test_default_analysis_is_served_from_the_entry(client, monkeypatch):  # noqa: F811
    c, rpc = client
    serve(rpc, [pump_buy(block_time=int(time.time()) - 7200), pump_sell(block_time=int(time.time()) - 3600)])
    report = c.post(SYNC, json={}).json()
    assert report["ranking"] == {"eligible": False, "score": None}
    calls = count_analyses(monkeypatch)
    analysis = c.get(f"/api/wallets/{WALLET}/analysis").json()
    assert calls == [] and analysis["snapshot_id"] == entry(c).snapshot_id
    assert c.get(f"/api/snapshots/{analysis['snapshot_id']}").json() == analysis
    # Other filters get their own entry, computed once.
    url = f"/api/wallets/{WALLET}/analysis?analysis_days=7"
    assert c.get(url).json()["filters"]["analysis_days"] == 7 and calls == [WALLET]
    c.get(url)
    assert calls == [WALLET]


def test_wallet_without_a_completed_sync_is_analyzed_as_of_now(client, monkeypatch):  # noqa: F811
    c, _ = client
    calls = count_analyses(monkeypatch)
    first, second = (c.get(f"/api/wallets/{WALLET}/analysis").json() for _ in range(2))
    assert len(calls) == 2 and entry(c) is None  # the window moves with "now": never served from an entry
    assert first["history_complete"] is False and second["history_complete"] is False


def test_method_change_and_new_wallets_are_recomputed(client, monkeypatch):  # noqa: F811
    c, rpc = client
    eligible_wallet(c, rpc)
    c.get("/api/rankings")
    other = b58(4242)
    c.post("/api/wallets", json={"address": other})  # no entry yet
    calls = count_analyses(monkeypatch)
    assert c.get("/api/rankings").json()["precomputed"]["recomputed_now"] == 1 and calls == [other]
    monkeypatch.setattr(ranking_module, "ENGINE", "synthetic-next-version")
    ranked = c.get("/api/rankings").json()
    assert ranked["precomputed"]["recomputed_now"] == 2 and ranked["total"] == 1
    assert entry(c).engine == "synthetic-next-version"


def test_worker_refreshes_stale_entries_without_creating_snapshots(client):  # noqa: F811
    c, rpc = client
    for i in range(3):
        c.post("/api/wallets", json={"address": b58(5000 + i)})
    assert c.get("/api/worker").json()["ranking"]["stale"] == 4
    before = snapshots(c)
    w = Worker(c.app.state.sessions, rpc, c.app.state.prices, worker_id="test-worker", refresh_minutes=0,
               lookup_funding=False)
    assert w.run_once() is None  # housekeeping only: no unit of work ran
    status = c.get("/api/worker").json()["ranking"]
    assert status == {"wallets": 4, "stale": 0, "engine": ranking_module.ENGINE}
    assert snapshots(c) == before  # entries nobody looked at do not pile up snapshots


def test_worker_refresh_is_sliced(client, monkeypatch):  # noqa: F811
    c, rpc = client
    c.post("/api/wallets", json={"address": b58(5100)})
    with c.app.state.sessions() as s:
        assert ranking_module.refresh_stale(s, budget_seconds=0) == {"refreshed": 1, "remaining": 1}
        assert ranking_module.refresh_stale(s, budget_seconds=0) == {"refreshed": 1, "remaining": 0}
        assert ranking_module.refresh_stale(s) == {"refreshed": 0, "remaining": 0}


def test_an_older_concurrent_refresh_never_overwrites_a_newer_one(client):  # noqa: F811
    c, _ = client
    c.get("/api/rankings")
    current = entry(c)
    values = {k: getattr(current, k) for k in current._fields}
    with c.app.state.sessions() as s:
        _upsert(s, {**values, "revision": current.revision + 2, "as_of": "newer"})
        _upsert(s, {**values, "revision": current.revision + 1, "as_of": "older"})
        s.commit()
    assert entry(c).as_of == "newer"


def test_equivalent_filters_share_an_entry_and_unused_sets_are_pruned(client):  # noqa: F811
    c, _ = client
    assert filters_key(Filters(max_entry_market_cap_usd=Decimal("250000.00"))) == DEFAULT_KEY
    assert filters_key(Filters(min_win_rate=0.7)) != DEFAULT_KEY
    c.get("/api/rankings")
    c.get("/api/rankings", params={"min_win_rate": 0.7})
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    with c.app.state.sessions() as s:
        s.execute(update(RankingEntry).values(computed_at=old))
        s.commit()
        assert prune(s) == 1  # the default set is always kept
        assert {r for r in s.scalars(select(RankingEntry.filters_key))} == {DEFAULT_KEY}
    assert entry(c, filters_key(DEFAULT_FILTERS)) is not None
