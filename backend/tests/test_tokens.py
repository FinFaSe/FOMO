"""Mint facts, entry market caps beyond pump.fun, open positions and slot ordering (all SYNTHETIC)."""

import json
import time
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from solders.pubkey import Pubkey

from tracker.adapters.quotes import DexScreenerQuotes
from tracker.adapters.solana import ProviderError
from tracker.analytics import reconstruct
from tracker.tokens import PUMP_PROGRAM, pump_bonding_curve, refresh_mints

from .conftest import AS_OF, TOKEN, WALLET, b58
from .test_chain_trades import serve
from .test_incremental_sync import client  # noqa: F401  (fixture)
from .test_normalize import RAYDIUM, pump_buy, pump_sell

SYNC = f"/api/wallets/{WALLET}/sync"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # SPL Token program


def mint_account(supply="1000000000000000", decimals=6, mint_authority=None, freeze_authority=None,
                 extensions=None, program=TOKEN_PROGRAM):
    """Shape of a jsonParsed SPL mint account (SYNTHETIC values)."""
    info = {"decimals": decimals, "supply": supply, "mintAuthority": mint_authority,
            "freezeAuthority": freeze_authority, "isInitialized": True}
    if extensions:
        info["extensions"] = [{"extension": e, "state": {}} for e in extensions]
    return {"owner": program, "lamports": 1461600, "executable": False,
            "data": {"program": "spl-token", "parsed": {"type": "mint", "info": info}, "space": 82}}


def trades(c):
    return c.get(f"/api/wallets/{WALLET}/trades?limit=200").json()["items"]


def test_bonding_curve_pda_matches_the_documented_derivation():
    curve = pump_bonding_curve(TOKEN)
    pda, bump = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(TOKEN))],
                                            Pubkey.from_string(PUMP_PROGRAM))
    assert curve == str(pda) and not pda.is_on_curve()
    assert Pubkey.create_program_address([b"bonding-curve", bytes(Pubkey.from_string(TOKEN)), bytes([bump])],
                                         Pubkey.from_string(PUMP_PROGRAM)) == pda
    assert pump_bonding_curve(b58(9)) != curve


def test_refresh_mints_parses_facts_proves_origin_and_caches(client):  # noqa: F811
    c, rpc = client
    pump_mint, plain, gone = b58(8001), b58(8002), b58(8003)
    rpc.account_data = {pump_mint: mint_account(), pump_bonding_curve(pump_mint): {"owner": PUMP_PROGRAM},
                        plain: mint_account(supply="5000000000", decimals=6, mint_authority=b58(1),
                                            freeze_authority=b58(2), extensions=["transferFeeConfig"])}
    with c.app.state.sessions() as s:
        report = refresh_mints(s, rpc, {pump_mint, plain, gone})
        assert report == {"mints": 3, "refreshed": 3, "pump_origin": 1, "rpc_calls": 2, "pending": 0}
        assert refresh_mints(s, rpc, {pump_mint, plain, gone})["rpc_calls"] == 0  # cached
        # After the TTL only mints without proven pump.fun origin are fetched again.
        again = refresh_mints(s, rpc, {pump_mint, plain, gone}, max_age=timedelta(0))
        assert again["refreshed"] == 2
    facts = c.get(f"/api/tokens/{plain}").json()["facts"]
    assert facts["mint_authority"] == b58(1) and facts["extensions"] == ["transferFeeConfig"]
    assert facts["supply_raw"] == "5000000000" and not facts["pump_origin"]
    assert c.get(f"/api/tokens/{gone}").json()["facts"]["exists"] is False
    assert c.get(f"/api/tokens/{pump_mint}").json()["facts"]["pump_origin"] is True
    assert c.get(f"/api/tokens/{b58(8999)}").status_code == 404


def test_graduated_pump_token_gets_verified_cap_from_its_bonding_curve_account(client):  # noqa: F811
    c, rpc = client
    mint = b58(8100)
    rpc.account_data = {mint: mint_account(), pump_bonding_curve(mint): {"owner": PUMP_PROGRAM}}
    serve(rpc, [pump_buy(mint=mint, program=PUMPSWAP, block_time=int(time.time()) - 600)])
    report = c.post(SYNC, json={}).json()
    assert report["mints"]["pump_origin"] == 1 and report["pricing"]["market_caps"] == 1
    [t] = trades(c)
    assert t["venues"] == ["pumpswap"] and t["evidence"]["market_cap"] == "verified"
    assert t["usd"]["market_cap_method"] == "pumpfun_fixed_supply_upper_bound"


