"""Normalizer vs GeckoTerminal's parsed trades: outcome classification and adapter parsing. SYNTHETIC data
shaped like the real transactions inspected on 2026-11-03 (relayed trades, split routes, arbitrage residue)."""

import json
from decimal import Decimal

import httpx

from tracker.adapters.geckoterminal import GeckoTerminal
from tracker.crosscheck import compare, crosscheck, summarize, venue_programs
from tracker.normalize import GECKO_DEXES, VENUES, WSOL

from .conftest import TOKEN, WALLET, b58
from .test_api import SyntheticRPC
from .test_normalize import POOL, RELAYER, SOL, balance, pump_buy, relayed_usdc_sell, tx

OTHER_POOL = b58(5551)


def gt(kind, tokens, quote_amount, signer=WALLET, quote=WSOL, sig="sig", volume=None):
    buy = kind == "buy"
    return {"tx": sig, "signer": signer, "kind": kind, "from_token": quote if buy else TOKEN,
            "to_token": TOKEN if buy else quote, "from_amount": Decimal(quote_amount if buy else tokens),
            "to_amount": Decimal(tokens if buy else quote_amount), "volume_usd": volume}


def test_same_side_and_exact_amount_agree_and_the_quote_is_compared_separately():
    row = compare(pump_buy(), gt("buy", 3_500_000, "0.99"))
    assert row["outcome"] == "agree" and abs(row["quote_diff"] - (1 / 0.99 - 1)) < 1e-9  # fees inside ours


def test_relayed_trades_are_attributed_to_the_signer_that_owns_the_tokens():
    row = compare(relayed_usdc_sell(1_488_440), gt("sell", 5, "0.15", signer=RELAYER, volume=Decimal("21.6")))
    assert row["outcome"] == "agree" and row["relayed"] and row["quote"] == "SOL"


def test_a_different_amount_through_one_pool_is_a_disagreement():
    row = compare(pump_buy(), gt("buy", 3_400_000, "1"))
    assert row["outcome"] == "disagree" and Decimal(row["token_ratio"]) > 1


def test_a_route_through_several_pools_is_not_compared_to_one_leg():
    payload = pump_buy()
    payload["meta"]["preTokenBalances"].append(balance(4, TOKEN, OTHER_POOL, 1_000_000_000_000))
    payload["meta"]["postTokenBalances"].append(balance(4, TOKEN, OTHER_POOL, 0))
    payload["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = str(4_500_000_000_000)
    assert compare(payload, gt("buy", 3_500_000, "1"))["outcome"] == "split_route"


def test_signers_without_the_token_change_are_not_traders():
    # Tokens move between two pools only; the signer just pays the fee (an executor or arbitrage program).
    payload = tx([RELAYER, POOL, OTHER_POOL], [SOL, 0, 0], [SOL - 5000, 0, 0],
                 pre_tokens=[balance(1, TOKEN, POOL, 10), balance(2, TOKEN, OTHER_POOL, 0)],
                 post_tokens=[balance(1, TOKEN, POOL, 0), balance(2, TOKEN, OTHER_POOL, 10)],
                 programs=[next(iter(VENUES))])
    assert compare(payload, gt("buy", "0.00001", "1", signer=RELAYER))["outcome"] == "not_trader"


def test_summary_and_sampling_over_an_adapter():
    rpc = SyntheticRPC()
    rpc.payloads["buy"] = pump_buy()

    class Gecko:
        def trending_pools(self, page):
            return [{"pool": POOL, "dex": "pump-fun"}]

        def pool_trades(self, pool):
            return [gt("buy", 3_500_000, "1", sig="buy")]

    report = crosscheck(rpc, Gecko())
    assert report["outcomes"] == {"agree": 1} and report["agreement"] == 1.0 and report["disagreements"] == []
    assert summarize([])["agreement"] is None


def test_venue_programs_flags_pools_owned_by_another_program():
    rpc = SyntheticRPC()
    programs = {name: program for program, name in VENUES.items()}

    class Gecko:
        def dex_pools(self, dex):
            return [f"{dex}-1", f"{dex}-2"]

    for dex, names in GECKO_DEXES.items():
        for i in (1, 2):
            rpc.account_data[f"{dex}-{i}"] = {"owner": programs[sorted(names)[0]]}
    rpc.account_data["orca-2"] = {"owner": "SomeOtherProgram"}
    result = venue_programs(rpc, Gecko())
    assert result["meteora"]["ok"] and not result["orca"]["ok"]


def test_adapter_parses_trades_and_drops_anomalous_or_malformed_rows():
    def row(**changes):
        attributes = {"tx_hash": "sig", "tx_from_address": WALLET, "kind": "buy", "from_token_address": WSOL,
                      "to_token_address": TOKEN, "from_token_amount": "0.5", "to_token_amount": "1000",
                      "volume_in_usd": "75.1", "block_timestamp": "2026-11-03T16:49:10Z", "is_anomalous": False}
        return {"type": "trade", "attributes": {**attributes, **changes}}

    body = {"data": [row(), row(is_anomalous=True), row(kind="swap"), row(to_token_amount="NaN?"),
                     row(from_token_amount="0")]}
    seen = {}

    def transport(request):
        seen["path"] = request.url.path
        return httpx.Response(200, text=json.dumps(body))

    [trade] = GeckoTerminal(httpx.Client(transport=httpx.MockTransport(transport)), spacing=0).pool_trades("POOL")
    assert seen["path"] == "/api/v2/networks/solana/pools/POOL/trades"
    assert trade["kind"] == "buy" and trade["to_amount"] == Decimal(1000) and trade["volume_usd"] == Decimal("75.1")
