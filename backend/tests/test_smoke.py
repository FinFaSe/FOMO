"""The live smoke test's own logic, run offline with SYNTHETIC adapters so it cannot rot unnoticed in CI.

The real run (`python -m tracker.smoke`) needs network access; this only proves the checks and the report."""

import time
from decimal import Decimal

import httpx

from tracker.adapters.solana import ProviderError
from tracker.migrate import head_revision
from tracker.normalize import GECKO_DEXES, JITO_TIP_ACCOUNTS, VENUES, WSOL
from tracker.smoke import jito_tip_accounts, run

from .conftest import TOKEN, WALLET, database_url
from .test_api import SyntheticPrices, SyntheticQuotes, SyntheticRPC
from .test_chain_trades import serve
from .test_history import POOL, SyntheticHistory, candle
from .test_normalize import pump_buy, pump_sell


def chain():
    rpc, now = SyntheticRPC(), int(time.time())
    serve(rpc, [pump_buy(block_time=now - 7200), pump_sell(block_time=now - 3600)])
    return rpc, rpc.history[-1]["signature"]  # the buy, as the audited transaction


def statuses(report):
    return {c["name"]: c["status"] for c in report["checks"]}


class Gecko(SyntheticHistory):
    """SYNTHETIC GeckoTerminal: one trending pool whose parsed trades are chain()'s buy (1 SOL for 3.5M tokens)
    and sell (3.5M tokens for 1.5 SOL), and one pool per DEX owned by the expected program."""

    def __init__(self, rpc):
        super().__init__()
        self.rpc = rpc
        programs = {name: program for program, name in VENUES.items()}
        for dex, names in GECKO_DEXES.items():
            rpc.account_data[f"pool-{dex}"] = {"owner": programs[sorted(names)[0]]}

    def trending_pools(self, page=1):
        return [{"pool": POOL, "dex": "pumpswap", "name": "SYNTHETIC / SOL"}]

    def pool_trades(self, pool):
        sell, buy = (item["signature"] for item in self.rpc.history)  # newest first
        tokens = Decimal(3_500_000)
        return [{"tx": buy, "signer": WALLET, "kind": "buy", "from_token": WSOL, "to_token": TOKEN,
                 "from_amount": Decimal(1), "to_amount": tokens, "volume_usd": None},
                {"tx": sell, "signer": WALLET, "kind": "sell", "from_token": TOKEN, "to_token": WSOL,
                 "from_amount": tokens, "to_amount": Decimal("1.5"), "volume_usd": None}]

    def dex_pools(self, dex, page=1):
        return [f"pool-{dex}"]


def market(rpc, usd="0.00015"):
    """SYNTHETIC current quote and pool candles: 1e-6 SOL × 150 USD/SOL = 0.00015 USD."""
    quotes, history = SyntheticQuotes(), Gecko(rpc)
    quotes.data[TOKEN] = {"found": True, "price_usd": usd, "dex_id": "pumpswap"}
    now = int(time.time())
    for timeframe, step in (("minute", 60), ("hour", 3600)):
        history.series[(POOL, timeframe)] = [candle(now // step * step - i * step, "1e-6", "1e-6") for i in range(1, 4)]
    return quotes, history


def test_all_checks_pass_with_working_sources(tmp_path):
    rpc, signature = chain()
    quotes, history = market(rpc)
    report = run(rpc, SyntheticPrices(), quotes, history, lambda: sorted(JITO_TIP_ACCOUNTS),
                 db_url=database_url(tmp_path), signature=signature)
    assert report["ok"] and report["failed_required"] == [] and report["warnings"] == []
    assert set(statuses(report).values()) == {"ok"}
    detail = {c["name"]: c["detail"] for c in report["checks"]}
    [event] = detail["normalization"]["events"]
    assert event["kind"] == "buy" and event["trade"] == "verified" and event["quote_asset"] == "SOL"
    assert detail["ingestion"]["first"]["transactions"] == 2 and detail["ingestion"]["incremental_resync"]["transactions"] == 0
    assert detail["chain_trades"]["by_kind"] == {"buy": 1, "sell": 1}
    assert detail["sol_usd"]["close"] == "150" and detail["jito_tip_accounts"]["match"]
    assert detail["migrations"]["revision"] == head_revision()
    assert detail["current_quotes"]["found"] == 1 and detail["pool_candles"]["ratio"] == "1.000"
    assert detail["trades_vs_geckoterminal"]["outcomes"] == {"agree": 2} and detail["venue_program_ids"]["dexes"] == 14


def test_a_pool_price_that_disagrees_with_the_quote_is_a_warning(tmp_path):
    rpc, signature = chain()
    quotes, history = market(rpc, usd="0.0015")  # ten times the candle price: not a SOL-denominated candle
    report = run(rpc, SyntheticPrices(), quotes, history, lambda: sorted(JITO_TIP_ACCOUNTS),
                 db_url=database_url(tmp_path), signature=signature)
    assert report["ok"] and report["warnings"] == ["pool_candles"]


def test_required_failures_fail_the_run_and_optional_ones_only_warn(tmp_path):
    rpc, signature = chain()
    prices = SyntheticPrices()

    def down(start, end):
        raise ProviderError("Synthetic price source unavailable", 503)

    def no_jito():
        raise httpx.ConnectError("synthetic: block engine unreachable")

    prices.minute_candles = down
    report = run(rpc, prices, SyntheticQuotes(), Gecko(rpc), no_jito, db_url=database_url(tmp_path),
                 signature=signature)
    assert not report["ok"]
    assert report["failed_required"] == ["sol_usd", "pricing_and_mint_facts"]
    assert report["warnings"] == ["jito_tip_accounts"]
    assert statuses(report)["ingestion"] == "ok"


def test_later_checks_are_skipped_when_the_transaction_is_unavailable(tmp_path):
    rpc, _ = chain()
    report = run(rpc, SyntheticPrices(), SyntheticQuotes(), Gecko(rpc), lambda: [],
                 db_url=database_url(tmp_path), signature="1" * 64)
    status = statuses(report)
    assert status["rpc_transaction"] == "failed" and status["normalization"] == "skipped"
    assert status["migrations"] == "ok" and status["ingestion"] == "skipped"
    assert status["jito_tip_accounts"] == "failed" and not report["ok"]


def test_jito_adapter_sends_get_tip_accounts():
    seen = {}

    def transport(request):
        seen.update(url=str(request.url), body=request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": ["A", "B"]})

    assert jito_tip_accounts(httpx.Client(transport=httpx.MockTransport(transport))) == ["A", "B"]
    assert seen["url"].endswith("/api/v1/getTipAccounts") and b'"getTipAccounts"' in seen["body"]
