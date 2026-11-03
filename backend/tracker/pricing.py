"""USD valuation and entry market cap for chain-derived trades.

- SOL-quoted trades: amount × SOL/USD close of the Coinbase 1-minute candle containing the block time
  (nearest candle within 5 minutes if that minute had no ticks). The candle's low/high is kept.
- USDC/USDT-quoted trades: valued at par (method `stablecoin_par`). Network fees still need SOL/USD.
- Market cap = USD price per token × supply bound. It is `verified` only for pump.fun mints: a stored
  bonding-curve trade of that mint (any wallet) or its existing bonding-curve account proves its origin,
  and pump.fun mints are created with a fixed supply of 1,000,000,000 tokens (6 decimals) and no mint
  authority. Burns can only lower the supply, so 1e9 is an upper bound: the cap is never understated
  against the maximum entry-cap filter.
- Other mints without mint authority get an `estimated` cap from their current supply (it may understate
  the entry cap if tokens were burned later), shown for research but not accepted by the ranking.
  Mints whose authority can still mint keep `market_cap: unknown`.
"""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from .adapters.prices import MAX_CANDLES
from .db import PriceCandle, TokenMint, Trade

PUMPFUN_SUPPLY = Decimal(1_000_000_000)
PUMPFUN_DECIMALS = 6
NEAREST = 300  # seconds of tolerance when the exact minute has no candle
PAD = 300  # seconds fetched around needed minutes so the nearest-candle fallback has data


def _windows(minutes: list[int]) -> list[tuple[int, int]]:
    """Group sorted minutes into request windows of at most MAX_CANDLES candles, padded both sides."""
    windows: list[tuple[int, int]] = []
    for m in minutes:
        if windows and (m + PAD + 60 - windows[-1][0]) // 60 <= MAX_CANDLES:
            windows[-1] = (windows[-1][0], m + PAD + 60)
        else:
            windows.append((m - PAD, m + PAD + 60))
    return windows


def _needs_usd(t: Trade) -> bool:
    usd = t.details.get("usd")
    return usd is None or usd.get("gross_usd") is None or usd.get("fees_usd") is None


def _candle(cache: dict[int, PriceCandle], block_time: int) -> tuple[PriceCandle | None, int | None]:
    """Candle of the trade's minute, else the nearest one within NEAREST seconds (earlier wins ties)."""
    minute = block_time // 60 * 60
    for offset in sorted(range(-NEAREST, NEAREST + 1, 60), key=lambda o: (abs(o), o)):
        if minute + offset in cache:
            return cache[minute + offset], offset
    return None, None


