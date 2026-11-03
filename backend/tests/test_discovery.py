"""Backward discovery over SYNTHETIC bonding-curve histories, SYNTHETIC prices and SYNTHETIC quotes."""

import zlib
from decimal import Decimal

import pytest

from tracker import discovery as discovery_module
from tracker.jobs import Worker
from tracker.tokens import PUMP_PROGRAM, pump_bonding_curve

from .conftest import WALLET, b58
from .test_incremental_sync import client  # noqa: F401  (fixture)
from .test_normalize import PUMP, RAYDIUM, RENT, SOL, balance, tx

CREATOR, SNIPER, ALICE, BOB, CAROL = (b58(n) for n in (7101, 7102, 7103, 7104, 7105))
T0 = 1_790_000_000  # launch time (synthetic)
BIG = 800_000_000_000_000  # curve inventory, raw units (6 decimals)


def curve_trade(buyer, mint, kind, sol, tokens, block_time, slot, program=PUMP, fee=5000):
    """A SYNTHETIC curve buy or sell by `buyer`; tokens are whole tokens (6 decimals)."""
    raw, lamports = tokens * 10 ** 6, int(sol * SOL)
    ata, curve, curve_ata = b58(zlib.crc32(f"{buyer}:{mint}".encode()) + 10 ** 6), pump_bonding_curve(mint), b58(7999)
    if kind == "buy":
        pre, post = [10 * SOL, 0, 30 * SOL, RENT], [10 * SOL - lamports - RENT - fee, RENT, 30 * SOL + lamports, RENT]
        pre_t, post_t = [balance(3, mint, curve, BIG)], [balance(1, mint, buyer, raw), balance(3, mint, curve, BIG - raw)]
    else:
        pre, post = [10 * SOL, RENT, 30 * SOL, RENT], [10 * SOL + lamports + RENT - fee, 0, 30 * SOL - lamports, RENT]
        pre_t, post_t = [balance(1, mint, buyer, raw), balance(3, mint, curve, BIG)], [balance(3, mint, curve, BIG + raw)]
    payload = tx([buyer, ata, curve, curve_ata], pre, post, pre_tokens=pre_t, post_tokens=post_t,
                 programs=[program], fee=fee, block_time=block_time)
    payload["slot"] = slot
    return payload


def serve_curve(rpc, mint, trades):
    """Register trades (chronological) as the bonding-curve history of `mint`."""
    history = []
    for n, payload in enumerate(trades):
        sig = b58(zlib.crc32(f"{mint}:{n}".encode()) * 1000 + n + 10 ** 9, 64)
        payload["transaction"]["signatures"] = [sig]
        rpc.payloads[sig] = payload
        history.insert(0, {"signature": sig, "blockTime": payload["blockTime"], "slot": payload["slot"], "err": None})
    rpc.histories[pump_bonding_curve(mint)] = history
    return history


def winner(rpc, mint, *, late=BOB, extra=()):
    # Entry caps at SOL = $150 and 1e9 supply: creator/sniper ≈ $4.3k, Alice $5k, Bob $15k. Alice exits at $15k.
    return serve_curve(rpc, mint, [
        curve_trade(CREATOR, mint, "buy", 1, 35_000_000, T0, 1000),
        curve_trade(SNIPER, mint, "buy", 1, 35_000_000, T0, 1000),
        curve_trade(ALICE, mint, "buy", 1, 30_000_000, T0 + 60, 1010),
        curve_trade(late, mint, "buy", 1, 10_000_000, T0 + 600, 1100),
        *extra,
        curve_trade(ALICE, mint, "sell", 3, 30_000_000, T0 + 3000, 1300),
    ])


def add_and_scan(c, mint, **body):
    assert c.post("/api/discovery/seeds", json={"mint": mint, **body}).status_code == 201
    return c.post(f"/api/discovery/seeds/{mint}/scan").json()


def quote(c, mint, cap):
    c.app.state.quotes.data[mint] = {"price_usd": Decimal(cap) / 10 ** 9, "liquidity_usd": Decimal(50_000),
                                     "market_cap_usd": Decimal(cap), "fdv_usd": Decimal(cap),
                                     "pair_address": "pair", "dex_id": "pumpswap", "symbol": "SYN"}


