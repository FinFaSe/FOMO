"""Copy-trading backtests, in two strictly separated forms.

1. Historical reconstruction (`historical_copy`): a follower copies each leader trade `delay` seconds later and
   fills at the first price observed in the database (any wallet's trade or a scanned early buy of that mint) at or
   after that moment, within `max_staleness_seconds`; otherwise the copy is counted as unfillable, never priced by
   assumption. Decisions only use the leader's trade itself; exits mirror the leader's sells proportionally.
   Observations are sparse and biased towards what was stored, so this is a reconstruction, not a forward test.
   Prices are in SOL per token (SOL-quoted trades only); delay 0 is the unreachable ideal of the leader's price.

2. Prospective tracking (`prospective_report`): every buy/sell alert records the market quote available when it
   was raised and again 5 and 60 minutes later (`alert_quotes`). Only those recorded quotes are used, so there is
   no hindsight; it becomes meaningful as alerts accumulate.
"""

import bisect
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import median

from sqlalchemy import select

from .adapters.solana import ProviderError
from .db import Alert, AlertQuote, EarlyBuy, PricePoint, Trade, Wallet, now_iso
from .history import candle_index, covering_candle
from .normalize import fee_sol

ONE = Decimal(1)
ZERO = Decimal(0)
QUOTE_OFFSETS = (0, 300, 3600)
MAX_QUOTE_ATTEMPTS = 5


def _text(value):
    return str(value) if value is not None else None


@dataclass
class Observation:
    time: int
    slot: int
    price: Decimal


@dataclass
class FollowerPosition:
    mint: str
    opened_at: int
    qty: Decimal = ZERO
    cost: Decimal = ZERO
    realized: Decimal = ZERO
    closed_cost: Decimal = ZERO
    fills: list = field(default_factory=list)


def _observations(session, mints: set[str]) -> dict[str, list[Observation]]:
    """SOL prices seen for each mint: every stored trade (any wallet) plus scanned early buys."""
    seen: dict[str, dict[tuple, Observation]] = defaultdict(dict)
    for t in session.scalars(select(Trade).where(Trade.mint.in_(sorted(mints)), Trade.kind.in_(("buy", "sell")))):
        d = t.details
        if t.block_time is None or d.get("quote_asset") != "SOL" or d.get("price") is None:
            continue
        seen[t.mint][(t.signature, t.event_index)] = Observation(t.block_time, t.slot, Decimal(d["price"]))
    for p in session.scalars(select(PricePoint).where(PricePoint.mint.in_(sorted(mints)))):
        if p.block_time is not None:
            seen[p.mint].setdefault((p.signature, p.event_index), Observation(p.block_time, p.slot,
                                                                              Decimal(p.price_sol)))
    for b in session.scalars(select(EarlyBuy).where(EarlyBuy.mint.in_(sorted(mints)))):
        if b.block_time is None or b.details.get("price_sol") is None:
            continue
        seen[b.mint].setdefault((b.signature, b.event_index), Observation(b.block_time, b.slot,
                                                                          Decimal(b.details["price_sol"])))
    return {m: sorted(v.values(), key=lambda o: (o.time, o.slot)) for m, v in seen.items()}


def _first_at_or_after(series: list[Observation], times: list[int], moment: int, staleness: int):
    i = bisect.bisect_left(times, moment)
    if i < len(series) and series[i].time - moment <= staleness:
        return series[i]
    return None


def _last_at_or_before(series: list[Observation], times: list[int], moment: int):
    i = bisect.bisect_right(times, moment)
    return series[i - 1] if i else None


def _leader_trades(session, wallets: list[str], since: int) -> list[Trade]:
    rows = session.scalars(select(Trade).where(Trade.wallet.in_(wallets), Trade.kind.in_(("buy", "sell")),
                                               Trade.block_time >= since)
                           .order_by(Trade.block_time, Trade.slot, Trade.signature, Trade.event_index))
    return [t for t in rows if t.details.get("quote_asset") == "SOL" and t.details.get("price") is not None]


