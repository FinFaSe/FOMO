"""Our normalizer against an independent parser, on the same real transactions.

`python -m tracker.crosscheck [--pages N] [--per-pool N] [--venues]` samples trending pools, takes GeckoTerminal's
parsed trades of each one, downloads every transaction from the RPC and normalizes it for the signer that owns
the token change (terminals often pay the fee through a relayer, and GeckoTerminal names the fee payer).

Outcomes per trade:
- `agree`: same side and exactly the same token amount. The quote is compared separately: our amount is what the
  wallet paid or received (venue and service fees included), GeckoTerminal's is the pool leg.
- `split_route`: the wallet's trade went through several pools; GeckoTerminal reports only this pool's part.
- `not_trader`: no signer owns the token change, or the signer only kept a fee or arbitrage residue (vaults,
  order executors, MEV); there is no trade to compare for a signer.
- `unverified`: our trade is not `verified` (token-for-token, unrecognized venue, ...); issues and unrecognized
  programs are reported.
- `disagree`: different side or amount on a single-pool trade. Each one is listed: a normalizer bug candidate.

`--venues` also checks that every recognized pool program owns the pools GeckoTerminal lists for its DEX.
"""

import argparse
import json
import statistics
import sys
from collections import Counter
from decimal import Decimal

from .adapters.solana import ProviderError
from .normalize import GECKO_DEXES, STABLES, VENUES, WSOL, _programs, normalize

BOOKKEEPING = {"11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
               "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
               "ComputeBudget111111111111111111111111111111"}


def _token_deltas(tx: dict, mint: str) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for field, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in (tx.get("meta") or {}).get(field) or []:
            if b.get("mint") == mint and b.get("owner"):
                q = b["uiTokenAmount"]
                out[b["owner"]] = out.get(b["owner"], Decimal(0)) + sign * Decimal(q["amount"]) / Decimal(10) ** q["decimals"]
    return {k: v for k, v in out.items() if v}


def compare(tx: dict, trade: dict) -> dict:
    """Classify one GeckoTerminal trade against our normalization of the same transaction."""
    buy = trade["kind"] == "buy"
    base, quote = (trade["to_token"], trade["from_token"]) if buy else (trade["from_token"], trade["to_token"])
    gt_base, gt_quote = (trade["to_amount"], trade["from_amount"]) if buy else (trade["from_amount"], trade["to_amount"])
    keys = tx["transaction"]["message"]["accountKeys"]
    signers = [k["pubkey"] for k in keys if isinstance(k, dict) and k.get("signer")]
    deltas = _token_deltas(tx, base)
    owners = [s for s in signers if s in deltas]
    trader = trade["signer"] if trade["signer"] in owners else (owners[0] if owners else None)
    row = {"tx": trade["tx"], "kind": trade["kind"], "quote": "SOL" if quote == WSOL else STABLES.get(quote, "token"),
           "relayed": trader is not None and bool(signers) and trader != signers[0]}
    if trader is None:
        return {**row, "outcome": "not_trader"}
    event = next((e for e in normalize(tx, trader) if e["mint"] == base), None)
    if event is None or event["kind"] not in ("buy", "sell"):
        return {**row, "outcome": "not_trader" if event is not None else "disagree",
                "ours": event["kind"] if event else None}
    if event["trade"] != "verified":
        return {**row, "outcome": "unverified", "issues": event["issues"],
                "programs": [p for p in _programs(tx) if p not in VENUES and p not in BOOKKEEPING]}
    other_pools = sum(1 for owner in deltas if owner != trader)
    if event["kind"] != trade["kind"]:
        return {**row, "outcome": "disagree", "ours": event["kind"]}
    if event["token_amount"] != gt_base:
        return {**row, "outcome": "split_route" if other_pools > 1 else "disagree",
                "token_ratio": str(event["token_amount"] / gt_base)}
    row["outcome"] = "agree"
    if event["quote_asset"] == "SOL" and quote == WSOL:
        row["quote_diff"] = float(event["quote_amount"] / gt_quote - 1)
    elif event["quote_asset"] in ("USDC", "USDT") and trade.get("volume_usd"):
        row["quote_diff"] = float(event["quote_amount"] / trade["volume_usd"] - 1)
    return row


