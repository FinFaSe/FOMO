import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .adapters.solana import ProviderError, digest, observe
from .analytics import Fill, analyze
from .db import AnalysisSnapshot, EventRecord, Observation, RawTransaction, Wallet, now_iso
from .schemas import Filters, LedgerImport, SyncRequest


def store_transaction(session, rpc, signature: str) -> RawTransaction:
    payload = rpc.transaction(signature)
    row = session.get(RawTransaction, signature)
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


def ingest(session, wallet: Wallet, rpc, request: SyncRequest) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=request.days)
    report = {"started_at": now_iso(), "since": since.isoformat(), "state": "running",
              "transactions": 0, "failed_transactions": 0, "pages": 0, "cursor": None,
              "signature_window_complete": False, "normalized_history_complete": False,
              "issues": [], "source": rpc.source}
    wallet.sync = report.copy()
    session.commit()
    before = None
    try:
        for _ in range(request.max_pages):
            page = rpc.signatures(wallet.address, before=before, limit=request.page_size)
            report["pages"] += 1
            if not page:
                report["signature_window_complete"] = True
                break
            crossed = False
            for item in page:
                block_time = item.get("blockTime")
                if block_time is None:
                    report["issues"].append("missing_block_time")
                elif block_time < since.timestamp():
                    crossed = True
                    break
                raw = store_transaction(session, rpc, item["signature"])
                observation = observe(raw.payload, wallet.address)
                old = session.scalar(select(Observation).where(Observation.wallet == wallet.address,
                                                               Observation.signature == raw.signature))
                if not old:
                    session.add(Observation(wallet=wallet.address, signature=raw.signature,
                                            details={**observation, "source": raw.source,
                                                     "payload_sha256": raw.digest, "fetched_at": raw.fetched_at}))
                report["transactions"] += 1
                report["failed_transactions"] += int(observation["status"] == "failed")
                session.commit()
            before = page[-1]["signature"]
            report["cursor"] = before
            if crossed or len(page) < request.page_size:
                report["signature_window_complete"] = "missing_block_time" not in report["issues"]
                break
        report["state"] = "scanned" if report["signature_window_complete"] else "partial"
        if not report["signature_window_complete"]:
            report["issues"].append("page_budget_reached")
    except ProviderError as exc:
        session.rollback()
        report["state"] = "partial"
        report["issues"].append(exc.message)
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
            raw = store_transaction(session, rpc, signature)
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
