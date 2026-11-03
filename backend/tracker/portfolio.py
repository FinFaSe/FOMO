"""Open positions marked to the current market price (unrealized PnL).

Quotes are volatile, so this view is computed on demand and never enters an immutable analysis snapshot.
A mark price is not an execution price: selling a large holding in a thin pool would move the price.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from .adapters.solana import ProviderError
from .analytics import reconstruct
from .db import TokenMint, TokenQuote, now_iso

QUOTE_TTL = timedelta(seconds=60)
LOW_LIQUIDITY_USD = Decimal(10_000)


def _text(value):
    return str(value) if value is not None else None


def refresh_quotes(session, quotes, mints: set[str], *, max_age: timedelta = QUOTE_TTL) -> dict:
    cutoff = (datetime.now(timezone.utc) - max_age).isoformat()
    known = {q.mint: q for q in session.scalars(select(TokenQuote).where(TokenQuote.mint.in_(sorted(mints))))}
    due = sorted(m for m in mints if m not in known or known[m].fetched_at < cutoff)
    if not due:
        return {"refreshed": 0, "found": 0}
    found = quotes.quotes(due)
    for mint in due:
        q = found.get(mint)
        row = known.get(mint) or TokenQuote(mint=mint, source=quotes.source)
        row.found = q is not None
        for key in ("price_usd", "liquidity_usd", "market_cap_usd", "fdv_usd"):
            setattr(row, key, _text(q[key]) if q else None)
        for key in ("pair_address", "dex_id", "symbol"):
            setattr(row, key, q[key] if q else None)
        row.source, row.fetched_at = quotes.source, now_iso()
        session.add(row)
    session.commit()
    return {"refreshed": len(due), "found": len(found)}


def _risks(facts: TokenMint | None, quote: TokenQuote | None) -> list[str]:
    risks = []
    if facts is not None:
        if facts.freeze_authority:
            risks.append("freeze_authority_active")  # the issuer can freeze holders' accounts
        if facts.mint_authority:
            risks.append("mint_authority_active")  # supply can still be inflated
        if any(e in ("transferFeeConfig", "transferHook", "permanentDelegate") for e in facts.extensions or []):
            risks.append("token2022_restrictive_extension")
    if quote is not None and quote.found and Decimal(quote.liquidity_usd or 0) < LOW_LIQUIDITY_USD:
        risks.append("low_liquidity")
    return risks


def open_positions(session, wallet: str, fills, quotes, *, refresh: bool = True) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    positions = [p for p in reconstruct(fills, now) if p.closed_at is None]
    mints = {p.token for p in positions}
    issues = []
    if refresh and quotes is not None and mints:
        try:
            refresh_quotes(session, quotes, mints)
        except ProviderError as exc:
            session.rollback()
            issues.append(exc.message)  # fall back to cached quotes, with their age shown
    quote_rows = {q.mint: q for q in session.scalars(select(TokenQuote).where(TokenQuote.mint.in_(sorted(mints))))}
    facts = {m.mint: m for m in session.scalars(select(TokenMint).where(TokenMint.mint.in_(sorted(mints))))}
    items = []
    value_total = basis_total = unrealized_total = Decimal(0)
    for p in sorted(positions, key=lambda p: p.opened_at, reverse=True):
        q = quote_rows.get(p.token)
        price = Decimal(q.price_usd) if q is not None and q.price_usd is not None else None
        value = p.quantity * price if price is not None else None
        basis = p.basis if p.money_known else None
        unrealized = value - basis if value is not None and basis is not None else None
        if unrealized is not None:
            value_total += value
            basis_total += basis
            unrealized_total += unrealized
        items.append({
            "token": p.token, "cycle": p.cycle, "opened_at": p.opened_at.isoformat(),
            "symbol": q.symbol if q is not None else None, "quantity": str(p.quantity),
            "cost_basis_usd": _text(basis), "price_usd": _text(price), "value_usd": _text(value),
            "unrealized_pnl_usd": _text(unrealized),
            "unrealized_pct": _text(unrealized / basis * 100) if unrealized is not None and basis else None,
            "realized_pnl_usd": _text(p.proceeds - p.allocated_cost) if p.money_known and p.allocated_cost else None,
            "liquidity_usd": q.liquidity_usd if q is not None else None,
            "current_market_cap_usd": q.market_cap_usd if q is not None else None,
            "dex_id": q.dex_id if q is not None else None,
            "quote_fetched_at": q.fetched_at if q is not None else None,
            "quote_found": bool(q is not None and q.found),
            "issues": sorted(p.issues), "risks": _risks(facts.get(p.token), q),
        })
    return {"wallet": wallet, "as_of": now.isoformat(), "positions": items, "issues": issues,
            "quote_source": getattr(quotes, "source", None),
            "totals": {"open_positions": len(items), "valued_positions": sum(i["unrealized_pnl_usd"] is not None
                                                                             for i in items),
                       "value_usd": _text(value_total), "cost_basis_usd": _text(basis_total),
                       "unrealized_pnl_usd": _text(unrealized_total)},
            "limitations": ["Precio de mercado actual (mark-to-market), no precio de ejecución: vender en un pool "
                            "con poca liquidez movería el precio.",
                            "Las cotizaciones son volátiles y no forman parte de los snapshots reproducibles.",
                            "Los totales solo suman posiciones con costo y precio conocidos."]}