def _neighborhoods(times: list[int]) -> list[int]:
    return sorted({t // 60 * 60 + o for t in times for o in range(-NEAREST, NEAREST + 1, 60)})


def _pump_mints(session, mints: set[str]) -> set[str]:
    """Mints proven to come from pump.fun: a stored bonding-curve trade (any wallet) or an existing
    bonding-curve account owned by the pump.fun program (`token_mints.pump_origin`)."""
    found: set[str] = set()
    ordered = sorted(mints)
    for i in range(0, len(ordered), 500):
        chunk = ordered[i:i + 500]
        for mint, details in session.execute(select(Trade.mint, Trade.details).where(Trade.mint.in_(chunk))):
            if "pump.fun" in (details.get("venues") or []):
                found.add(mint)
        found.update(session.scalars(select(TokenMint.mint).where(TokenMint.mint.in_(chunk),
                                                                  TokenMint.pump_origin.is_(True))))
    return found


def _market_cap(price_usd: Decimal, decimals: int | None, is_pump: bool, facts: TokenMint | None):
    """(market cap, method, supply used, evidence status) for one trade."""
    if is_pump and decimals == PUMPFUN_DECIMALS:
        return price_usd * PUMPFUN_SUPPLY, "pumpfun_fixed_supply_upper_bound", PUMPFUN_SUPPLY, "verified"
    if (facts is not None and facts.exists and facts.supply_raw is not None and facts.mint_authority is None
            and facts.decimals is not None and facts.decimals == decimals):
        # No mint authority: supply can only have shrunk (burns) since it was revoked, so the current
        # supply may understate the supply at entry. Shown as an estimate, never used for the ranking.
        supply = Decimal(facts.supply_raw) / Decimal(10) ** facts.decimals
        return price_usd * supply, "current_supply_mint_authority_revoked", supply, "estimated"
    return None, None, None, "unknown"


def load_candles(session, prices, times: list[int], needed: list[int] | None = None, *,
                 max_requests: int = 20) -> tuple[dict[int, PriceCandle], int, list[str]]:
    """Cached SOL/USD candles around `times`; fetches what `needed` lacks (prices=None: cache only)."""
    source = getattr(prices, "source", None)
    symbol = getattr(prices, "symbol", "SOL-USD")
    wanted = _neighborhoods(times)
    cache: dict[int, PriceCandle] = {}
    for i in range(0, len(wanted), 500):
        query = select(PriceCandle).where(PriceCandle.symbol == symbol, PriceCandle.minute.in_(wanted[i:i + 500]))
        if source:
            query = query.where(PriceCandle.source == source)
        cache.update({c.minute: c for c in session.scalars(query)})
    missing = sorted({t // 60 * 60 for t in (times if needed is None else needed) if _candle(cache, t)[0] is None})
    requests, issues = 0, []
    if prices is not None and missing:
        for start, end in _windows(missing):
            if requests >= max_requests:
                issues.append("price_request_budget_reached")
                break
            candles = prices.minute_candles(start, end)
            requests += 1
            for c in candles:
                if c["minute"] in cache:
                    continue
                row = PriceCandle(source=source, symbol=symbol, minute=c["minute"],
                                  **{k: str(c[k]) for k in ("open", "high", "low", "close")})
                session.add(row)
                cache[c["minute"]] = row
            session.commit()
    return cache, requests, issues


def sol_usd(cache: dict[int, PriceCandle], block_time: int) -> Decimal | None:
    candle, _ = _candle(cache, block_time)
    return Decimal(candle.close) if candle else None


def price_trades(session, wallet: str, prices, *, max_requests: int = 20) -> dict:
    """Value the wallet's buys and sells in USD. `prices=None` uses cached candles only (no network)."""

    rows = [t for t in session.scalars(select(Trade).where(Trade.wallet == wallet))
            if t.kind in ("buy", "sell") and t.block_time is not None and t.details.get("quote_asset")]
    mints = {t.mint for t in rows}
    pump = _pump_mints(session, mints)
    facts = {m.mint: m for m in session.scalars(select(TokenMint).where(TokenMint.mint.in_(sorted(mints))))}
    # Unpriced trades, plus priced ones whose cap is not verified yet (new mint facts may settle it).
    pending = [t for t in rows if _needs_usd(t) or t.details["evidence"].get("market_cap") != "verified"]
    report = {"priced": 0, "unpriced": 0, "market_caps": 0, "estimated_caps": 0, "price_requests": 0,
              "source": None, "issues": []}
    if not pending:
        return report
    needed = [t.block_time for t in pending if _needs_usd(t)]
    cache, requests, issues = load_candles(session, prices, [t.block_time for t in pending], needed,
                                           max_requests=max_requests)
    report["price_requests"], report["issues"] = requests, issues
    source = getattr(prices, "source", None)
    for t in pending:
        d = t.details
        usd = dict(d.get("usd") or {})
        if _needs_usd(t):
            candle, offset = _candle(cache, t.block_time)
            sol_usd = Decimal(candle.close) if candle else None
            quote = Decimal(d["quote_amount"]) if d.get("quote_amount") is not None else None
            if quote is None:
                gross = None
            elif d["quote_asset"] == "SOL":
                gross = quote * sol_usd if sol_usd is not None else None
            else:
                gross = quote  # stablecoin at par
            fees = Decimal(d["network_fee_sol"]) * sol_usd if sol_usd is not None else None
            usd = {"sol_usd": str(sol_usd) if candle else None,
                   "sol_usd_range": [candle.low, candle.high] if candle else None,
                   "candle_minute": candle.minute if candle else None, "candle_offset_seconds": offset,
                   "price_source": f"{candle.source}/products/{candle.symbol}" if candle else None,
                   "quote_method": "sol_usd_candle" if d["quote_asset"] == "SOL" else "stablecoin_par",
                   "gross_usd": str(gross) if gross is not None else None,
                   "fees_usd": str(fees) if fees is not None else None,
                   "price_usd": str(gross / Decimal(d["token_amount"])) if gross is not None else None,
                   "market_cap_usd": None, "market_cap_method": None, "supply_bound": None}
        cap_status = "unknown"
        if usd.get("price_usd") is not None:
            cap, method, supply, cap_status = _market_cap(Decimal(usd["price_usd"]), d.get("decimals"),
                                                          t.mint in pump, facts.get(t.mint))
            usd["market_cap_usd"] = str(cap) if cap is not None else None
            usd["market_cap_method"], usd["supply_bound"] = method, str(supply) if supply is not None else None
        priced = usd.get("gross_usd") is not None and usd.get("fees_usd") is not None
        evidence = {**d["evidence"], "usd": "verified" if priced else "unpriced", "market_cap": cap_status}
        if usd == d.get("usd") and evidence == d["evidence"]:
            continue  # nothing new for this trade
        evidence["valued_at"] = datetime.now(timezone.utc).isoformat()
        t.details = {**d, "usd": usd, "evidence": evidence}  # reassign: JSON columns track assignment
        report["priced"] += int(priced)
        report["unpriced"] += int(not priced)
        report["market_caps"] += int(cap_status == "verified")
        report["estimated_caps"] += int(cap_status == "estimated")
    report["source"] = source
    session.commit()
    return report
