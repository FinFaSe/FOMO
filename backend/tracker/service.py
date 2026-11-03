import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from .adapters.solana import ProviderError, digest, observe
from .analytics import Fill, analyze, reconstruct, wilson
from .db import AnalysisSnapshot, EventRecord, Observation, RawTransaction, SyncState, Trade, Wallet, now_iso
from .normalize import NORMALIZER_VERSION, normalize
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


def _text(value):
    return str(value) if value is not None else None


def store_trades(session, wallet: str, raw: RawTransaction) -> int:
    """Derive trade/transfer rows from stored evidence. Deterministic; no RPC call."""

    events = normalize(raw.payload, wallet)
    for e in events:
        details = {"token_amount": str(e["token_amount"]), "decimals": e["decimals"], "quote_asset": e["quote_asset"],
                   "quote_amount": _text(e["quote_amount"]), "price": _text(e["price"]),
                   "network_fee_sol": str(e["network_fee_sol"]), "tip_sol": str(e["tip_sol"]),
                   "venues": e["venues"], "issues": e["issues"],
                   "evidence": {"trade": e["trade"], "usd": "unknown", "market_cap": "unknown",
                                "payload_sha256": raw.digest, "rpc_source": raw.source}}
        session.add(Trade(wallet=wallet, signature=raw.signature, event_index=e["event_index"], mint=e["mint"],
                          kind=e["kind"], slot=raw.slot, block_time=raw.block_time, details=details,
                          normalizer=NORMALIZER_VERSION))
    return len(events)


def renormalize(session, wallet: str) -> dict:
    """Rebuild every trade of the wallet from its stored observations, e.g. after a normalizer update."""

    session.execute(delete(Trade).where(Trade.wallet == wallet))
    rows = list(session.scalars(select(RawTransaction)
                                .join(Observation, Observation.signature == RawTransaction.signature)
                                .where(Observation.wallet == wallet)))
    transactions = trades = 0
    for raw in rows:
        transactions += 1
        trades += store_trades(session, wallet, raw)
    state = session.get(SyncState, wallet)
    if state is not None:
        state.normalizer = NORMALIZER_VERSION
    session.commit()
    return {"transactions": transactions, "trades": trades, "normalizer": NORMALIZER_VERSION,
            "rpc_calls": 0}


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
    observation, trades = record_observation(session, wallet.address, raw)
    report["transactions"] += 1
    report["failed_transactions"] += int(observation["status"] == "failed")
    report["trades"] += trades


def record_observation(session, wallet: str, raw: RawTransaction) -> tuple[dict, int]:
    """Add the wallet's observation and normalized trades for a stored transaction (caller commits)."""
    observation = observe(raw.payload, wallet)
    session.add(Observation(wallet=wallet, signature=raw.signature,
                            details={**observation, "source": raw.source,
                                     "payload_sha256": raw.digest, "fetched_at": raw.fetched_at}))
    return observation, store_trades(session, wallet, raw)


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


def _noop(report: dict):
    return None


def _catch_up(session, wallet: Wallet, rpc, state: SyncState, request: SyncRequest, report: dict,
              budget: list[int], tick=_noop) -> bool:
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
            tick(report)
        if len(page) < request.page_size:
            if state.catchup_head:
                state.head_signature = state.catchup_head
            state.catchup_head = state.catchup_before = None
            state.updated_at = now_iso()
            session.commit()
            return True
    return False


def _backfill(session, wallet: Wallet, rpc, state: SyncState, request: SyncRequest, report: dict,
              budget: list[int], since: int, tick=_noop) -> bool:
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
            tick(report)
        if len(page) < request.page_size:
            state.history_start_reached = True
            state.covered_from = 0
            state.updated_at = now_iso()
            session.commit()
            return True
    return False


def _revalidate(session, wallet: Wallet, rpc, request: SyncRequest, report: dict, budget: list[int],
                since: int, tick=_noop) -> bool:
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
            tick(report)
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