def _leader_closed_roi(trades: list[Trade], mints: set[str]) -> Decimal | None:
    """The leader's own realized SOL return on the given mints (weighted average cost, closed cycles)."""
    qty, cost, realized, closed_cost = defaultdict(Decimal), defaultdict(Decimal), ZERO, ZERO
    for t in trades:
        if t.mint not in mints:
            continue
        d = t.details
        amount = Decimal(d["token_amount"])
        sol = Decimal(d["quote_amount"])
        fees = fee_sol({"network_fee_sol": "0", **d})
        if t.kind == "buy":
            qty[t.mint] += amount
            cost[t.mint] += sol + fees
        elif qty[t.mint] > 0:
            sold = min(amount, qty[t.mint])
            allocated = cost[t.mint] * sold / qty[t.mint]
            qty[t.mint] -= sold
            cost[t.mint] -= allocated
            realized += sol - fees - allocated
            closed_cost += allocated
    return realized / closed_cost * 100 if closed_cost else None


def historical_copy(session, wallets: list[str], *, delays: list[int], size_sol: Decimal, slippage_bps: Decimal,
                    impact_bps_per_sol: Decimal, tx_cost_sol: Decimal, max_staleness_seconds: int,
                    since_days: int, use_candles: bool = True, as_of: datetime | None = None) -> dict:
    as_of = as_of or datetime.now(timezone.utc)
    since = int((as_of - timedelta(days=since_days)).timestamp())
    end = int(as_of.timestamp())
    trades = [t for t in _leader_trades(session, wallets, since) if t.block_time <= end]
    observations = _observations(session, {t.mint for t in trades})
    times = {m: [o.time for o in series] for m, series in observations.items()}
    candles = candle_index(session, {t.mint for t in trades}) if use_candles else {}
    slip = slippage_bps / Decimal(10_000)
    impact = impact_bps_per_sol / Decimal(10_000)
    results = [_simulate(trades, observations, times, candles, d, size_sol, slip, impact, tx_cost_sol,
                         max_staleness_seconds, end) for d in delays]
    return {"wallets": wallets, "as_of": as_of.isoformat(), "since_days": since_days, "leader_trades": len(trades),
            "observed_mints": len(observations), "observations": sum(len(v) for v in observations.values()),
            "candle_mints": len(candles), "candles": sum(len(v[0]) for v in candles.values()),
            "params": {"delays": delays, "size_sol": str(size_sol), "slippage_bps": str(slippage_bps),
                       "impact_bps_per_sol": str(impact_bps_per_sol), "tx_cost_sol": str(tx_cost_sol),
                       "max_staleness_seconds": max_staleness_seconds, "use_candles": use_candles},
            "results": results,
            "limitations": [
                "Reconstrucción histórica: precios observados en la base (operaciones guardadas y compras tempranas "
                "escaneadas), escasos y sesgados hacia lo que se sincronizó; no es una prueba prospectiva.",
                "Retraso 0 usa el precio del propio líder: un ideal inalcanzable, solo como referencia.",
                "Sin observación dentro de la ventana la copia no se ejecuta; no se inventan precios.",
                "Con velas de pool (GeckoTerminal, terceros) y sin precio on-chain en el minuto siguiente, la copia "
                "se ejecuta de forma pesimista: compra al máximo y vende al mínimo de la vela que cubre ese momento.",
                "Solo operaciones con contrapartida SOL; deslizamiento e impacto son supuestos configurables."]}


EXACT_WINDOW = 60  # an on-chain price within this many seconds after the target is used as is


def _fill(t, kind, observations, times, candles, delay, staleness):
    """(price, time, source) for a copy of trade `t`, or None. Never uses a price before t + delay."""
    target = t.block_time + delay
    series, stamps = observations.get(t.mint, []), times.get(t.mint, [])
    exact = _first_at_or_after(series, stamps, target, min(EXACT_WINDOW, staleness))
    if exact is not None:
        return exact.price, exact.time, "chain"
    candle = covering_candle(candles, t.mint, target)
    if candle is not None:  # the price at `target` lay within the candle's range: assume the worst of it
        return Decimal(candle.high if kind == "buy" else candle.low), target, "candle"
    later = _first_at_or_after(series, stamps, target, staleness)
    return (later.price, later.time, "chain") if later is not None else None


