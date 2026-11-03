"""Precomputed ranking: each wallet's analysis per filter set, kept current by an input revision.

A wallet's analysis depends only on its own trades, ledger events and sync state, plus the method versions.
Every change to those bumps `Wallet.analysis_revision` in the same transaction (see tracker.db), so an entry
whose revision and engine match is exactly what recomputing now would return. Entries are refreshed after
each sync (default filters), by the worker in the background, and on read for whatever is still stale:
reads never serve outdated results, they only stop recomputing every wallet.
"""

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects import postgresql, sqlite

from .analytics import VERSION
from .db import AnalysisSnapshot, RankingEntry, SyncState, Wallet, now_iso
from .entities import entity_index
from .normalize import NORMALIZER_VERSION
from .schemas import Filters
from .service import save_snapshot, wallet_analysis

ENGINE = f"{VERSION}+{NORMALIZER_VERSION}"
DEFAULT_FILTERS = Filters()
# Entries of other filter sets that were not recomputed for this long are dropped (rebuilt if used again).
KEEP_OTHER_FILTERS = timedelta(days=7)
NOTICE = "Solo historial completo y evidencia verificada; las importaciones no se autoverifican."


def canonical_filters(filters: Filters) -> dict:
    """Filters with decimals in canonical form, so 250000 and 250000.0 share one entry."""
    return {k: format(v.normalize(), "f") if isinstance(v, Decimal) else v
            for k, v in sorted(filters.model_dump().items())}


def filters_key(filters: Filters) -> str:
    return hashlib.sha256(json.dumps(canonical_filters(filters), sort_keys=True).encode()).hexdigest()


DEFAULT_KEY = filters_key(DEFAULT_FILTERS)


def _current(key: str):
    return and_(RankingEntry.filters_key == key, RankingEntry.revision == Wallet.analysis_revision,
                RankingEntry.engine == ENGINE)


def _stale(key: str):
    """(address, current revision) of the wallets whose entry for this filter set is missing or outdated."""
    return (select(Wallet.address, Wallet.analysis_revision)
            .outerjoin(RankingEntry, and_(RankingEntry.wallet == Wallet.address, RankingEntry.filters_key == key))
            .where(or_(RankingEntry.wallet.is_(None), RankingEntry.revision != Wallet.analysis_revision,
                       RankingEntry.engine != ENGINE))
            .order_by(Wallet.address))


def _upsert(session, values: dict) -> None:
    table = RankingEntry.__table__
    insert = {"postgresql": postgresql.insert, "sqlite": sqlite.insert}.get(session.get_bind().dialect.name)
    if insert is None:
        session.merge(RankingEntry(**values))
        return
    stmt = insert(table).values(**values)
    session.execute(stmt.on_conflict_do_update(
        index_elements=[table.c.wallet, table.c.filters_key],
        set_={k: stmt.excluded[k] for k in values if k not in ("wallet", "filters_key")},
        # A slower concurrent refresh that started from an older revision never overwrites a newer one.
        where=table.c.revision <= stmt.excluded.revision))


def refresh_wallet(session, wallet: Wallet, filters: Filters = DEFAULT_FILTERS, *, key: str | None = None,
                   revision: int | None = None) -> dict:
    """Recompute and store one entry; returns the full analysis.

    The revision is read before the inputs: a write landing in between leaves the entry marked stale (it is
    recomputed again), never a stale entry marked current.
    """
    key = key or filters_key(filters)
    if revision is None:
        revision = session.scalar(select(Wallet.analysis_revision).where(Wallet.address == wallet.address))
    started = time.perf_counter()
    result = wallet_analysis(session, wallet, filters, persist=False)
    score = result["score"]
    _upsert(session, {
        "wallet": wallet.address, "filters_key": key, "filters": canonical_filters(filters), "revision": revision,
        "engine": ENGINE, "as_of": result["as_of"], "eligible": result["eligible"],
        "score": score["verified_smart_money"],
        "summary": {"score": score, "metrics": result["metrics"], "exclusion_reasons": result["exclusion_reasons"],
                    "history_complete": result["history_complete"],
                    "behavior": result["behavior"]["classification"]},
        "result": json.loads(json.dumps(result)), "snapshot_id": result["snapshot_id"], "computed_at": now_iso(),
        "compute_ms": round((time.perf_counter() - started) * 1000)})
    session.commit()
    return result