def test_scan_finds_the_launch_early_buyers_flags_and_entry_caps(client):  # noqa: F811
    c, rpc = client
    mint = b58(7201)
    winner(rpc, mint)
    quote(c, mint, 100_000)
    result = add_and_scan(c, mint, label="Winner A")
    seed, report = result["seed"], result["report"]
    assert report["state"] == "done" and seed["address_kind"] == "pump_bonding_curve"
    assert seed["scan_address"] == pump_bonding_curve(mint) and seed["creator"] == CREATOR
    outcome = seed["outcome"]
    assert outcome["pump_origin"] and outcome["early_buyers"] == 4 and outcome["transactions"] == 5
    assert outcome["peak_basis"] == "current_quote" and outcome["peak_cap_lower_bound_usd"] == "100000"
    assert Decimal(outcome["max_scanned_cap_usd"]) == Decimal(15_000)
    buyers = {b["wallet"]: b for b in c.get(f"/api/discovery/seeds/{mint}/buyers").json()["items"]}
    assert buyers[CREATOR]["flags"] == ["creator", "launch_tx"] and buyers[CREATOR]["rank"] == 1
    assert buyers[SNIPER]["flags"] == ["launch_slot"]
    assert buyers[ALICE]["flags"] == [] and buyers[ALICE]["minutes_after_launch"] == 1
    assert Decimal(buyers[ALICE]["entry_cap_usd"]) == Decimal(5_000) and buyers[ALICE]["cap_status"] == "verified"
    assert Decimal(buyers[BOB]["entry_cap_usd"]) == Decimal(15_000)
    assert ALICE not in [b for b, v in buyers.items() if v.get("trade") != "verified"]


def test_listing_resumes_across_runs_on_a_busy_curve(client, monkeypatch):  # noqa: F811
    c, rpc = client
    monkeypatch.setattr(discovery_module, "PAGE", 2)
    mint = b58(7202)
    winner(rpc, mint)
    c.post("/api/discovery/seeds", json={"mint": mint, "max_pages": 1})
    first = c.post(f"/api/discovery/seeds/{mint}/scan").json()
    assert first["report"]["state"] == "partial" and first["seed"]["state"] == "queued"
    for _ in range(5):
        r = c.post(f"/api/discovery/seeds/{mint}/scan").json()
        if r["seed"]["state"] == "done":
            break
    assert r["seed"]["state"] == "done" and r["seed"]["creator"] == CREATOR
    assert r["seed"]["outcome"]["early_buyers"] == 4


def test_candidates_cross_reference_seeds(client):  # noqa: F811
    c, rpc = client
    a, b, dud = b58(7301), b58(7302), b58(7303)
    winner(rpc, a)
    winner(rpc, b, late=CAROL)
    winner(rpc, dud)  # never went anywhere: no quote, peak = scanned trades only ($15k)
    quote(c, a, 100_000)
    quote(c, b, 250_000)
    for mint in (a, b, dud):
        add_and_scan(c, mint)
    result = c.get("/api/discovery/candidates").json()
    assert result["seeds"] == 3
    by = {i["wallet"]: i for i in result["items"]}
    assert CREATOR not in by and SNIPER not in by  # flagged buyers are excluded by default
    assert result["items"][0]["wallet"] == ALICE
    assert (by[ALICE]["seeds"], by[ALICE]["hits"], by[ALICE]["misses"]) == (3, 2, 1)
    assert by[BOB]["hits"] == 0 and by[BOB]["misses"] == 2 and by[CAROL]["hits"] == 1
    entry = next(e for e in by[ALICE]["entries"] if e["mint"] == a)
    assert entry["status"] == "hit" and Decimal(entry["multiple"]) == 20
    assert {i["wallet"] for i in c.get("/api/discovery/candidates?min_seeds=2").json()["items"]} == {ALICE, BOB}
    flagged = {i["wallet"]: i for i in c.get("/api/discovery/candidates?include_flagged=true").json()["items"]}
    assert flagged[CREATOR]["flags"] == ["creator", "launch_tx"]
    # A lower multiple turns Bob's entries on the winners into hits ($15k × 3 ≤ $100k).
    loose = {i["wallet"]: i for i in c.get("/api/discovery/candidates?multiple=3").json()["items"]}
    assert loose[BOB]["hits"] == 1


