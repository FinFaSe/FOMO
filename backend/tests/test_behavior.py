"""Bots/MEV, Jito tips, funding origins, linked wallets, insiders and copyability (all SYNTHETIC)."""

from datetime import timedelta
from decimal import Decimal

from tracker import entities as entities_module
from tracker.analytics import Fill, analyze, behavior, reconstruct
from tracker.normalize import JITO_TIP_ACCOUNTS, normalize
from tracker.schemas import Filters, address

from .conftest import AS_OF, WALLET, b58
from .test_discovery import ALICE, BOB, CREATOR, add_and_scan, quote, winner
from .test_incremental_sync import client  # noqa: F401  (fixture)
from .test_normalize import PUMP, RENT, SOL, SYSTEM, balance, tx

FUNDER, EXCHANGE, OTHER = b58(6101), b58(6102), b58(6103)
TIP = sorted(JITO_TIP_ACCOUNTS)[0]


def fill_at(token, kind, minutes, slot=None, gross="100"):
    return Fill(signature=b58(int(minutes * 1000) + (7 if kind == "sell" else 3), 64), event_index=0, token=token,
                timestamp=AS_OF - timedelta(days=1) + timedelta(minutes=minutes), kind=kind,
                quantity=Decimal(1), gross_usd=Decimal(gross), fees_usd=Decimal(0),
                market_cap_usd=Decimal(100000), source_url="https://example.org/synthetic",
                evidence={"trade": "verified", "usd": "verified", "market_cap": "verified"}, slot=slot)


def classify(fills):
    since = AS_OF - timedelta(days=90)
    return behavior(reconstruct(fills, AS_OF), fills, since, AS_OF)


def test_round_trips_inside_two_blocks_look_like_mev():
    fills = []
    for i in range(3):
        fills += [fill_at(b58(9000 + i), "buy", i * 10, slot=1000 + i * 100),
                  fill_at(b58(9000 + i), "sell", i * 10 + 0.5, slot=1001 + i * 100)]
    assert classify(fills)["same_block_round_trips"] == 3 and classify(fills)["classification"] == "likely_bot"
    assert classify(fills[:2])["classification"] == "possible_bot"
    r = analyze(WALLET, fills, Filters(), AS_OF)
    assert "bot_or_mev_behavior" in r["exclusion_reasons"] and r["behavior"]["classification"] == "likely_bot"


def test_fast_flips_and_trade_frequency():
    flips = []
    for i in range(10):  # held 30 s each
        flips += [fill_at(b58(9100 + i), "buy", i * 5), fill_at(b58(9100 + i), "sell", i * 5 + 0.5)]
    signals = classify(flips)
    assert signals["fast_round_trips"] == 10 and signals["classification"] == "likely_bot"
    busy = []
    for i in range(150):  # 300 trades within five hours of one day, each position held 90 s
        busy += [fill_at(b58(9200 + i), "buy", i * 2), fill_at(b58(9200 + i), "sell", i * 2 + 1.5)]
    assert classify(busy)["max_trades_per_day"] == 300 and classify(busy)["classification"] == "likely_bot"
    calm = []
    for i in range(10):  # held for hours, a few trades a day
        calm += [fill_at(b58(9300 + i), "buy", i * 600), fill_at(b58(9300 + i), "sell", i * 600 + 300)]
    assert classify(calm)["classification"] == "none"


def test_jito_tip_is_split_from_the_swap_and_only_counts_when_the_wallet_pays():
    assert all(address(a) for a in JITO_TIP_ACCOUNTS) and len(JITO_TIP_ACCOUNTS) == 8
    ata, curve = b58(6201), b58(6202)
    t = tx([WALLET, ata, curve, TIP], [10 * SOL, 0, 30 * SOL, SOL],
           [10 * SOL - SOL - RENT - 5000 - 1_000_000, RENT, 31 * SOL, SOL + 1_000_000],
           pre_tokens=[], post_tokens=[balance(1, b58(6203), WALLET, 1_000_000)], programs=[PUMP])
    [event] = normalize(t, WALLET)
    assert event["quote_amount"] == Decimal(1) and event["tip_sol"] == Decimal("0.001")
    relayed = tx([OTHER, WALLET, ata, curve, TIP], [SOL, 10 * SOL, 0, 30 * SOL, SOL],
                 [SOL - 5000 - 1_000_000, 9 * SOL - RENT, RENT, 31 * SOL, SOL + 1_000_000],
                 post_tokens=[balance(2, b58(6203), WALLET, 1_000_000)], programs=[PUMP], signers={OTHER, WALLET})
    [event] = normalize(relayed, WALLET)
    assert event["tip_sol"] == 0 and event["quote_amount"] == Decimal(1)


def funding_tx(funder, wallet, block_time, lamports=SOL):
    return tx([funder, wallet, SYSTEM], [10 * SOL, 0, 1], [10 * SOL - lamports - 5000, lamports, 1],
              programs=[SYSTEM], block_time=block_time)


def serve_history(rpc, owner, payloads):
    """Chronological payloads become `owner`'s signature history (newest first)."""
    history = []
    for n, payload in enumerate(payloads):
        sig = b58(abs(hash_text(owner)) * 100 + n + 5 * 10 ** 9, 64)
        payload["transaction"]["signatures"] = [sig]
        rpc.payloads[sig] = payload
        history.insert(0, {"signature": sig, "blockTime": payload["blockTime"], "err": None})
    rpc.histories[owner] = history


def hash_text(text):
    import zlib
    return zlib.crc32(text.encode())