def _simulate(trades, observations, times, candles, delay, size, slip, impact, tx_cost, staleness, end) -> dict:
    leader_qty: dict[str, Decimal] = defaultdict(Decimal)
    positions: dict[str, FollowerPosition] = {}
    closed: list[FollowerPosition] = []
    counts = {"buy_signals": 0, "filled_buys": 0, "unfillable_buys": 0, "sell_signals": 0, "mirrored_sells": 0,
              "unfillable_sells": 0, "fills_chain": 0, "fills_candle": 0}
    traded_mints = set()
    for t in trades:
        d = t.details
        amount = Decimal(d["token_amount"])
        if delay == 0:
            obs = Observation(t.block_time, t.slot, Decimal(d["price"]))  # ideal: the leader's own price
        else:
            filled = _fill(t, t.kind, observations, times, candles, delay, staleness)
            obs = Observation(filled[1], 0, filled[0]) if filled else None
            source = filled[2] if filled else None
        if t.kind == "buy":
            counts["buy_signals"] += 1
            leader_qty[t.mint] += amount
            if obs is None:
                counts["unfillable_buys"] += 1
                continue
            price = obs.price * (ONE + slip + impact * size)
            pos = positions.get(t.mint) or FollowerPosition(t.mint, obs.time)
            positions[t.mint] = pos
            pos.qty += size / price
            pos.cost += size + tx_cost
            pos.fills.append({"kind": "buy", "leader_time": t.block_time, "fill_time": obs.time,
                              "price_sol": str(price), "source": "leader" if delay == 0 else source})
            counts["filled_buys"] += 1
            if delay:
                counts[f"fills_{source}"] += 1
            traded_mints.add(t.mint)
            continue
        counts["sell_signals"] += 1
        before = leader_qty[t.mint]
        fraction = ONE if before <= 0 else min(ONE, amount / before)  # unknown position: treat as a full exit
        leader_qty[t.mint] = max(ZERO, before - amount)
        pos = positions.get(t.mint)
        if pos is None or pos.qty <= 0:
            continue
        if obs is None:
            counts["unfillable_sells"] += 1
            continue
        if leader_qty[t.mint] <= before * Decimal("0.0001"):
            fraction = ONE  # the leader is out (or left dust): so is the follower
        sell_qty = pos.qty * fraction
        value = sell_qty * obs.price
        proceeds = value * (ONE - slip - impact * value) - tx_cost
        allocated = pos.cost * sell_qty / pos.qty
        pos.realized += proceeds - allocated
        pos.closed_cost += allocated
        pos.qty -= sell_qty
        pos.cost -= allocated
        pos.fills.append({"kind": "sell", "leader_time": t.block_time, "fill_time": obs.time,
                          "price_sol": str(obs.price), "fraction": str(fraction),
                          "source": "leader" if delay == 0 else source})
        counts["mirrored_sells"] += 1
        if delay:
            counts[f"fills_{source}"] += 1
        if pos.qty <= 0 or fraction == ONE:
            closed.append(pos)
            del positions[t.mint]
    wins = sum(p.realized > 0 for p in closed)
    invested = sum((p.closed_cost for p in closed), ZERO)
    realized = sum((p.realized for p in closed), ZERO)
    roi = realized / invested * 100 if invested else None
    leader_roi = _leader_closed_roi(trades, {p.mint for p in closed})
    marked, mark_value = 0, ZERO
    for pos in positions.values():
        last = _last_at_or_before(observations.get(pos.mint, []), times.get(pos.mint, []), end)
        if last is not None:
            marked += 1
            mark_value += pos.qty * last.price
    return {"delay_seconds": delay, **counts, "closed_positions": len(closed), "wins": wins,
            "losses": sum(p.realized < 0 for p in closed), "win_rate": wins / len(closed) if closed else None,
            "invested_sol": _text(invested), "realized_pnl_sol": _text(realized), "roi_pct": _text(roi),
            "leader_roi_pct": _text(leader_roi), "copy_gap_pct": _text(roi - leader_roi)
            if roi is not None and leader_roi is not None else None,
            "open_positions": len(positions), "open_cost_sol": _text(sum((p.cost for p in positions.values()), ZERO)),
            "open_marked": marked, "open_mark_value_sol": _text(mark_value) if marked else None,
            "positions": [{"mint": p.mint, "realized_pnl_sol": str(p.realized), "cost_sol": str(p.closed_cost),
                           "roi_pct": _text(p.realized / p.closed_cost * 100) if p.closed_cost else None,
                           "fills": p.fills} for p in closed][:50]}


# ---------------------------------------------------------------------------------------------- prospective