def test_revoked_authority_gives_an_estimated_cap_that_the_ranking_rejects(client):  # noqa: F811
    c, rpc = client
    mint = b58(8200)
    rpc.account_data = {mint: mint_account(supply="2000000000000000")}  # 2e9 tokens, no mint authority
    now = int(time.time())
    serve(rpc, [pump_buy(mint=mint, program=RAYDIUM, block_time=now - 7200),
                pump_sell(mint=mint, program=RAYDIUM, block_time=now - 3600)])
    report = c.post(SYNC, json={}).json()
    assert report["pricing"]["estimated_caps"] == 2 and report["pricing"]["market_caps"] == 0
    sell, buy = trades(c)
    assert buy["evidence"]["market_cap"] == "estimated"
    assert buy["usd"]["market_cap_method"] == "current_supply_mint_authority_revoked"
    assert Decimal(buy["usd"]["market_cap_usd"]) == Decimal(150) / Decimal(3_500_000) * 2 * 10 ** 9
    analysis = c.get(f"/api/wallets/{WALLET}/analysis").json()
    [p] = analysis["positions"]
    assert p["early_entry_reported"] and not p["market_cap_verified"]
    assert "unverified_trades_usd_or_historical_market_cap" in analysis["exclusion_reasons"]
    # Proof of pump.fun origin arriving later upgrades the estimate on the next valuation.
    rpc.account_data[pump_bonding_curve(mint)] = {"owner": PUMP_PROGRAM}
    with c.app.state.sessions() as s:
        refresh_mints(s, rpc, {mint}, max_age=timedelta(0))
    assert c.post(f"/api/wallets/{WALLET}/price").json()["market_caps"] == 2
    assert all(t["evidence"]["market_cap"] == "verified" for t in trades(c))


def test_active_mint_authority_leaves_the_cap_unknown(client):  # noqa: F811
    c, rpc = client
    mint = b58(8300)
    rpc.account_data = {mint: mint_account(mint_authority=b58(5))}
    serve(rpc, [pump_buy(mint=mint, program=RAYDIUM, block_time=int(time.time()) - 600)])
    c.post(SYNC, json={})
    [t] = trades(c)
    assert t["evidence"]["market_cap"] == "unknown" and t["usd"]["market_cap_usd"] is None


def test_mint_lookup_failure_does_not_break_the_sync(client):  # noqa: F811
    c, rpc = client

    def down(*args, **kwargs):
        raise ProviderError("Synthetic accounts unavailable", 503)

    rpc.accounts = down
    serve(rpc, [pump_buy(block_time=int(time.time()) - 600)])
    report = c.post(SYNC, json={}).json()
    assert report["state"] == "scanned" and report["mints"] == {"error": "Synthetic accounts unavailable"}
    assert report["pricing"]["priced"] == 1  # stored bonding-curve trade still proves pump.fun origin


