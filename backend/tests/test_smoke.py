"""The live smoke test's own logic, run offline with SYNTHETIC adapters so it cannot rot unnoticed in CI.

The real run (`python -m tracker.smoke`) needs network access; this only proves the checks and the report."""

import time

import httpx

from tracker.adapters.solana import ProviderError
from tracker.migrate import head_revision
from tracker.normalize import JITO_TIP_ACCOUNTS
from tracker.smoke import jito_tip_accounts, run

from .conftest import TOKEN, database_url
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


def market(usd="0.00015"):
    """SYNTHETIC current quote and pool candles: 1e-6 SOL × 150 USD/SOL = 0.00015 USD."""
    quotes, history = SyntheticQuotes(), SyntheticHistory()
    quotes.data[TOKEN] = {"found": True, "price_usd": usd, "dex_id": "pumpswap"}
    now = int(time.time())
    for timeframe, step in (("minute", 60), ("hour", 3600)):
        history.series[(POOL, timeframe)] = [candle(now // step * step - i * step, "1e-6", "1e-6") for i in range(1, 4)]
    return quotes, history


def test_all_checks_pass_with_working_sources(tmp_path):
    rpc, signature = chain()
    quotes, history = market()
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


def test_a_pool_price_that_disagrees_with_the_quote_is_a_warning(tmp_path):
    rpc, signature = chain()
    quotes, history = market(usd="0.0015")  # ten times the candle price: not a SOL-denominated candle
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
    report = run(rpc, prices, SyntheticQuotes(), SyntheticHistory(), no_jito, db_url=database_url(tmp_path),
                 signature=signature)
    assert not report["ok"]
    assert report["failed_required"] == ["sol_usd", "pricing_and_mint_facts"]
    assert report["warnings"] == ["jito_tip_accounts"]
    assert statuses(report)["ingestion"] == "ok"


def test_later_checks_are_skipped_when_the_transaction_is_unavailable(tmp_path):
    rpc, _ = chain()
    report = run(rpc, SyntheticPrices(), SyntheticQuotes(), SyntheticHistory(), lambda: [],
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