def test_funding_origin_and_funder_kind(client):  # noqa: F811
    c, rpc = client
    serve_history(rpc, ALICE, [funding_tx(FUNDER, ALICE, 1_700_000_000)])
    rpc.histories[FUNDER] = [{"signature": b58(1, 64), "blockTime": 1_700_000_000}]
    r = c.post(f"/api/funding/{ALICE}/lookup").json()
    assert (r["state"], r["funder"], r["funder_kind"], r["lamports"]) == ("done", FUNDER, "wallet", str(SOL))
    # An exchange-like funder: 1,000 signatures within an hour.
    serve_history(rpc, BOB, [funding_tx(EXCHANGE, BOB, 1_700_000_100)])
    rpc.histories[EXCHANGE] = [{"signature": b58(i + 10, 64), "blockTime": 1_700_000_000 + i} for i in range(1000)]
    assert c.post(f"/api/funding/{BOB}/lookup").json()["funder_kind"] == "high_volume"
    assert c.get(f"/api/funding/{BOB}").json()["funder"] == EXCHANGE


def test_funding_unknown_cases(client, monkeypatch):  # noqa: F811
    c, rpc = client
    # The wallet paid for its own first transaction: no incoming SOL from someone else.
    serve_history(rpc, ALICE, [funding_tx(ALICE, OTHER, 1_700_000_000)])
    r = c.post(f"/api/funding/{ALICE}/lookup").json()
    assert r["state"] == "unknown" and r["reason"] == "no_incoming_sol_in_first_transactions"
    monkeypatch.setattr(entities_module, "PAGE", 1)
    serve_history(rpc, BOB, [funding_tx(FUNDER, BOB, 1_700_000_000 + i) for i in range(20)])
    assert c.post(f"/api/funding/{BOB}/lookup").json()["reason"] == "history_too_long"
    assert c.post("/api/funding/nope").status_code == 422


def test_shared_funders_and_co_buys_form_entities(client):  # noqa: F811
    c, rpc = client
    rpc.histories[FUNDER] = []
    rpc.histories[EXCHANGE] = [{"signature": b58(i + 10, 64), "blockTime": 1_700_000_000 + i} for i in range(1000)]
    for wallet, funder in ((ALICE, FUNDER), (BOB, FUNDER), (OTHER, EXCHANGE), (b58(6104), EXCHANGE)):
        serve_history(rpc, wallet, [funding_tx(funder, wallet, 1_700_000_000)])
        c.post(f"/api/funding/{wallet}/lookup")
    found = c.get("/api/entities").json()["items"]
    [group] = [e for e in found if ALICE in e["members"]]
    assert set(group["members"]) == {ALICE, BOB} and group["links"][0]["reason"] == "same_funder"
    assert not any(OTHER in e["members"] for e in found)  # an exchange funds strangers: no link
    # Wallet profile endpoint shows the funding origin and the entity.
    c.post("/api/wallets", json={"address": ALICE})
    links = c.get(f"/api/wallets/{ALICE}/links").json()
    assert links["funding"]["funder"] == FUNDER and set(links["entity"]["members"]) == {ALICE, BOB}


def test_insiders_are_flagged_and_excluded_from_candidates(client):  # noqa: F811
    c, rpc = client
    a, b = b58(7301), b58(7302)
    winner(rpc, a)
    winner(rpc, b)
    quote(c, a, 100_000)
    quote(c, b, 100_000)
    add_and_scan(c, a)
    add_and_scan(c, b)
    assert {i["wallet"] for i in c.get("/api/discovery/candidates").json()["items"]} >= {ALICE, BOB}
    serve_history(rpc, ALICE, [funding_tx(CREATOR, ALICE, 1_700_000_000)])  # the creator funded Alice
    serve_history(rpc, BOB, [funding_tx(FUNDER, BOB, 1_700_000_000)])
    serve_history(rpc, CREATOR, [funding_tx(FUNDER, CREATOR, 1_700_000_000)])  # Bob shares the creator's funder
    rpc.histories[FUNDER] = []
    for wallet in (ALICE, BOB, CREATOR):
        c.post(f"/api/funding/{wallet}/lookup")
    default = {i["wallet"] for i in c.get("/api/discovery/candidates").json()["items"]}
    assert ALICE not in default and BOB not in default
    flagged = {i["wallet"]: i for i in c.get("/api/discovery/candidates?include_flagged=true").json()["items"]}
    assert "funded_by_creator" in flagged[ALICE]["flags"]
    assert "shares_funder_with_creator" in flagged[BOB]["flags"]
    assert flagged[ALICE]["entity"]["size"] >= 2


def test_copy_lag_cost_and_size_versus_the_curve(client):  # noqa: F811
    c, rpc = client
    mint = b58(7901)
    winner(rpc, mint)
    add_and_scan(c, mint)
    buyers = {b["wallet"]: b for b in c.get(f"/api/discovery/seeds/{mint}/buyers").json()["items"]}
    alice = buyers[ALICE]
    # The next scanned trade 30 s after Alice's buy is Bob's, at 3× her price: a follower pays +200%.
    assert Decimal(alice["price_move_30s"]) == 2 and Decimal(alice["price_move_5s"]) == 2
    assert Decimal(alice["curve_sol_before"]) == 30 and Decimal(alice["size_vs_curve"]) == Decimal(1) / 30
    assert buyers[BOB]["price_move_60s"] is None or Decimal(buyers[BOB]["price_move_60s"]) >= -1
    candidate = next(i for i in c.get("/api/discovery/candidates").json()["items"] if i["wallet"] == ALICE)
    assert Decimal(candidate["median_price_move_30s"]) == 2