FINALIZATION_MARGIN = timedelta(seconds=120)


def ingest(session, wallet: Wallet, rpc, request: SyncRequest, on_progress=None) -> dict:
    """Incremental scan: catch up on signatures newer than head, then backfill below tail.

    Signatures already observed for this wallet cost no getTransaction call. `max_pages` is a
    per-run budget of signature pages; the next run resumes from the stored cursors.
    `on_progress(report)` runs after every stored transaction; raising ProviderError there stops the
    scan cleanly (used for cancellation).
    """
    tick = on_progress or _noop

    since_dt = datetime.now(timezone.utc) - timedelta(days=request.days)
    since = int(since_dt.timestamp())
    report = {"started_at": now_iso(), "since": since_dt.isoformat(), "state": "running",
              "mode": "revalidate" if request.revalidate else "incremental",
              "transactions": 0, "trades": 0, "already_known": 0, "revalidated": 0, "rpc_transaction_fetches": 0,
              "reused_stored_payloads": 0, "failed_transactions": 0, "pages": 0, "cursor": None,
              "signature_window_complete": False, "normalized_history_complete": False,
              "issues": [], "source": rpc.source}
    wallet.sync = report.copy()
    session.commit()
    budget = [request.max_pages]
    state = None
    # Listings use `finalized`, which lags the cluster by seconds; leave a margin before claiming coverage.
    listed_through = (datetime.now(timezone.utc) - FINALIZATION_MARGIN).replace(microsecond=0).isoformat()
    try:
        if request.revalidate:
            complete = _revalidate(session, wallet, rpc, request, report, budget, since, tick)
            complete = complete and "missing_block_time" not in report["issues"]
            state = session.get(SyncState, wallet.address)
        else:
            state = session.get(SyncState, wallet.address)
            if state is None:
                state = SyncState(wallet=wallet.address)
                session.add(state)
                session.commit()
            if state.normalizer != NORMALIZER_VERSION:
                # Wallets scanned before (or with an older normalizer) are rebuilt from stored evidence.
                rebuilt = renormalize(session, wallet.address)
                if rebuilt["transactions"]:
                    report["renormalized"] = rebuilt
            initialized = state.head_signature is not None or state.history_start_reached
            caught_up = (_catch_up(session, wallet, rpc, state, request, report, budget, tick)
                         if initialized else True)
            backfilled = caught_up and _backfill(session, wallet, rpc, state, request, report, budget, since, tick)
            complete = caught_up and backfilled and state.missing_block_times == 0
            if caught_up and backfilled and state.missing_block_times and "missing_block_time" not in report["issues"]:
                report["issues"].append("missing_block_time")
            # A completed catch-up, or a first scan from the newest signature (which may find nothing
            # inside the window), covers everything listed up to now.
            if caught_up and (backfilled or state.head_signature is not None or state.history_start_reached):
                state.caught_up_at = listed_through
                session.commit()
            report["normalized_history_complete"] = complete
        report["signature_window_complete"] = complete
        report["state"] = "scanned" if complete else "partial"
        if not complete and budget[0] <= 0:
            report["issues"].append("page_budget_reached")
    except ProviderError as exc:
        # Work committed before the failure is kept; cursors point at the last stored signature.
        session.rollback()
        report["state"] = "partial"
        report["issues"].append(exc.message)
        report["error"] = exc.message
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


