"""Live smoke test against the real network: RPC, migrations, normalization, prices, quotes and Jito.

    python -m tracker.smoke [--database-url URL] [--signature SIG] [--wallet ADDRESS] [--pages N] [--page-size N]

(`scripts/live_smoke.py` runs the same thing.) Needs outbound access to the Solana RPC (SOLANA_RPC_URL), Coinbase,
DEX Screener, GeckoTerminal and the Jito block engine. Nothing synthetic is generated: the JSON output reports
what each real source returned. Besides the audited transaction and its signer's wallet (or `--wallet`), it
normalizes a few of the latest transactions of pump.fun, PumpSwap and Jupiter, so a venue upgrade that the
normalizer no longer understands shows up here. The default database is a temporary SQLite file; `--database-url`
adds one wallet and its transactions to the database given. Exit code 1 when a required check fails; optional
checks (current quotes, pool candles, Jito tip accounts) are reported without failing the run.
"""

import argparse
import json
import sys
import tempfile
import time
from decimal import Decimal

import httpx
from fastapi.testclient import TestClient

from .adapters.solana import ProviderError, digest, observe
from .main import create_app
from .migrate import current_revision, head_revision
from .normalize import JITO_TIP_ACCOUNTS, NORMALIZER_VERSION, VENUES, WSOL, normalize

# Public transaction observed during the source audit; its signer is a research candidate, not a proven trader.
AUDITED_SIGNATURE = "k5QYVE2czvAA5bP8wjPF5rdgxEsCzzN4XBQaLJW5VkGTxsbv5yedb5hpodSRBjmRfhPDvAGJeiNM9mYrcCyX2oh"
JITO_TIP_ACCOUNTS_URL = "https://mainnet.block-engine.jito.wtf/api/v1/getTipAccounts"
KINDS = {"buy", "sell", "transfer_in", "transfer_out"}
QUOTES = {"SOL", "USDC", "USDT"}
SAMPLED_VENUES = ("pump.fun", "pumpswap", "jupiter-v6")


class Skip(Exception):
    """A check that cannot run because an earlier one failed or there is nothing to check."""


def jito_tip_accounts(client: httpx.Client | None = None) -> list[str]:
    """The tip accounts the Jito block engine currently designates (JSON-RPC getTipAccounts)."""
    client = client or httpx.Client(timeout=20)
    r = client.post(JITO_TIP_ACCOUNTS_URL, json={"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts", "params": []})
    r.raise_for_status()
    result = r.json().get("result")
    if not isinstance(result, list) or not all(isinstance(a, str) for a in result):
        raise ValueError("Respuesta de getTipAccounts inesperada")
    return result


def _event(e: dict) -> dict:
    return {k: str(v) if isinstance(v, Decimal) else v for k, v in e.items()
            if k in ("event_index", "mint", "kind", "token_amount", "quote_asset", "quote_amount", "venues", "trade",
                     "issues", "tip_sol")}


def _event_problems(e: dict) -> list[str]:
    """Invariants every normalized event must satisfy, whatever the transaction."""
    problems = []
    if e["kind"] not in KINDS:
        problems.append(f"kind {e['kind']!r}")
    if not e["token_amount"] > 0:
        problems.append("token_amount <= 0")
    if e["trade"] == "verified" and not (e["venues"] and e["quote_asset"] in QUOTES and e["quote_amount"]):
        problems.append("verified without venue or quote")
    if e["quote_amount"] is not None and e["quote_amount"] < 0:
        problems.append("negative quote_amount")
    return problems