def test_declared_peak_is_used_but_labeled(client):  # noqa: F811
    c, rpc = client
    mint = b58(7401)
    winner(rpc, mint)
    seed = add_and_scan(c, mint, peak_market_cap_usd="2000000",
                        peak_source_url="https://example.org/synthetic-chart")["seed"]
    assert seed["outcome"]["peak_basis"] == "declared" and seed["declared_peak_cap_usd"] == "2000000"
    alice = next(i for i in c.get("/api/discovery/candidates").json()["items"] if i["wallet"] == ALICE)
    assert alice["declared_peak_hits"] == 1
    bad = c.post("/api/discovery/seeds", json={"mint": mint, "peak_market_cap_usd": "1",
                                              "peak_source_url": "https://example.org/?key=x"})
    assert bad.status_code == 422


def test_study_adds_the_candidate_and_queues_a_sync(client):  # noqa: F811
    c, rpc = client
    mint = b58(7501)
    winner(rpc, mint)
    add_and_scan(c, mint)
    r = c.post(f"/api/discovery/candidates/{ALICE}/study")
    assert r.status_code == 202 and r.json()["job"]["state"] == "queued"
    assert r.json()["wallet"]["label"] == "Descubierta · 1 semillas"
    alice = next(i for i in c.get("/api/discovery/candidates").json()["items"] if i["wallet"] == ALICE)
    assert alice["tracked"]
    assert not c.post(f"/api/discovery/candidates/{ALICE}/study").json()["created"]


def test_worker_scans_queued_seeds_only_when_no_sync_is_waiting(client):  # noqa: F811
    c, rpc = client
    mint = b58(7601)
    winner(rpc, mint)
    c.post("/api/discovery/seeds", json={"mint": mint})
    c.post(f"/api/wallets/{WALLET}/jobs", json={})
    w = Worker(c.app.state.sessions, rpc, c.app.state.prices, quotes=c.app.state.quotes,
               worker_id="test-worker", refresh_minutes=0, progress_interval=0)
    first = w.run_once()
    assert type(first).__name__ == "Job"
    second = w.run_once()
    assert type(second).__name__ == "DiscoverySeed" and second.mint == mint
    assert c.get("/api/discovery/seeds").json()["items"][0]["state"] == "done"
    # Lowest priority: the funding lookups the scan queued (creator + early buyers + the tracked wallet).
    looked_up = []
    while (unit := w.run_once()) is not None:
        assert type(unit).__name__ == "WalletFunding"
        looked_up.append(unit.address)
    assert CREATOR in looked_up and ALICE in looked_up and WALLET in looked_up


def test_seed_without_activity_fails_with_a_hint_and_non_pump_launch_is_flagged(client):  # noqa: F811
    c, rpc = client
    empty = b58(7701)
    rpc.histories[pump_bonding_curve(empty)] = []
    r = add_and_scan(c, empty)
    assert r["seed"]["state"] == "failed" and "indica el pool" in r["seed"]["error"]
    other = b58(7702)
    serve_curve(rpc, other, [curve_trade(CREATOR, other, "buy", 1, 35_000_000, T0, 1000, program=RAYDIUM),
                             curve_trade(ALICE, other, "buy", 1, 30_000_000, T0 + 60, 1010, program=RAYDIUM)])
    seed = add_and_scan(c, other)["seed"]
    assert not seed["outcome"]["pump_origin"] and "launch_without_pump_program" in seed["outcome"]["issues"]
    alice = next(b for b in c.get(f"/api/discovery/seeds/{other}/buyers").json()["items"] if b["wallet"] == ALICE)
    assert alice["entry_cap_usd"] is None and alice["cap_status"] == "unknown"


def test_legacy_discovery_scans_the_bonding_curve_when_it_exists(client):  # noqa: F811
    c, rpc = client
    mint = b58(7801)
    winner(rpc, mint)
    rpc.account_data[pump_bonding_curve(mint)] = {"owner": PUMP_PROGRAM}
    r = c.post("/api/discovery", json={"token": mint, "limit": 3}).json()
    assert r["scanned_address"] == pump_bonding_curve(mint) and ALICE in r["candidates"]


@pytest.mark.parametrize("body", [{"mint": "nope"}, {"mint": b58(1), "early_trades": 5},
                                  {"mint": b58(1), "pool": "nope"}])
def test_seed_validation(client, body):  # noqa: F811
    c, _ = client
    assert c.post("/api/discovery/seeds", json=body).status_code == 422