def refresh_stale(session, filters: Filters = DEFAULT_FILTERS, *, budget_seconds: float | None = None) -> dict:
    """Recompute the missing or outdated entries of a filter set: all of them, or until the budget is spent
    (always at least one, so a background pass makes progress)."""
    key = filters_key(filters)
    started, refreshed = time.monotonic(), 0
    stale = session.execute(_stale(key)).all()
    for address, revision in stale:
        if budget_seconds is not None and refreshed and time.monotonic() - started >= budget_seconds:
            break
        refresh_wallet(session, session.get(Wallet, address), filters, key=key, revision=revision)
        refreshed += 1
    return {"refreshed": refreshed, "remaining": len(stale) - refreshed}


def prune(session, older_than: timedelta = KEEP_OTHER_FILTERS) -> int:
    """Drop entries of non-default filter sets not recomputed recently; the default set is always kept."""
    cutoff = (datetime.now(timezone.utc) - older_than).isoformat()
    deleted = session.execute(delete(RankingEntry).where(RankingEntry.filters_key != DEFAULT_KEY,
                                                         RankingEntry.computed_at < cutoff)).rowcount
    session.commit()
    return deleted


def materialize(session, snapshot_ids: list[str]) -> None:
    """Save as immutable snapshots the entries whose ids the API is about to return, so every id it hands
    out stays resolvable after the entry is refreshed."""
    if not snapshot_ids:
        return
    saved = set(session.scalars(select(AnalysisSnapshot.id).where(AnalysisSnapshot.id.in_(snapshot_ids))))
    missing = [i for i in snapshot_ids if i not in saved]
    if missing:
        for (result,) in session.execute(select(RankingEntry.result).where(RankingEntry.snapshot_id.in_(missing))):
            save_snapshot(session, result)


def current_analysis(session, wallet: Wallet, filters: Filters) -> dict:
    """The analysis as of the wallet's last completed sync, from its entry when that is current.

    Without a completed sync the default as_of is "now", which moves on every call: computed each time.
    """
    caught_up = session.scalar(select(SyncState.caught_up_at).where(SyncState.wallet == wallet.address))
    if caught_up is None:
        return wallet_analysis(session, wallet, filters)
    key = filters_key(filters)
    result = session.scalar(select(RankingEntry.result).join(Wallet, Wallet.address == RankingEntry.wallet)
                            .where(RankingEntry.wallet == wallet.address, _current(key)))
    if result is None:
        result = refresh_wallet(session, wallet, filters, key=key)
    save_snapshot(session, result)
    return result


def ranking(session, filters: Filters, *, limit: int = 20, offset: int = 0) -> dict:
    """Eligible wallets ordered by verified score, served from the entries (stale ones are recomputed first)."""
    refreshed = refresh_stale(session, filters)["refreshed"]  # whatever the worker has not refreshed yet
    key = filters_key(filters)
    eligible = and_(RankingEntry.filters_key == key, RankingEntry.eligible.is_(True))
    total = session.scalar(select(func.count()).select_from(RankingEntry).where(eligible))
    rows = session.execute(
        select(RankingEntry.wallet, Wallet.label, RankingEntry.as_of, RankingEntry.summary, RankingEntry.snapshot_id,
               RankingEntry.computed_at)
        .join(Wallet, Wallet.address == RankingEntry.wallet).where(eligible)
        .order_by(RankingEntry.score.desc(), RankingEntry.wallet).offset(offset).limit(limit)).all()
    materialize(session, [r.snapshot_id for r in rows])
    groups = entity_index(session) if rows else {}
    items = []
    for r in rows:
        entity = groups.get(r.wallet)
        items.append({"address": r.wallet, "label": r.label, "as_of": r.as_of, "score": r.summary["score"],
                      "metrics": r.summary["metrics"], "snapshot_id": r.snapshot_id, "computed_at": r.computed_at,
                      # Linked wallets may be one actor: they are not independent confirmations.
                      "entity": {"id": entity["id"], "size": entity["size"],
                                 "members": entity["members"]} if entity else None})
    oldest, wallets = session.execute(select(func.min(RankingEntry.computed_at), func.count())
                                      .where(RankingEntry.filters_key == key)).one()
    return {"items": items, "total": total, "as_of": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "notice": NOTICE,
            "precomputed": {"wallets": wallets, "recomputed_now": refreshed, "oldest_computed_at": oldest}}


def ranking_status(session) -> dict:
    """Freshness of the default filter set (what the worker keeps current)."""
    wallets = session.scalar(select(func.count()).select_from(Wallet))
    stale = session.scalar(select(func.count()).select_from(_stale(DEFAULT_KEY).subquery()))
    return {"wallets": wallets, "stale": stale, "engine": ENGINE}
