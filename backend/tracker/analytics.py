"""Deterministic weighted-average accounting and conservative research metrics.

Closed flat-to-flat cycles are positions; partial exits never count as independent wins.
Win-rate confidence uses token-cluster outcomes to prevent repeated trades of one token
from artificially increasing effective sample size. Correlation between tokens remains a limitation.
"""

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import mean, median

from .schemas import Filters

ZERO = Decimal(0)
VERSION = "wac-wilson-token-v1"


@dataclass
class Fill:
    signature: str
    event_index: int
    token: str
    timestamp: datetime
    kind: str
    quantity: Decimal
    gross_usd: Decimal | None
    fees_usd: Decimal | None
    market_cap_usd: Decimal | None
    source_url: str
    evidence: dict = field(default_factory=dict)


@dataclass
class Position:
    token: str
    cycle: int
    opened_at: datetime
    closed_at: datetime | None = None
    quantity: Decimal = ZERO
    basis: Decimal = ZERO
    allocated_cost: Decimal = ZERO
    proceeds: Decimal = ZERO
    money_known: bool = True
    issues: set = field(default_factory=set)
    entries: list[Decimal | None] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)

    @property
    def pnl(self):
        return self.proceeds - self.allocated_cost if self.money_known else None

    @property
    def roi(self):
        return self.pnl / self.allocated_cost * 100 if self.pnl is not None and self.allocated_cost else None

    @property
    def evaluable(self):
        return self.closed_at is not None and not self.issues and self.roi is not None

    @property
    def verified(self):
        return bool(self.fills) and all(
            f.evidence.get("trade") == "verified" and f.evidence.get("usd") == "verified"
            for f in self.fills
        )

    @property
    def cap_verified(self):
        return bool(self.entries) and all(c is not None for c in self.entries) and all(
            f.evidence.get("market_cap") == "verified" for f in self.fills if f.kind == "buy"
        )


def reconstruct(fills: list[Fill], as_of: datetime) -> list[Position]:
    current: dict[str, Position] = {}
    counts: dict[str, int] = defaultdict(int)
    output: list[Position] = []
    # Signature order breaks timestamp ties deterministically, but cannot establish intrablock order.
    # A tied timestamp for the same mint across signatures is marked ambiguous below.
    timestamps = defaultdict(set)
    for f in fills:
        if f.timestamp <= as_of:
            timestamps[(f.token, f.timestamp)].add(f.signature)
    for f in sorted((f for f in fills if f.timestamp <= as_of),
                    key=lambda f: (f.timestamp, f.signature, f.event_index, f.token)):
        p = current.get(f.token)
        if p is None:
            counts[f.token] += 1
            p = Position(f.token, counts[f.token], f.timestamp)
            output.append(p)
            current[f.token] = p
        p.fills.append(f)
        if len(timestamps[(f.token, f.timestamp)]) > 1:
            p.issues.add("ambiguous_transaction_order")
        if f.evidence.get("movement") in ("mismatch", "unavailable"):
            p.issues.add("chain_evidence_mismatch")
        if f.kind in ("transfer_in", "transfer_out"):
            p.issues.add("transfer_unknown_cost_or_disposal")
        if f.kind in ("buy", "transfer_in"):
            p.quantity += f.quantity
            if f.kind == "buy":
                p.entries.append(f.market_cap_usd)
                if f.gross_usd is None or f.fees_usd is None:
                    p.money_known = False
                    p.issues.add("missing_historical_usd_or_fees")
                else:
                    p.basis += f.gross_usd + f.fees_usd
        else:
            if p.quantity < f.quantity:
                p.issues.add("missing_opening_inventory")
                p.money_known = False
                # Preserve oversells as an invalid position; never fabricate an opening lot.
                p.quantity = ZERO
            else:
                allocated = p.basis * f.quantity / p.quantity
                p.basis -= allocated
                p.quantity -= f.quantity
                if f.kind == "sell":
                    p.allocated_cost += allocated
                    if f.gross_usd is None or f.fees_usd is None:
                        p.money_known = False
                        p.issues.add("missing_historical_usd_or_fees")
                    else:
                        p.proceeds += f.gross_usd - f.fees_usd
            if p.quantity == 0:
                p.closed_at = f.timestamp
                del current[f.token]
    return output


