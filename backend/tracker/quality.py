"""Data quality over the tracked wallets: what the tracker sees, what it cannot, and why. Local data only.

`GET /api/quality` and `python -m tracker.quality` report, for every tracked wallet (or the ones given):
how much of the 90-day window is synced, how many trades are verified and why the rest are not, which
unrecognized programs move the wallets' tokens (venues worth adding), how much is valued in USD and with
a verified entry market cap, and why positions and wallets do not reach the ranking.
"""

import argparse
import json
from collections import Counter

from sqlalchemy import select

from .db import Observation, RankingEntry, RawTransaction, SyncState, Trade, Wallet
from .normalize import VENUES, _programs
from .ranking import DEFAULT_KEY, refresh_stale
from .service import default_as_of, history_complete

# Programs every transaction uses for bookkeeping; never a trading venue.
BOOKKEEPING = {
    "11111111111111111111111111111111",  # System
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token Account
    "ComputeBudget111111111111111111111111111111",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",  # Memo v2
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",  # Memo v1
}
UNKNOWN_PROGRAM_SAMPLE = 500  # unrecognized-venue transactions inspected for their programs


def _top(counter: Counter, n: int) -> dict:
    return dict(counter.most_common(n))


def quality(session, wallets: list[str] | None = None, *, days: int = 90, top: int = 15) -> dict:
    addresses = wallets or list(session.scalars(select(Wallet.address).order_by(Wallet.address)))
    report = {"wallets": len(addresses), "window_days": days}

    sync = Counter()
    for address in addresses:
        state = session.get(SyncState, address)
        if state is None:
            sync["never_synced"] += 1
        elif history_complete(state, default_as_of(session, address), days):
            sync["complete"] += 1
        else:
            sync["partial"] += 1
    report["sync"] = dict(sync)

    statuses = Counter(d.get("status") for (d,) in session.execute(
        select(Observation.details).where(Observation.wallet.in_(addresses))))
    report["transactions"] = {"observed": sum(statuses.values()), "failed": statuses.get("failed", 0)}

    kinds, evidence, quotes, venues, issues = Counter(), Counter(), Counter(), Counter(), Counter()
    usd, caps = Counter(), Counter()
    unknown_venue = []
    with_trades = set()
    for signature, kind, details in session.execute(
            select(Trade.signature, Trade.kind, Trade.details).where(Trade.wallet.in_(addresses))):
        with_trades.add(signature)
        ev = details["evidence"]
        kinds[kind] += 1
        evidence[ev["trade"]] += 1
        issues.update(details.get("issues") or [])
        if kind in ("buy", "sell"):
            quotes[details.get("quote_asset") or "none"] += 1
            venues.update(details.get("venues") or ["none"])
            usd[ev.get("usd", "unknown")] += 1
            if kind == "buy":
                caps[ev.get("market_cap", "unknown")] += 1
        if "no_recognized_venue" in (details.get("issues") or []):
            unknown_venue.append(signature)
    report["transactions"]["with_token_movement"] = len(with_trades)
    report["trades"] = {"total": sum(kinds.values()), "by_kind": dict(kinds), "by_evidence": dict(evidence),
                        "quote_assets": dict(quotes), "venues": _top(venues, top), "issues": dict(issues)}
    report["valuation"] = {"buy_sell_usd": dict(usd), "buy_market_cap": dict(caps)}

    programs = Counter()
    sample = list(dict.fromkeys(unknown_venue))[:UNKNOWN_PROGRAM_SAMPLE]
    for (payload,) in session.execute(select(RawTransaction.payload).where(RawTransaction.signature.in_(sample))):
        programs.update(p for p in _programs(payload) if p not in VENUES and p not in BOOKKEEPING)
    report["unrecognized_programs"] = {"transactions_inspected": len(sample), "programs": _top(programs, top)}

    # Default-filter analysis, from the precomputed entries (recomputed here if outdated).
    refresh_stale(session)
    entries = session.execute(select(RankingEntry.eligible, RankingEntry.summary, RankingEntry.result)
                              .where(RankingEntry.filters_key == DEFAULT_KEY, RankingEntry.wallet.in_(addresses))).all()
    reasons, behavior, position_issues, positions = Counter(), Counter(), Counter(), Counter()
    for eligible, summary, result in entries:
        reasons.update(summary["exclusion_reasons"])
        behavior[summary["behavior"]] += 1
        m = summary["metrics"]
        positions.update(closed=m["closed_positions"], open=m["open_positions"],
                         evaluable=m["evaluable_positions"], excluded=m["excluded_positions"])
        for p in result["positions"]:
            if p["status"] == "closed" and not p["evaluable"]:
                position_issues.update(p["issues"] or ["no_roi"])
    report["analysis"] = {"eligible": sum(e for e, _, _ in entries), "exclusion_reasons": dict(reasons),
                          "behavior": dict(behavior), "positions": dict(positions),
                          "closed_not_evaluable_by_issue": dict(position_issues)}
    return report


def main(argv=None) -> int:
    from .db import database

    parser = argparse.ArgumentParser(description="Calidad de los datos de las wallets seguidas (sin red).")
    parser.add_argument("wallets", nargs="*", help="por defecto, todas")
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args(argv)
    _, sessions = database()
    with sessions() as session:
        print(json.dumps(quality(session, args.wallets or None, days=args.days), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