def _sample(trades: list[dict], n: int) -> list[dict]:
    """Up to n trades spread over the list, skipping transactions with several trades in this pool."""
    counts = Counter(t["tx"] for t in trades)
    single = [t for t in trades if counts[t["tx"]] == 1]
    step = max(1, len(single) // max(1, n))
    return single[::step][:n]


def crosscheck(rpc, gecko, *, pools: list[dict] | None = None, pages: int = 1, per_pool: int = 6,
               max_pools: int | None = None) -> dict:
    if pools is None:
        pools = [p for page in range(1, pages + 1) for p in gecko.trending_pools(page)]
    pools = pools[:max_pools] if max_pools else pools
    rows = []
    for pool in pools:
        try:
            sample = _sample(gecko.pool_trades(pool["pool"]), per_pool)
        except ProviderError:
            continue
        fetched = rpc.transactions([t["tx"] for t in sample]) if hasattr(rpc, "transactions") else {}
        for trade in sample:
            tx = fetched.get(trade["tx"])
            try:
                tx = tx if isinstance(tx, dict) else rpc.transaction(trade["tx"])
            except ProviderError as exc:
                rows.append({"tx": trade["tx"], "dex": pool["dex"], "outcome": "rpc_error", "error": exc.message})
                continue
            rows.append({**compare(tx, trade), "dex": pool["dex"], "pool": pool["pool"]})
    return summarize(rows)


def summarize(rows: list[dict]) -> dict:
    outcomes = Counter(r["outcome"] for r in rows)
    diffs = sorted(abs(r["quote_diff"]) for r in rows if "quote_diff" in r)
    checked = sum(outcomes[k] for k in ("agree", "split_route", "disagree"))
    programs = Counter(p for r in rows if r["outcome"] == "unverified" for p in r.get("programs") or [])
    return {
        "trades": len(rows), "outcomes": dict(outcomes),
        "by_dex": {dex: dict(Counter(r["outcome"] for r in rows if r.get("dex") == dex))
                   for dex in sorted({r.get("dex") for r in rows if r.get("dex")})},
        "agreement": round(outcomes["agree"] / (outcomes["agree"] + outcomes["disagree"]), 4)
        if outcomes["agree"] + outcomes["disagree"] else None,
        "checked_single_pool": checked - outcomes["split_route"],
        "relayed": sum(1 for r in rows if r.get("relayed")),
        "quote_diff_abs": {"n": len(diffs), "median": round(statistics.median(diffs), 5) if diffs else None,
                           "p90": round(diffs[int(0.9 * (len(diffs) - 1))], 5) if diffs else None,
                           "within_2pct": sum(d <= 0.02 for d in diffs)},
        "unverified_issues": dict(Counter(i for r in rows if r["outcome"] == "unverified" for i in r.get("issues") or [])),
        "unverified_programs": dict(programs.most_common(10)),
        "disagreements": [r for r in rows if r["outcome"] == "disagree"],
    }


def venue_programs(rpc, gecko, dexes: dict[str, set[str]] = GECKO_DEXES, per_dex: int = 20) -> dict:
    """For each DEX, the programs owning the pools GeckoTerminal lists, against the venues we recognize."""
    programs = {name: program for program, name in VENUES.items()}
    out = {}
    for dex, names in dexes.items():
        pools = gecko.dex_pools(dex)[:per_dex]
        owners = Counter()
        for i in range(0, len(pools), 100):
            for account in rpc.accounts(pools[i:i + 100], encoding="base64", data_slice={"offset": 0, "length": 0}):
                owners[account["owner"] if account else None] += 1
        expected = {programs[n] for n in names}
        out[dex] = {"pools": len(pools), "owners": dict(owners),
                    "ok": bool(pools) and set(owners) <= expected}
    return out


def main(argv=None) -> int:
    from .adapters.geckoterminal import GeckoTerminal
    from .adapters.solana import SolanaRPC

    parser = argparse.ArgumentParser(description="Normalizador frente a las operaciones de GeckoTerminal.")
    parser.add_argument("--pages", type=int, default=1, help="páginas de pools en tendencia (20 por página)")
    parser.add_argument("--per-pool", type=int, default=6)
    parser.add_argument("--venues", action="store_true", help="comprobar también los programas de cada DEX")
    args = parser.parse_args(argv)
    rpc, gecko = SolanaRPC(), GeckoTerminal()
    report = {"trades": crosscheck(rpc, gecko, pages=args.pages, per_pool=args.per_pool)}
    if args.venues:
        report["venues"] = venue_programs(rpc, gecko)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0 if not report["trades"]["disagreements"] and all(
        v["ok"] for v in report.get("venues", {}).values()) else 1


if __name__ == "__main__":
    sys.exit(main())
