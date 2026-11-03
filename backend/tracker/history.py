"""Price history for tokens: on-chain price points and third-party pool candles.

- `price_points`: every buy/sell price seen in a stored transaction, for any signer (verifiable; written by the
  discovery scan, which sees many traders that are not tracked).
- `pool_candles`: OHLC of a token's deepest pool from GeckoTerminal (third party, labeled). A `price_histories` row
  is a download request for a time range; the worker fetches it backwards with `before_timestamp`, a few requests
  per run, resuming from its cursor. Minute candles for ranges up to 3 days, hour candles beyond. Free history
  reaches about 6 months back, so older ranges are clipped.
"""

import bisect
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, update

from .adapters.geckoterminal import TIMEFRAMES, WSOL
from .adapters.solana import ProviderError
from .db import DiscoverySeed, PoolCandle, PriceHistory, PricePoint, TokenMint, Trade, now_iso
from .pricing import _market_cap, load_candles, sol_usd

MINUTE_RANGE_LIMIT = 3 * 86400
FREE_HISTORY = 180 * 86400
REQUESTS_PER_RUN = 12
STALE_AFTER = timedelta(minutes=10)
STABLE_QUOTES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KLNRFeaDs3YdGBWbhTRc"}


def record_price_points(session, events) -> int:
    """events: (raw, signer, normalized event). SOL-quoted buys/sells with a price become price points."""
    rows = [(raw, signer, e) for raw, signer, e in events
            if e["kind"] in ("buy", "sell") and e["quote_asset"] == "SOL" and e["price"] is not None]
    if not rows:
        return 0
    keys = {(raw.signature, e["event_index"], signer) for raw, signer, e in rows}
    existing = set(session.execute(select(PricePoint.signature, PricePoint.event_index, PricePoint.signer)
                                   .where(PricePoint.signature.in_(sorted({k[0] for k in keys})))).all())
    added = 0
    for raw, signer, e in rows:
        key = (raw.signature, e["event_index"], signer)
        if key in existing:
            continue
        existing.add(key)
        session.add(PricePoint(mint=e["mint"], signature=raw.signature, event_index=e["event_index"], signer=signer,
                               side=e["kind"], slot=raw.slot, block_time=raw.block_time, price_sol=str(e["price"]),
                               sol_amount=str(e["quote_amount"]), token_amount=str(e["token_amount"])))
        added += 1
    return added


def history_json(row: PriceHistory) -> dict:
    iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None  # noqa: E731
    return {"id": row.id, "mint": row.mint, "timeframe": row.timeframe, "from": iso(row.requested_from),
            "to": iso(row.requested_to), "state": row.state, "pool": row.pool, "quote_mint": row.quote_mint,
            "dex": row.dex, "candles": row.candles, "requests": row.requests, "cursor": iso(row.cursor),
            "error": row.error, "requested_by": row.requested_by, "finished_at": row.finished_at}


def enqueue_history(session, mint: str, start: int, end: int, requested_by: str = "manual") -> PriceHistory | None:
    now = int(datetime.now(timezone.utc).timestamp())
    end, start = min(end, now), max(start, now - FREE_HISTORY)
    if start >= end:
        return None
    timeframe = "minute" if end - start <= MINUTE_RANGE_LIMIT else "hour"
    covering = session.scalar(select(PriceHistory).where(
        PriceHistory.mint == mint, PriceHistory.timeframe == timeframe, PriceHistory.requested_from <= start,
        PriceHistory.requested_to >= end, PriceHistory.state.in_(("queued", "fetching", "done"))).limit(1))
    if covering is not None:
        return covering
    pool = session.scalar(select(PriceHistory).where(PriceHistory.mint == mint, PriceHistory.pool.is_not(None))
                          .limit(1))
    row = PriceHistory(mint=mint, timeframe=timeframe, requested_from=start, requested_to=end, cursor=end,
                       requested_by=requested_by, pool=pool.pool if pool else None,
                       quote_mint=pool.quote_mint if pool else None, dex=pool.dex if pool else None)
    session.add(row)
    session.commit()
    return row


