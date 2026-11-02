import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .adapters.solana import ProviderError, digest, observe
from .analytics import Fill, analyze
from .db import AnalysisSnapshot, EventRecord, Observation, RawTransaction, SyncState, Wallet, now_iso
from .schemas import Filters, LedgerImport, SyncRequest


def store_transaction(session, rpc, signature: str, *, refresh: bool = False) -> RawTransaction:
    """Return stored evidence; only call the RPC when it is missing or `refresh` is requested.

    A refresh re-downloads the finalized transaction and compares digests, so a provider that
    changes a finalized response produces a conflict instead of a silent overwrite.
    """
    row = session.get(RawTransaction, signature)
    if row and not refresh:
        return row
    payload = rpc.transaction(signature)
    if row:
        # Finalized ledger should not change; preserve original evidence instead of silently replacing it.
        if row.digest != digest(payload):
            raise ProviderError("Conflicto de evidencia finalized: posible cambio de proveedor o reorganización", 409)
        return row
    row = RawTransaction(signature=signature, slot=payload["slot"], block_time=payload.get("blockTime"),
                         payload=payload, digest=digest(payload), source=rpc.source)
    session.add(row)
    session.flush()
    return row


def _record(session, wallet: Wallet, rpc, signature: str, report: dict, *, refresh: bool = False):
    """Store one listed signature for the wallet. Known observations cost no RPC call."""

    known = session.scalar(select(Observation.id).where(Observation.wallet == wallet.address,
                                                        Observation.signature == signature))
    if known and not refresh:
        report["already_known"] += 1
        return
    stored = session.get(RawTransaction, signature) is not None
    raw = store_transaction(session, rpc, signature, refresh=refresh)
    report["rpc_transaction_fetches"] += int(refresh or not stored)
    report["reused_stored_payloads"] += int(stored and not refresh)
    report["cursor"] = signature
    if known:
        report["revalidated"] += 1
        return
    observation = observe(raw.payload, wallet.address)
    session.add(Observation(wallet=wallet.address, signature=raw.signature,
                            details={**observation, "source": raw.source,
                                     "payload_sha256": raw.digest, "fetched_at": raw.fetched_at}))
    report["transactions"] += 1
    report["failed_transactions"] += int(observation["status"] == "failed")


def _missing_block_time(state: SyncState | None, report: dict):
    if state is not None:
        state.missing_block_times += 1
    if "missing_block_time" not in report["issues"]:
        report["issues"].append("missing_block_time")


def _page(rpc, wallet: Wallet, report: dict, budget: list[int], **kwargs):
    """List one page. An empty listing (nothing new) is reported but does not spend the budget."""
    page = rpc.signatures(wallet.address, **kwargs)
    report["pages"] += 1
    if page:
        budget[0] -= 1
    return page


def _catch_up(session, wallet: Wallet, rpc, state: SyncState, request: SyncRequest, report: dict,
              budget: list[int]) -> bool:
    """List signatures newer than head (resuming an interrupted catch-up). True when it reached head."""

    while budget[0] > 0:
        page = _page(rpc, wallet, report, budget, before=state.catchup_before,
                     until=state.head_signature, limit=request.page_size)
        for item in page:
            _record(session, wallet, rpc, item["signature"], report)
            if state.catchup_head is None:
                state.catchup_head = item["signature"]
            state.catchup_before = item["signature"]
            if item.get("blockTime") is None:
                _missing_block_time(state, report)
            state.updated_at = now_iso()
            session.commit()
        if len(page) < request.page_size:
            if state.catchup_head:
                state.head_signature = state.catchup_head
            state.catchup_head = state.catchup_before = None
            state.updated_at = now_iso()
            session.commit()
            return True
    return False


def _backfill(session, wallet: Wallet, rpc, state: SyncState, request: SyncRequest, report: dict,
              budget: list[int], since: int) -> bool:
    """Extend coverage below tail until the window start or the wallet's first signature."""

    if state.history_start_reached or (state.covered_from is not None and state.covered_from <= since):
        return True
    while budget[0] > 0:
        page = _page(rpc, wallet, report, budget, before=state.tail_signature, limit=request.page_size)
        for item in page:
            block_time = item.get("blockTime")
            if block_time is not None and block_time < since:
                # Listing is newest first: every signature newer than this one is already stored.
                # Tail stays on the last processed signature so a wider window resumes right here.
                state.covered_from = block_time + 1
                state.updated_at = now_iso()
                session.commit()
                return True
            _record(session, wallet, rpc, item["signature"], report)
            if state.head_signature is None:
                state.head_signature = item["signature"]
            state.tail_signature = item["signature"]
            if block_time is None:
                _missing_block_time(state, report)
            else:
                state.tail_block_time = block_time
                state.covered_from = block_time + 1
            state.updated_at = now_iso()
            session.commit()
        if len(page) < request.page_size:
            state.history_start_reached = True
            state.covered_from = 0
            state.updated_at = now_iso()
            session.commit()
            return True
    return False