def _chain_fills(session, wallet: str) -> tuple[list[Fill], set[tuple[str, str]]]:
    """Fills from normalized chain trades; USD and market cap only where pricing proved them."""

    fills, covered = [], set()
    for t in session.scalars(select(Trade).where(Trade.wallet == wallet)):
        if t.block_time is None:
            continue
        d, usd = t.details, t.details.get("usd") or {}
        covered.add((t.signature, t.mint))

        def money(key):
            return Decimal(usd[key]) if usd.get(key) is not None else None

        evidence = d["evidence"]
        fills.append(Fill(signature=t.signature, event_index=t.event_index, token=t.mint,
                          timestamp=datetime.fromtimestamp(t.block_time, timezone.utc), kind=t.kind,
                          quantity=Decimal(d["token_amount"]), gross_usd=money("gross_usd"),
                          fees_usd=money("fees_usd"), market_cap_usd=money("market_cap_usd") if t.kind == "buy" else None,
                          source_url=f"https://solscan.io/tx/{t.signature}", slot=t.slot,
                          evidence={"source": "chain", "movement": "chain", "trade": evidence["trade"],
                                    "usd": evidence.get("usd", "unknown"),
                                    "market_cap": evidence.get("market_cap", "unknown"),
                                    "normalizer": t.normalizer, "payload_sha256": evidence["payload_sha256"]}))
    return fills, covered


def history_complete(state: SyncState | None, as_of: datetime, days: int) -> bool:
    """True when the signature scan covers [as_of − days, as_of] and every stored transaction is normalized."""
    if state is None or state.caught_up_at is None or state.covered_from is None:
        return False
    since = int((as_of - timedelta(days=days)).timestamp())
    return (datetime.fromisoformat(state.caught_up_at) >= as_of and state.covered_from <= since
            and not state.missing_block_times and state.catchup_before is None
            and state.normalizer == NORMALIZER_VERSION)


def default_as_of(session, wallet: str) -> datetime:
    """Analyze as of the last completed sync when there is one; otherwise now."""
    state = session.get(SyncState, wallet)
    if state is not None and state.caught_up_at is not None:
        return datetime.fromisoformat(state.caught_up_at)
    return datetime.now(timezone.utc).replace(microsecond=0)


def wallet_fills(session, wallet: str) -> list[Fill]:
    """Chain-derived fills plus imported ledger events not already covered by the chain."""
    fills, covered = _chain_fills(session, wallet)
    for row in session.scalars(select(EventRecord).where(EventRecord.wallet == wallet)):
        if (row.signature, row.token) in covered:
            continue  # the chain-derived event for the same signature and mint takes precedence
        p = row.payload
        fills.append(Fill(signature=row.signature, event_index=row.event_index, token=row.token,
                          timestamp=datetime.fromisoformat(row.timestamp), kind=p["kind"],
                          quantity=Decimal(p["quantity"]), source_url=p["source_url"], evidence=row.evidence,
                          **{k: Decimal(p[k]) if p.get(k) is not None else None for k in
                             ("gross_usd", "fees_usd", "market_cap_usd")}))
    return fills


def wallet_analysis(session, wallet: Wallet, filters: Filters, as_of: datetime | None = None):
    as_of = as_of or default_as_of(session, wallet.address)
    fills = wallet_fills(session, wallet.address)
    complete = history_complete(session.get(SyncState, wallet.address), as_of, filters.analysis_days)
    result = analyze(wallet.address, fills, filters, as_of, history_complete=complete)
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


ISSUE_NAMES = {"missing_historical_usd_or_fees": "missing_sol_quote"}


def _sol_fill(t: Trade) -> Fill:
    d = t.details
    sol = Decimal(d["quote_amount"]) if d["quote_asset"] == "SOL" and d["quote_amount"] is not None else None
    return Fill(signature=t.signature, event_index=t.event_index, token=t.mint,
                timestamp=datetime.fromtimestamp(t.block_time, timezone.utc), kind=t.kind,
                quantity=Decimal(d["token_amount"]), gross_usd=sol,
                fees_usd=Decimal(d["network_fee_sol"]) + Decimal(d.get("tip_sol", "0")),
                market_cap_usd=None, source_url=f"https://solscan.io/tx/{t.signature}", slot=t.slot,
                evidence={**d["evidence"], "quote_asset": d["quote_asset"], "issues": d["issues"]})