def claim_history(session, worker_id: str) -> PriceHistory | None:
    cutoff = (datetime.now(timezone.utc) - STALE_AFTER).isoformat()
    session.execute(update(PriceHistory).where(PriceHistory.state == "fetching", PriceHistory.heartbeat_at < cutoff)
                    .values(state="queued", locked_by=None))
    session.commit()
    for row in list(session.scalars(select(PriceHistory).where(PriceHistory.state == "queued")
                                    .order_by(PriceHistory.created_at).limit(5))):
        won = session.execute(update(PriceHistory).where(PriceHistory.id == row.id, PriceHistory.state == "queued")
                              .values(state="fetching", locked_by=worker_id, heartbeat_at=now_iso())).rowcount == 1
        session.commit()
        if won:
            session.refresh(row)
            return row
    return None


def fetch_history(session, source, row: PriceHistory, prices=None, *,
                  max_requests: int = REQUESTS_PER_RUN) -> PriceHistory:
    try:
        if row.pool is None:
            pool = source.best_pool(row.mint)
            row.requests += 1
            if pool is None:
                return _finish(session, row, "failed", "no_pool_found")
            row.pool, row.quote_mint, row.dex = pool["pool"], pool["quote_mint"], pool["dex"]
        step = TIMEFRAMES[row.timeframe]
        before = row.cursor or row.requested_to
        for _ in range(max_requests):
            if before <= row.requested_from:
                break
            candles = source.candles(row.pool, row.timeframe, before)
            row.requests += 1
            wanted = [c for c in candles if c["ts"] + step > row.requested_from]
            for c in wanted:
                session.merge(PoolCandle(pool=row.pool, timeframe=row.timeframe, ts=c["ts"], mint=row.mint,
                                         quote_mint=row.quote_mint, source=getattr(source, "source", "unknown"),
                                         **{k: str(c[k]) for k in ("open", "high", "low", "close")},
                                         volume=str(c["volume"]) if c.get("volume") is not None else None))
            row.candles += len(wanted)
            before = min((c["ts"] for c in candles), default=row.requested_from)  # empty: no older data
            row.cursor, row.heartbeat_at = before, now_iso()
            session.commit()
        if before > row.requested_from:
            row.state, row.locked_by = "queued", None  # budget spent: resume from the cursor next run
            session.commit()
            return row
        _finish(session, row, "done")
        refresh_seed_peak(session, row.mint, prices)
        return row
    except ProviderError as exc:
        session.rollback()
        return _finish(session, row, "failed", exc.message)


def _finish(session, row, state, error=None):
    row.state, row.error, row.locked_by, row.finished_at = state, error, None, now_iso()
    session.commit()
    return row


def candle_index(session, mints: set[str], timeframe: str = "minute") -> dict[str, tuple[list[int], list]]:
    """SOL-quoted candles per mint, sorted, for in-memory lookups."""
    index: dict[str, tuple[list[int], list]] = {}
    for c in session.scalars(select(PoolCandle).where(PoolCandle.mint.in_(sorted(mints)),
                                                      PoolCandle.timeframe == timeframe,
                                                      PoolCandle.quote_mint == WSOL).order_by(PoolCandle.ts)):
        times, rows = index.setdefault(c.mint, ([], []))
        if times and times[-1] == c.ts:
            continue  # two pools: keep the first (deepest at download time)
        times.append(c.ts)
        rows.append(c)
    return index


def covering_candle(index, mint: str, moment: int, timeframe: str = "minute"):
    times, rows = index.get(mint, ([], []))
    i = bisect.bisect_right(times, moment) - 1
    if i >= 0 and times[i] <= moment < times[i] + TIMEFRAMES[timeframe]:
        return rows[i]
    return None