def run(rpc, prices, quotes, history, jito=jito_tip_accounts, *, db_url: str, signature: str = AUDITED_SIGNATURE,
        wallet: str | None = None, pages: int = 1, page_size: int = 5, per_venue: int = 4) -> dict:
    checks, ctx = [], {}

    def check(name: str, required: bool, fn):
        started = time.perf_counter()
        try:
            detail, status = fn(), "ok"
        except Skip as exc:
            detail, status = {"reason": str(exc)}, "skipped"
        except Exception as exc:  # report every failure and keep going
            detail, status = {"error": f"{type(exc).__name__}: {exc}"}, "failed"
        checks.append({"name": name, "required": required, "status": status,
                       "seconds": round(time.perf_counter() - started, 2), "detail": detail})

    def need(*keys):
        missing = [k for k in keys if k not in ctx]
        if missing:
            raise Skip(f"depende de: {', '.join(missing)}")
        return [ctx[k] for k in keys]

    def transaction():
        tx = rpc.transaction(signature)
        signers = observe(tx, WSOL)["signers"]
        if not signers:
            raise AssertionError("la transacción auditada no tiene firmante")
        ctx["tx"], ctx["signer"], ctx["wallet"] = tx, signers[0], wallet or signers[0]
        return {"signature": signature, "slot": tx["slot"], "block_time": tx.get("blockTime"),
                "payload_sha256": digest(tx), "signer": signers[0], "wallet": ctx["wallet"]}

    def digest_stable():
        [tx] = need("tx")
        again = rpc.transaction(signature)
        assert digest(again) == digest(tx), "dos descargas finalized del mismo bloque difieren"
        return {"payload_sha256": digest(again)}

    def normalization():
        tx, signer = need("tx", "signer")
        events = normalize(tx, signer)
        problems = {e["event_index"]: p for e in events if (p := _event_problems(e))}
        assert not problems, f"eventos inválidos: {problems}"
        return {"normalizer": NORMALIZER_VERSION, "events": [_event(e) for e in events],
                "note": None if events else "la transacción auditada no mueve tokens de su firmante"}

    def live_swaps():
        """The latest successful transactions of each sampled venue, normalized for their fee payer."""
        programs = {name: program for program, name in VENUES.items()}
        venues, invalid, verified = {}, {}, 0
        for name in SAMPLED_VENUES:
            listed = rpc.signatures(programs[name], limit=max(20, 3 * per_venue))
            stats = {"sampled": [], "events": 0, "verified": 0, "kinds": {}, "quotes": {}, "issues": {}, "tips": 0}
            for item in [i for i in listed if not i.get("err")][:per_venue]:
                tx = rpc.transaction(item["signature"])
                payer = observe(tx, WSOL)["signers"][0]
                events = normalize(tx, payer)
                stats["sampled"].append(item["signature"])
                for e in events:
                    stats["events"] += 1
                    stats["verified"] += e["trade"] == "verified"
                    stats["tips"] += e["tip_sol"] > 0
                    for key, value in (("kinds", e["kind"]), ("quotes", e["quote_asset"] or "none")):
                        stats[key][value] = stats[key].get(value, 0) + 1
                    for issue in e["issues"]:
                        stats["issues"][issue] = stats["issues"].get(issue, 0) + 1
                    if problems := _event_problems(e):
                        invalid[f"{item['signature']}#{e['event_index']}"] = problems
            verified += stats["verified"]
            venues[name] = stats
        assert not invalid, f"eventos inválidos: {invalid}"
        assert verified, "ninguna compra/venta verificada en las transacciones recientes de los venues muestreados"
        return {"venues": venues, "verified_trades": verified}

    def migrations():
        app = create_app(db_url, rpc=rpc, prices=prices, quotes=quotes)
        ctx["app"] = app
        current, head = current_revision(app.state.engine), head_revision()
        assert current == head, f"esquema en {current}, se esperaba {head}"
        return {"revision": current, "dialect": app.state.engine.dialect.name}

    def ingestion():
        app, wallet = need("app", "wallet")
        client = ctx["client"] = TestClient(app).__enter__()
        created = client.post("/api/wallets", json={"address": wallet, "label": "Smoke test (datos reales)"})
        assert created.status_code in (201, 409), created.text
        request = {"max_pages": pages, "page_size": page_size, "days": 90}
        first = client.post(f"/api/wallets/{wallet}/sync", json=request).json()
        assert first.get("transactions", 0) > 0 or first.get("already_known", 0) > 0, \
            f"no se ingirió ninguna transacción: {first.get('issues')}"
        again = client.post(f"/api/wallets/{wallet}/sync", json=request).json()
        # An active wallet gets new transactions between the two syncs: only those may be downloaded.
        assert again["rpc_transaction_fetches"] <= again["transactions"], \
            "la resincronización volvió a descargar transacciones guardadas"
        observations = client.get(f"/api/wallets/{wallet}/observations").json()["total"]
        assert observations == first["transactions"] + again["transactions"], "observaciones duplicadas"
        ctx["sync"] = first
        keys = ("state", "transactions", "already_known", "rpc_transaction_fetches", "trades", "issues")
        return {"first": {k: first.get(k) for k in keys}, "incremental_resync": {k: again.get(k) for k in keys},
                "observations": observations}

    def chain_trades():
        client, wallet = need("client", "wallet")
        page = client.get(f"/api/wallets/{wallet}/trades", params={"limit": 200}).json()
        items = page["items"]
        summary = {"trades": page["total"], "summarized": len(items), "by_kind": {}, "by_evidence": {}, "issues": {},
                   "venues": {}}
        for t in items:
            for key, value in (("by_kind", t["kind"]), ("by_evidence", t["evidence"]["trade"])):
                summary[key][value] = summary[key].get(value, 0) + 1
            for issue in t.get("issues") or []:
                summary["issues"][issue] = summary["issues"].get(issue, 0) + 1
            for venue in t.get("venues") or []:
                summary["venues"][venue] = summary["venues"].get(venue, 0) + 1
        ctx["mints"] = sorted({t["mint"] for t in items if t["kind"] in ("buy", "sell")})
        return summary

    def sol_usd():
        [tx] = need("tx")
        moment = tx.get("blockTime")
        if not moment:
            raise Skip("la transacción auditada no tiene blockTime")
        candles = prices.minute_candles(moment - 180, moment + 60)
        assert candles, "sin velas SOL/USD alrededor de la transacción auditada"
        closes = [c["close"] for c in candles]
        assert all(Decimal(1) < c < Decimal(100000) for c in closes), f"precio fuera de rango: {closes}"
        return {"source": getattr(prices, "source", None), "candles": len(candles), "close": str(closes[-1])}

    def pricing():
        [report] = need("sync")
        result = {"pricing": report.get("pricing"), "mints": report.get("mints")}
        for key in ("pricing", "mints"):
            assert not (report.get(key) or {}).get("error"), f"{key}: {report[key]['error']}"
        return result

    def analysis():
        client, wallet = need("client", "wallet")
        result = client.get(f"/api/wallets/{wallet}/analysis").json()
        assert not result["eligible"], "una sync de pocas páginas no puede validar una wallet"
        ranking = client.get("/api/rankings")
        assert ranking.status_code == 200, ranking.text
        return {"snapshot_id": result["snapshot_id"], "exclusion_reasons": result["exclusion_reasons"],
                "ranking_total": ranking.json()["total"]}

    def current_quotes():
        mints = ctx.get("mints") or []
        if not mints:
            raise Skip("la sync no encontró tokens comprados o vendidos")
        found = ctx["quotes"] = quotes.quotes(mints[:30])
        return {"requested": len(mints[:30]), "found": len(found),
                "sample": {m: {k: str(v) if isinstance(v, Decimal) else v for k, v in q.items()
                               if k in ("price_usd", "liquidity_usd", "market_cap_usd", "dex_id", "symbol")}
                           for m, q in list(found.items())[:3]}}

    def pool_candles():
        mints = ctx.get("mints") or []
        if not mints:
            raise Skip("la sync no encontró tokens comprados o vendidos")
        for mint in mints[:5]:
            pool = history.best_pool(mint)
            if pool is None:
                continue
            now = int(time.time())
            rows = history.candles(pool["pool"], "hour", now, limit=24)
            result = {"mint": mint, "pool": pool["pool"], "quote_mint": pool.get("quote_mint"), "dex": pool.get("dex"),
                      "hour_candles": len(rows), "last_close": str(rows[-1]["close"]) if rows else None}
            # Cross-check: candles are requested in the quote token (SOL), so close × SOL/USD must match the
            # DEX Screener USD price of the same token within a factor of two.
            usd = ((ctx.get("quotes") or {}).get(mint) or {}).get("price_usd")
            minutes = history.candles(pool["pool"], "minute", now, limit=5)
            try:
                sol = prices.minute_candles(now - 900, now)
            except ProviderError:
                sol = None  # already reported by sol_usd
            if pool.get("quote_mint") == WSOL and usd and minutes and sol:
                implied = minutes[-1]["close"] * sol[-1]["close"]
                ratio = implied / Decimal(str(usd))
                result.update(implied_usd=str(implied), dexscreener_usd=str(usd), ratio=str(round(ratio, 3)))
                assert Decimal("0.5") <= ratio <= 2, f"vela × SOL/USD no cuadra con DEX Screener: {result}"
            return result
        raise AssertionError(f"ningún pool para {len(mints[:5])} tokens operados")

    def jito_accounts():
        live = set(jito())
        assert live, "getTipAccounts devolvió una lista vacía"
        return {"designated": len(live), "known": len(JITO_TIP_ACCOUNTS), "match": live == set(JITO_TIP_ACCOUNTS),
                "missing_here": sorted(live - JITO_TIP_ACCOUNTS), "not_designated": sorted(JITO_TIP_ACCOUNTS - live)}

    try:
        check("rpc_transaction", True, transaction)
        check("rpc_digest_stable", True, digest_stable)
        check("normalization", True, normalization)
        check("live_swaps", True, live_swaps)
        check("migrations", True, migrations)
        check("ingestion", True, ingestion)
        check("chain_trades", True, chain_trades)
        check("sol_usd", True, sol_usd)
        check("pricing_and_mint_facts", True, pricing)
        check("analysis_and_ranking", True, analysis)
        check("current_quotes", False, current_quotes)
        check("pool_candles", False, pool_candles)
        check("jito_tip_accounts", False, jito_accounts)
    finally:
        if "client" in ctx:
            ctx["client"].__exit__(None, None, None)
        if "app" in ctx:
            ctx["app"].state.engine.dispose()
    failed = [c["name"] for c in checks if c["required"] and c["status"] != "ok"]
    return {"real_data": True, "ok": not failed, "failed_required": failed,
            "warnings": [c["name"] for c in checks if not c["required"] and c["status"] != "ok"],
            "verified_performance": False, "checks": checks}


def main(argv=None) -> int:
    from .adapters.geckoterminal import GeckoTerminal
    from .adapters.prices import CoinbaseCandles
    from .adapters.quotes import DexScreenerQuotes
    from .adapters.solana import SolanaRPC

    parser = argparse.ArgumentParser(description="Smoke test contra la red real (sin datos sintéticos).")
    parser.add_argument("--database-url", help="por defecto, un SQLite temporal")
    parser.add_argument("--signature", default=AUDITED_SIGNATURE)
    parser.add_argument("--wallet", help="wallet a sincronizar (por defecto, el firmante de la transacción auditada)")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=5)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="fomo-live-smoke-") as temp:
        report = run(SolanaRPC(), CoinbaseCandles(), DexScreenerQuotes(), GeckoTerminal(),
                     db_url=args.database_url or f"sqlite:///{temp}/live.db", signature=args.signature,
                     wallet=args.wallet, pages=args.pages, page_size=args.page_size)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