def test_open_positions_are_marked_to_market(client):  # noqa: F811
    c, rpc = client
    quotes = c.app.state.quotes
    held, closed = b58(8400), b58(8401)
    now = int(time.time())
    serve(rpc, [pump_buy(mint=closed, block_time=now - 9000), pump_sell(mint=closed, block_time=now - 8000),
                pump_buy(mint=held, block_time=now - 600)])
    rpc.account_data = {held: mint_account(freeze_authority=b58(7))}
    c.post(SYNC, json={})
    quotes.data = {held: {"price_usd": Decimal("0.0001"), "liquidity_usd": Decimal("4000"),
                          "market_cap_usd": Decimal("100000"), "fdv_usd": Decimal("100000"),
                          "pair_address": "pair", "dex_id": "pumpswap", "symbol": "SYN"}}
    r = c.get(f"/api/wallets/{WALLET}/open-positions").json()
    [p] = r["positions"]  # the closed round trip is not listed
    basis = (1 + Decimal("0.000105")) * 150
    assert p["token"] == held and Decimal(p["cost_basis_usd"]) == basis
    assert Decimal(p["value_usd"]) == Decimal(3_500_000) * Decimal("0.0001") == Decimal(350)
    assert Decimal(p["unrealized_pnl_usd"]) == 350 - basis
    assert p["symbol"] == "SYN" and set(p["risks"]) == {"freeze_authority_active", "low_liquidity"}
    assert Decimal(r["totals"]["unrealized_pnl_usd"]) == 350 - basis and r["totals"]["valued_positions"] == 1
    # Quotes are cached for a minute; a failing source falls back to the cached quote with a notice.
    calls = len(quotes.calls)
    c.get(f"/api/wallets/{WALLET}/open-positions")
    assert len(quotes.calls) == calls
    with c.app.state.sessions() as s:
        from tracker.db import TokenQuote
        s.get(TokenQuote, held).fetched_at = "2000-01-01T00:00:00+00:00"
        s.commit()
    quotes.quotes = lambda mints: (_ for _ in ()).throw(ProviderError("Synthetic quotes down", 503))
    degraded = c.get(f"/api/wallets/{WALLET}/open-positions").json()
    assert degraded["issues"] == ["Synthetic quotes down"] and degraded["positions"][0]["price_usd"] == "0.0001"


def test_open_position_without_a_quote_has_no_value(client):  # noqa: F811
    c, rpc = client
    serve(rpc, [pump_buy(block_time=int(time.time()) - 600)])
    c.post(SYNC, json={})
    [p] = c.get(f"/api/wallets/{WALLET}/open-positions").json()["positions"]
    assert p["quote_found"] is False and p["value_usd"] is None and p["unrealized_pnl_usd"] is None


def test_slots_order_fills_that_share_a_block_time(fill):
    buy, sell = fill("buy", 10, "100", days_ago=1), fill("sell", 10, "300", days_ago=1)
    sell.timestamp = buy.timestamp
    # Signature order would put the sell first; the slots say the buy came first.
    buy.signature, sell.signature = "z" * 88, "a" * 88
    buy.slot, sell.slot = 101, 102
    [p] = reconstruct([sell, buy], AS_OF)
    assert not p.issues and p.closed_at is not None and p.pnl == Decimal("200")
    sell.slot = 101  # same slot: intrablock order unknown
    assert "ambiguous_transaction_order" in reconstruct([sell, buy], AS_OF)[0].issues
    sell.slot = None  # unknown slot (e.g. imported ledger)
    assert "ambiguous_transaction_order" in reconstruct([sell, buy], AS_OF)[0].issues


def test_dexscreener_adapter_picks_the_deepest_base_pair_and_batches():
    requests = []
    mints = [b58(9000 + i) for i in range(31)]

    def transport(request):
        requests.append(request.url.path)
        pairs = [
            {"baseToken": {"address": mints[0], "symbol": "AAA"}, "priceUsd": "0.5", "liquidity": {"usd": 100},
             "pairAddress": "shallow", "dexId": "raydium"},
            {"baseToken": {"address": mints[0], "symbol": "AAA"}, "priceUsd": "0.51", "liquidity": {"usd": 9000},
             "pairAddress": "deep", "dexId": "pumpswap", "marketCap": 51000, "fdv": 51000},
            # The mint as quote token is not a quote of the mint itself.
            {"baseToken": {"address": b58(1)}, "quoteToken": {"address": mints[1]}, "priceUsd": "99"},
        ]
        return httpx.Response(200, text=json.dumps(pairs))

    out = DexScreenerQuotes(httpx.Client(transport=httpx.MockTransport(transport))).quotes(mints)
    assert len(requests) == 2 and requests[0].startswith("/tokens/v1/solana/")
    assert requests[0].count(",") == 29 and requests[1].endswith(mints[30])
    assert out[mints[0]]["pair_address"] == "deep" and out[mints[0]]["price_usd"] == Decimal("0.51")
    assert mints[1] not in out
    failing = DexScreenerQuotes(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with pytest.raises(ProviderError):
        failing.quotes([mints[0]])