def _revalidate(session, wallet: Wallet, rpc, request: SyncRequest, report: dict, budget: list[int],
                since: int) -> bool:
    """Re-download from the newest signature and compare digests. Does not move incremental cursors."""

    before = None
    while budget[0] > 0:
        page = _page(rpc, wallet, report, budget, before=before, limit=request.page_size)
        for item in page:
            block_time = item.get("blockTime")
            if block_time is not None and block_time < since:
                return True
            _record(session, wallet, rpc, item["signature"], report, refresh=True)
            if block_time is None:
                _missing_block_time(None, report)
            session.commit()
        if len(page) < request.page_size:
            return True
        before = page[-1]["signature"]
    return False


def _coverage(state: SyncState | None) -> dict | None:
    if state is None:
        return None
    return {"head_signature": state.head_signature, "tail_signature": state.tail_signature,
            "tail_block_time": state.tail_block_time,
            "covered_from": (datetime.fromtimestamp(state.covered_from, timezone.utc).isoformat()
                             if state.covered_from is not None else None),
            "history_start_reached": state.history_start_reached,
            "catchup_pending": state.catchup_before is not None,
            "missing_block_times": state.missing_block_times, "updated_at": state.updated_at}


def ingest(session, wallet: Wallet, rpc, request: SyncRequest) -> dict:
    """Incremental scan: catch up on signatures newer than head, then backfill below tail.

    Signatures already observed for this wallet cost no getTransaction call. `max_pages` is a
    per-run budget of signature pages; the next run resumes from the stored cursors.
    """

    since_dt = datetime.now(timezone.utc) - timedelta(days=request.days)
    since = int(since_dt.timestamp())
    report = {"started_at": now_iso(), "since": since_dt.isoformat(), "state": "running",
              "mode": "revalidate" if request.revalidate else "incremental",
              "transactions": 0, "already_known": 0, "revalidated": 0, "rpc_transaction_fetches": 0,
              "reused_stored_payloads": 0, "failed_transactions": 0, "pages": 0, "cursor": None,
              "signature_window_complete": False, "normalized_history_complete": False,
              "issues": [], "source": rpc.source}
    wallet.sync = report.copy()
    session.commit()
    budget = [request.max_pages]
    state = None
    try:
        if request.revalidate:
            complete = _revalidate(session, wallet, rpc, request, report, budget, since)
            complete = complete and "missing_block_time" not in report["issues"]
            state = session.get(SyncState, wallet.address)
        else:
            state = session.get(SyncState, wallet.address)
            if state is None:
                state = SyncState(wallet=wallet.address)
                session.add(state)
                session.commit()
            initialized = state.head_signature is not None or state.history_start_reached
            caught_up = _catch_up(session, wallet, rpc, state, request, report, budget) if initialized else True
            backfilled = caught_up and _backfill(session, wallet, rpc, state, request, report, budget, since)
            complete = caught_up and backfilled and state.missing_block_times == 0
            if caught_up and backfilled and state.missing_block_times and "missing_block_time" not in report["issues"]:
                report["issues"].append("missing_block_time")
        report["signature_window_complete"] = complete
        report["state"] = "scanned" if complete else "partial"
        if not complete and budget[0] <= 0:
            report["issues"].append("page_budget_reached")
    except ProviderError as exc:
        # Work committed before the failure is kept; cursors point at the last stored signature.
        session.rollback()
        report["state"] = "partial"
        report["issues"].append(exc.message)
    report["coverage"] = _coverage(state)
    report["finished_at"] = now_iso()
    # Wallet may have expired on rollback, but its identity is retained in this session.
    wallet.sync = report
    session.commit()
    return report