def wilson(wins: int, total: int) -> tuple[float | None, float | None]:
    if not total:
        return None, None
    z = 1.959963984540054
    p = wins / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def num(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def position_json(p: Position, threshold: Decimal) -> dict:
    return {
        "token": p.token, "cycle": p.cycle, "opened_at": p.opened_at.isoformat(),
        "closed_at": p.closed_at.isoformat() if p.closed_at else None,
        "status": "closed" if p.closed_at else "open", "remaining_quantity": str(p.quantity),
        "remaining_cost_usd": num(p.basis) if p.money_known else None,
        "realized_cost_usd": num(p.allocated_cost) if p.money_known else None,
        "realized_proceeds_usd": num(p.proceeds) if p.money_known else None,
        "realized_pnl_usd": num(p.pnl), "realized_roi_pct": num(p.roi),
        "unrealized_pnl_usd": None, "entry_market_caps_usd": [num(x) for x in p.entries],
        "evaluable": p.evaluable, "verified": p.verified, "market_cap_verified": p.cap_verified,
        "early_entry_reported": bool(p.entries) and all(c is not None and c <= threshold for c in p.entries),
        "issues": sorted(p.issues),
        "fills": [{"signature": f.signature, "event_index": f.event_index, "kind": f.kind,
                   "timestamp": f.timestamp.isoformat(), "quantity": str(f.quantity),
                   "gross_usd": num(f.gross_usd), "fees_usd": num(f.fees_usd),
                   "market_cap_usd": num(f.market_cap_usd), "source_url": f.source_url,
                   "transaction_url": f"https://solscan.io/tx/{f.signature}", "evidence": f.evidence}
                  for f in p.fills],
    }


def analyze(wallet: str, fills: list[Fill], filters: Filters, as_of: datetime,
            history_complete: bool = False) -> dict:
    since = as_of - timedelta(days=filters.analysis_days)
    all_positions = reconstruct(fills, as_of)
    positions = [p for p in all_positions if (p.closed_at or p.fills[-1].timestamp) >= since]
    evaluated = [p for p in positions if p.evaluable]
    n = len(evaluated)
    wins = sum(p.pnl > 0 for p in evaluated)
    big = [p for p in evaluated if p.roi >= filters.min_realized_roi_pct]
    super_wins = [p for p in evaluated if p.roi >= 1000]
    early = [p for p in evaluated if p.entries and all(
        c is not None and c <= filters.max_entry_market_cap_usd for c in p.entries)]
    early_big = [p for p in big if p in early]
    # Aggregate repeated flat-to-flat cycles of each mint before estimating confidence.
    groups = defaultdict(list)
    for p in evaluated:
        groups[p.token].append(p)
    effective_n = len(groups)
    token_wins = sum(sum(p.pnl for p in group) > 0 for group in groups.values())
    low, high = wilson(token_wins, effective_n)
    pnls = [p.pnl for p in evaluated]
    profit = sum((v for v in pnls if v > 0), ZERO)
    loss = -sum((v for v in pnls if v < 0), ZERO)
    total_cost = sum((p.allocated_cost for p in evaluated), ZERO)
    pnl = sum(pnls, ZERO) if n else None
    rois = [p.roi for p in evaluated]
    holds = [(p.closed_at - p.opened_at).total_seconds() for p in evaluated]
    caps = [c for p in evaluated for c in p.entries if c is not None]
    concentration = float(max(pnls) / profit) if profit else None
    weekly = defaultdict(lambda: ZERO)
    equity, peak, drawdown = ZERO, ZERO, ZERO
    curve = []
    for p in sorted(evaluated, key=lambda p: (p.closed_at, p.token, p.cycle)):
        equity += p.pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        week = (p.closed_at - timedelta(days=p.closed_at.weekday())).date().isoformat()
        weekly[week] += p.pnl
        curve.append({"timestamp": p.closed_at.isoformat(), "cumulative_realized_pnl_usd": str(equity)})
    first_monday = since.date() - timedelta(days=since.weekday())
    week_count = (as_of.date() - first_monday).days // 7 + 1
    # Missing/inactive weeks contribute zero; they cannot be dropped to inflate consistency.
    consistency = sum(v > 0 for v in weekly.values()) / week_count
    verified = sum(p.verified and p.cap_verified for p in evaluated)
    coverage = verified / n if n else 0.0
    big_bound = wilson(sum(any(p in big for p in g) for g in groups.values()), effective_n)[0] or 0
    super_bound = wilson(sum(any(p in super_wins for p in g) for g in groups.values()), effective_n)[0] or 0
    early_bound = wilson(sum(all(p in early for p in g) for g in groups.values()), effective_n)[0] or 0
    shrinkage = effective_n / (effective_n + 30)
    risk_return = max(0, min(1, float((pnl or ZERO) / total_cost) if total_cost else 0)) * shrinkage
    parts = {"temporal_consistency": round(25 * consistency * shrinkage, 4),
             "big_winner_frequency": round(20 * big_bound, 4),
             "roi_1000_frequency": round(15 * super_bound, 4),
             "net_risk_adjusted_return": round(15 * risk_return / (1 + float(drawdown / total_cost)
                                                                  if total_cost else 1), 4),
             "early_entry": round(10 * early_bound, 4),
             "replicability": 0.0, "data_quality": round(5 * coverage * int(history_complete), 4)}
    penalty = min(0.5, max(0, ((concentration or 0) - 0.5)))
    reasons = []
    if n < filters.min_evaluable_positions:
        reasons.append("insufficient_evaluable_positions")
    if not n or wins / n < filters.min_win_rate:
        reasons.append("observed_win_rate_below_threshold")
    if filters.conservative_win_rate and (low is None or low < filters.min_win_rate):
        reasons.append("wilson_lower_bound_below_threshold")
    if not early_big:
        reasons.append("no_early_realized_big_winner")
    if coverage < 1:
        reasons.append("unverified_trades_usd_or_historical_market_cap")
    if not history_complete:
        reasons.append("incomplete_history")
    if any(p.issues for p in positions):
        reasons.append("unresolved_inventory_or_transfer_risk")
    if any(p.closed_at is not None and not p.evaluable for p in positions):
        reasons.append("incomplete_position_coverage")
    if not n:
        reasons.append("no_evaluable_closed_positions")
    result = {
        "method_version": VERSION, "wallet": wallet, "as_of": as_of.isoformat(),
        "window_start": since.isoformat(), "filters": filters.model_dump(mode="json"),
        "eligible": not reasons, "exclusion_reasons": reasons,
        "evidence_level": "verified" if not reasons else "insufficient",
        "history_complete": history_complete,
        "score": {"historical_skill_provisional": round(sum(parts.values()) * (1 - penalty), 2) if n else None,
                  "verified_smart_money": round(sum(parts.values()) * (1 - penalty), 2) if not reasons else None,
                  "components": parts, "weights": [25, 20, 15, 15, 10, 10, 5],
                  "concentration_penalty_fraction": round(penalty, 4),
                  "copy_risk": "unknown", "replicability": "not_measured"},
        "metrics": {
            "closed_positions": sum(p.closed_at is not None for p in positions),
            "open_positions": sum(p.closed_at is None for p in positions),
            "evaluable_positions": n, "effective_sample_tokens": effective_n,
            "excluded_positions": sum(p.closed_at is not None and not p.evaluable for p in positions),
            "wins": wins, "losses": sum(p.pnl < 0 for p in evaluated),
            "break_even": sum(p.pnl == 0 for p in evaluated),
            "win_rate": wins / n if n else None,
            "token_cluster_win_rate": token_wins / effective_n if effective_n else None,
            "win_rate_ci95": [low, high], "big_winners": len(big), "early_big_winners": len(early_big),
            "big_winner_rate": len(big) / n if n else None,
            "roi_1000_rate": len(super_wins) / n if n else None,
            "median_roi_pct": num(median(rois)) if n else None,
            "mean_roi_pct": num(mean(rois)) if n else None,
            "net_realized_pnl_usd": num(pnl),
            "aggregate_realized_roi_pct": num(pnl / total_cost * 100) if n and total_cost else None,
            "profit_factor": num(profit / loss) if loss else None,
            "profit_factor_status": "finite" if loss else ("no_losses_observed" if n else "unknown"),
            "mean_hold_seconds": mean(holds) if holds else None,
            "median_hold_seconds": median(holds) if holds else None,
            "median_entry_market_cap_usd": num(median(caps)) if caps else None,
            "top_profit_share": concentration, "max_realized_drawdown_usd": str(drawdown) if n else None,
            "verified_data_fraction": coverage,
        },
        "weekly_realized_pnl": [{"week": k, "pnl_usd": str(v)} for k, v in sorted(weekly.items())],
        "realized_pnl_curve": curve,
        "positions": [position_json(p, filters.max_entry_market_cap_usd) for p in positions],
        "limitations": ["No se mide rentabilidad no realizada ni replicabilidad sin precios de ejecución.",
                        "Wilson usa resultados agregados por token; la correlación entre tokens puede persistir.",
                        "Las métricas declaradas son exploratorias hasta verificar USD, fees, capitalización e historial.",
                        "La curva es PnL realizado acumulado, no el valor total del portafolio."],
    }
    # Include every consumed fact and provenance, not only resulting aggregates, in snapshot identity.
    canonical = {"result": result, "inputs": [{**vars(f), "timestamp": f.timestamp.isoformat()}
                                               for f in sorted(fills, key=lambda f: (
                                                   f.timestamp, f.signature, f.event_index, f.token))
                                               if f.timestamp <= as_of]}
    result["snapshot_id"] = hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()
    return result