def refresh_seed_peak(session, mint: str, prices=None):
    """Use the highest candle as the seed's peak when it beats the other lower bounds."""
    seed = session.get(DiscoverySeed, mint)
    if seed is None or not seed.outcome:
        return
    best = None
    for c in session.scalars(select(PoolCandle).where(PoolCandle.mint == mint)):
        if c.quote_mint not in STABLE_QUOTES and c.quote_mint != WSOL:
            continue
        if best is None or Decimal(c.high) > Decimal(best.high):
            best = c
    if best is None:
        return
    price = Decimal(best.high)
    if best.quote_mint == WSOL:
        try:
            cache, _, _ = load_candles(session, prices, [best.ts])
        except ProviderError:
            session.rollback()
            return
        usd = sol_usd(cache, best.ts)
        if usd is None:
            return
        price *= usd
    outcome = dict(seed.outcome)
    facts = session.get(TokenMint, mint)
    decimals = 6 if outcome.get("pump_origin") else (facts.decimals if facts else None)
    cap, _method, _supply, status = _market_cap(price, decimals, bool(outcome.get("pump_origin")), facts)
    if cap is None:
        return
    outcome.update({"pool_peak_cap_usd": str(cap), "pool_peak_at": datetime.fromtimestamp(best.ts, timezone.utc)
                    .isoformat(), "pool_peak_cap_status": status, "pool_peak_source": best.source})
    current = Decimal(outcome["peak_cap_lower_bound_usd"]) if outcome.get("peak_cap_lower_bound_usd") else None
    if current is None or cap > current:
        outcome["peak_cap_lower_bound_usd"], outcome["peak_basis"] = str(cap), "pool_ohlcv"
        launch = Decimal(outcome["launch_cap_usd"]) if outcome.get("launch_cap_usd") else None
        outcome["peak_multiple_from_launch"] = str(cap / launch) if launch else None
    seed.outcome = outcome
    session.commit()


def chart(session, mint: str, wallet: str | None, start: int | None, end: int | None) -> dict:
    trades = []
    if wallet:
        trades = [t for t in session.scalars(select(Trade).where(Trade.wallet == wallet, Trade.mint == mint,
                                                                 Trade.kind.in_(("buy", "sell")))
                                             .order_by(Trade.block_time)) if t.block_time is not None]
    if start is None or end is None:
        if trades:
            start, end = trades[0].block_time - 1800, trades[-1].block_time + 6 * 3600
        else:
            end = int(datetime.now(timezone.utc).timestamp())
            start = end - 86400
    candles, timeframe = [], None
    for tf in ("minute", "hour"):
        rows = list(session.scalars(select(PoolCandle).where(PoolCandle.mint == mint, PoolCandle.timeframe == tf,
                                                             PoolCandle.ts.between(start - TIMEFRAMES[tf], end))
                                    .order_by(PoolCandle.ts).limit(3000)))
        if rows:
            candles, timeframe = rows, tf
            break
    # On-chain prices: scanned price points plus every tracked wallet's trades of this mint.
    seen = {}
    for p in session.scalars(select(PricePoint).where(PricePoint.mint == mint, PricePoint.block_time.between(start, end))
                             .order_by(PricePoint.block_time).limit(3000)):
        seen[(p.signature, p.event_index)] = (p.block_time, p.price_sol, p.side)
    for t in session.scalars(select(Trade).where(Trade.mint == mint, Trade.kind.in_(("buy", "sell")),
                                                 Trade.block_time.between(start, end)).limit(3000)):
        if t.details.get("quote_asset") == "SOL" and t.details.get("price") is not None:
            seen.setdefault((t.signature, t.event_index), (t.block_time, t.details["price"], t.kind))
    points = sorted(seen.values())
    histories = list(session.scalars(select(PriceHistory).where(PriceHistory.mint == mint)
                                     .order_by(PriceHistory.id.desc()).limit(5)))
    return {"mint": mint, "from": start, "to": end, "timeframe": timeframe,
            "quote": "SOL" if candles and candles[0].quote_mint == WSOL else (candles[0].quote_mint if candles else None),
            "candles": [{"ts": c.ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in candles],
            "points": [{"ts": ts, "price_sol": price, "side": side} for ts, price, side in points],
            "trades": [{"ts": t.block_time, "kind": t.kind, "price_sol": t.details.get("price"),
                        "quote_asset": t.details.get("quote_asset"), "token_amount": t.details["token_amount"],
                        "signature": t.signature} for t in trades],
            "histories": [history_json(h) for h in histories],
            "notice": "Velas de GeckoTerminal (terceros) en la moneda de cotización del pool; los puntos son precios "
                      "de transacciones guardadas (on-chain)."}