def import_ledger(session, wallet: str, request: LedgerImport):
    inserted, duplicates = 0, 0
    for event in request.events:
        payload = event.model_dump(mode="json")
        old = session.scalar(select(EventRecord).where(
            EventRecord.wallet == wallet, EventRecord.signature == event.signature,
            EventRecord.event_index == event.event_index, EventRecord.token == event.token))
        if old:
            if old.payload != payload:
                raise ProviderError("Importación en conflicto: la misma clave contiene datos distintos", 409)
            duplicates += 1
            continue
        session.add(EventRecord(wallet=wallet, signature=event.signature, event_index=event.event_index,
                                token=event.token, timestamp=event.timestamp.isoformat(), payload=payload,
                                evidence={"movement": "unverified", "trade": "unverified",
                                          "usd": "reported", "market_cap": "reported" if
                                          event.market_cap_usd is not None else "unknown"}))
        session.flush()
        inserted += 1
    session.commit()
    return {"inserted": inserted, "duplicates": duplicates, "verification": "reported_not_verified"}


def verify_ledger(session, wallet: str, rpc, *, limit: int = 50, offset: int = 0):
    records = list(session.scalars(select(EventRecord).where(EventRecord.wallet == wallet)))
    all_signatures = sorted({e.signature for e in records})
    signatures = all_signatures[offset:offset + limit]
    matches, errors = 0, []
    for signature in signatures:
        matching = [e for e in records if e.signature == signature]
        try:
            raw = store_transaction(session, rpc, signature, refresh=True)
            ob = observe(raw.payload, wallet)
            for row in matching:
                same_mint = [e for e in matching if e.token == row.token]
                expected = Decimal(row.payload["quantity"]) * (
                    1 if row.payload["kind"] in ("buy", "transfer_in") else -1)
                timestamp = datetime.fromisoformat(row.timestamp).timestamp()
                movement = "unverified"
                if len(same_mint) == 1:
                    movement = "matched" if (ob["status"] == "finalized"
                                and raw.block_time == timestamp
                                and Decimal(ob["token_deltas"].get(row.token, "0")) == expected) else "mismatch"
                row.evidence = {**row.evidence, "movement": movement, "trade": "unverified",
                                "rpc_source": raw.source, "payload_sha256": raw.digest,
                                "checked_at": now_iso(), "slot": raw.slot,
                                "note": "Un delta coincidente no prueba un swap ni la valoración USD"}
                matches += int(movement == "matched")
            session.commit()
        except ProviderError as exc:
            session.rollback()
            for row in matching:
                row.evidence = {**row.evidence, "movement": "unavailable", "checked_at": now_iso(),
                                "verification_error": exc.message}
            session.commit()
            errors.append({"signature": signature, "error": exc.message})
    remaining = max(0, len(all_signatures) - offset - len(signatures))
    return {"checked_signatures": len(signatures), "matched_movements": matches, "errors": errors,
            "remaining_signatures": remaining,
            "next_offset": offset + len(signatures) if remaining else None,
            "usd_and_historical_market_cap_verified": False}


def wallet_analysis(session, wallet: Wallet, filters: Filters, as_of: datetime):
    records = session.scalars(select(EventRecord).where(EventRecord.wallet == wallet.address))
    fills = []
    for row in records:
        p = row.payload
        fills.append(Fill(signature=row.signature, event_index=row.event_index, token=row.token,
                          timestamp=datetime.fromisoformat(row.timestamp), kind=p["kind"],
                          quantity=Decimal(p["quantity"]), source_url=p["source_url"], evidence=row.evidence,
                          **{k: Decimal(p[k]) if p.get(k) is not None else None for k in
                             ("gross_usd", "fees_usd", "market_cap_usd")}))
    # Signature coverage alone is NOT complete normalized trade/inventory/valuation coverage.
    result = analyze(wallet.address, fills, filters, as_of, history_complete=False)
    if not session.get(AnalysisSnapshot, result["snapshot_id"]):
        session.add(AnalysisSnapshot(id=result["snapshot_id"], wallet=wallet.address,
                                     as_of=as_of.isoformat(), result=json.loads(json.dumps(result))))
        try:
            session.commit()
        except IntegrityError:
            # Ranking and profile GETs can request the same immutable snapshot concurrently.
            session.rollback()
            existing = session.get(AnalysisSnapshot, result["snapshot_id"])
            if existing is None or existing.result != result:
                raise
    return result