def schedule_alert_quotes(session, alerts: list[Alert]):
    """Called when buy/sell alerts are created: quote now and again after each offset."""
    for alert in alerts:
        if alert.kind not in ("buy", "sell"):
            continue
        created = datetime.fromisoformat(alert.created_at)
        for offset in QUOTE_OFFSETS:
            session.add(AlertQuote(alert_id=alert.id, mint=alert.mint, offset_seconds=offset,
                                   due_at=(created + timedelta(seconds=offset)).isoformat()))


def capture_quotes(session, quotes, *, limit: int = 60) -> dict:
    """Fetch quotes that are due (one batched request per 30 mints). The actual fetch time is recorded."""
    report = {"captured": 0, "missing": 0, "failed": 0}
    if quotes is None:
        return report
    due = list(session.scalars(select(AlertQuote).where(AlertQuote.status == "pending",
                                                        AlertQuote.due_at <= now_iso())
                               .order_by(AlertQuote.due_at).limit(limit)))
    if not due:
        return report
    try:
        found = quotes.quotes(sorted({q.mint for q in due}))
    except ProviderError:
        for q in due:
            q.attempts += 1
            if q.attempts >= MAX_QUOTE_ATTEMPTS:
                q.status = "failed"
                report["failed"] += 1
        session.commit()
        return report
    fetched = now_iso()
    for q in due:
        quote = found.get(q.mint)
        q.fetched_at, q.source = fetched, getattr(quotes, "source", None)
        if quote is None:
            q.status = "missing"
            report["missing"] += 1
        else:
            q.status, q.price_usd = "done", str(quote["price_usd"])
            q.liquidity_usd = _text(quote.get("liquidity_usd"))
            report["captured"] += 1
    session.commit()
    return report


def prospective_report(session, *, slippage_bps: Decimal = Decimal(100)) -> dict:
    slip = slippage_bps / Decimal(10_000)
    quotes = defaultdict(dict)
    lateness = []
    for q in session.scalars(select(AlertQuote).where(AlertQuote.status == "done")):
        quotes[q.alert_id][q.offset_seconds] = q
        lateness.append((datetime.fromisoformat(q.fetched_at) - datetime.fromisoformat(q.due_at)).total_seconds())
    alerts = list(session.scalars(select(Alert).where(Alert.kind.in_(("buy", "sell"))).order_by(Alert.id)))
    labels = {w.address: w.label for w in session.scalars(select(Wallet))}
    sells = defaultdict(list)
    for a in alerts:
        if a.kind == "sell":
            sells[(a.wallet, a.mint)].append(a)
    horizons = {"5m": 300, "60m": 3600}
    returns = {name: [] for name in [*horizons, "exit_on_leader_sell"]}
    per_wallet = defaultdict(lambda: {name: [] for name in returns})
    entries = 0
    for a in alerts:
        if a.kind != "buy" or 0 not in quotes[a.id]:
            continue
        entries += 1
        entry = Decimal(quotes[a.id][0].price_usd) * (ONE + slip)
        for name, offset in horizons.items():
            if offset in quotes[a.id]:
                r = Decimal(quotes[a.id][offset].price_usd) * (ONE - slip) / entry - 1
                returns[name].append(r)
                per_wallet[a.wallet][name].append(r)
        exit_alert = next((s for s in sells[(a.wallet, a.mint)] if s.id > a.id and 0 in quotes[s.id]), None)
        if exit_alert is not None:
            r = Decimal(quotes[exit_alert.id][0].price_usd) * (ONE - slip) / entry - 1
            returns["exit_on_leader_sell"].append(r)
            per_wallet[a.wallet]["exit_on_leader_sell"].append(r)

    def summary(values):
        return {"n": len(values), "median_pct": _text(median(values) * 100) if values else None,
                "mean_pct": _text(sum(values) / len(values) * 100) if values else None,
                "hit_rate": sum(v > 0 for v in values) / len(values) if values else None}

    return {"slippage_bps": str(slippage_bps), "buy_alerts_with_entry_quote": entries,
            "median_quote_lateness_seconds": median(lateness) if lateness else None,
            "horizons": {name: summary(values) for name, values in returns.items()},
            "wallets": sorted(({"wallet": w, "label": labels.get(w, ""),
                                **{name: summary(v) for name, v in data.items()}}
                               for w, data in per_wallet.items()),
                              key=lambda x: -(x["60m"]["n"] or 0)),
            "notice": "Seguimiento prospectivo: solo cotizaciones registradas en el momento de cada alerta y después; "
                      "sin información futura. Es significativo cuando se acumulan muchas alertas."}