def chain_positions(session, wallet: str, as_of: datetime, days: int) -> dict:
    """Positions rebuilt from chain-derived trades, denominated in SOL. Exploratory, not a ranking input.

    Reuses the weighted-average accounting of `analytics.reconstruct`; its USD fields carry SOL here.
    """

    rows = list(session.scalars(select(Trade).where(Trade.wallet == wallet)
                                .order_by(Trade.block_time, Trade.signature, Trade.event_index)))
    undated = sum(t.block_time is None for t in rows)
    positions = reconstruct([_sol_fill(t) for t in rows if t.block_time is not None], as_of)
    since = as_of - timedelta(days=days)
    output = []
    for p in positions:
        issues = {ISSUE_NAMES.get(i, i) for i in p.issues}
        for f in p.fills:
            if f.evidence["trade"] == "inferred":
                issues.add("inferred_trade")
            if f.kind in ("buy", "sell") and f.evidence["quote_asset"] not in ("SOL", None):
                issues.add("non_sol_quote")
            issues.update(f.evidence["issues"])
        buys = [f for f in p.fills if f.kind == "buy" and f.gross_usd is not None]
        bought = sum((f.quantity for f in buys), Decimal(0))
        output.append({
            "token": p.token, "cycle": p.cycle, "status": "closed" if p.closed_at else "open",
            "opened_at": p.opened_at.isoformat(), "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            "remaining_quantity": str(p.quantity),
            "remaining_cost_sol": _text(p.basis) if p.money_known else None,
            "realized_cost_sol": _text(p.allocated_cost) if p.money_known else None,
            "realized_proceeds_sol": _text(p.proceeds) if p.money_known else None,
            "realized_pnl_sol": _text(p.pnl), "realized_roi_pct": _text(p.roi),
            "avg_entry_price_sol": _text(sum((f.gross_usd for f in buys), Decimal(0)) / bought) if bought else None,
            "evaluable": p.evaluable and not issues, "issues": sorted(issues),
            "fills": [{"signature": f.signature, "event_index": f.event_index, "kind": f.kind,
                       "timestamp": f.timestamp.isoformat(), "quantity": str(f.quantity),
                       "quote_sol": _text(f.gross_usd), "fees_sol": str(f.fees_usd),
                       "trade": f.evidence["trade"], "transaction_url": f.source_url} for f in p.fills],
        })
    closed = [p for p in output if p["status"] == "closed" and p["closed_at"] >= since.isoformat()]
    evaluable = [p for p in closed if p["evaluable"]]
    pnls = [Decimal(p["realized_pnl_sol"]) for p in evaluable]
    cost = sum((Decimal(p["realized_cost_sol"]) for p in evaluable), Decimal(0))
    wins = sum(v > 0 for v in pnls)
    rois = sorted(Decimal(p["realized_roi_pct"]) for p in evaluable)
    low, high = wilson(wins, len(evaluable))
    return {
        "wallet": wallet, "as_of": as_of.isoformat(), "window_start": since.isoformat(), "denomination": "SOL",
        "normalizer": NORMALIZER_VERSION, "trades": len(rows), "undated_trades": undated,
        "summary": {"closed_positions": len(closed), "evaluable_positions": len(evaluable),
                    "open_positions": sum(p["status"] == "open" for p in output),
                    "wins": wins, "losses": sum(v < 0 for v in pnls),
                    "win_rate": wins / len(evaluable) if evaluable else None, "win_rate_ci95": [low, high],
                    "net_realized_pnl_sol": _text(sum(pnls, Decimal(0))) if evaluable else None,
                    "aggregate_realized_roi_pct": _text(sum(pnls, Decimal(0)) / cost * 100) if cost else None,
                    "median_roi_pct": _text(rois[len(rois) // 2]) if rois else None},
        "positions": output,
        "limitations": ["Denominado en SOL: el ROI en USD difiere si el precio de SOL cambió durante la posición.",
                        "Sin precios USD ni market cap histórico: no alimenta el ranking validado.",
                        "Propinas (Jito) y rent pagado a cuentas ajenas se cuentan dentro del costo del swap.",
                        "Win rate por posición, sin agrupar por token ni ajustar correlación; exploratorio."],
    }
