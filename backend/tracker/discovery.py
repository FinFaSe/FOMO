"""Backward discovery: who bought seed tokens first, and who keeps doing it across tokens that ran.

For a pump.fun mint every curve trade touches its bonding-curve PDA, so listing that account's signatures back to
the oldest one (the token's creation) yields all of its earliest trades, which scanning the mint address misses.
Other tokens need their pool address. Each early buy records rank, minutes after launch and an entry market cap
(SOL/USD candle × pump.fun's fixed 1e9 supply, or the estimates used elsewhere). A seed's peak is only known from
below: max(cap seen in the scanned trades, current cap from DEX Screener, optional declared peak). A wallet "hit"
a seed when that lower bound is at least `multiple` × its own entry cap.

Seeds should include failures, not only winners; otherwise the hit rate inherits survivorship bias. Results are
candidates to study (sync + ranking), never validated traders.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import median

from sqlalchemy import delete, select, update

from .adapters.solana import ProviderError
from .analytics import wilson
from .db import DiscoverySeed, EarlyBuy, TokenMint, TokenQuote, Wallet, now_iso
from .normalize import normalize
from .portfolio import refresh_quotes
from .pricing import _market_cap, load_candles, sol_usd
from .schemas import SeedCreate
from .service import store_transaction
from .tokens import PUMP_PROGRAM, pump_bonding_curve, refresh_mints

PAGE = 1000
STALE_AFTER = timedelta(minutes=10)
FLAG_NAMES = ("creator", "launch_tx", "launch_slot")


def _text(value):
    return str(value) if value is not None else None


def seed_json(seed: DiscoverySeed) -> dict:
    return {"mint": seed.mint, "label": seed.label, "scan_address": seed.scan_address,
            "address_kind": seed.address_kind, "state": seed.state, "params": seed.params, "pages": seed.pages,
            "reached_start": seed.reached_start, "launch_signature": seed.launch_signature,
            "launch_slot": seed.launch_slot,
            "launch_time": (datetime.fromtimestamp(seed.launch_time, timezone.utc).isoformat()
                            if seed.launch_time else None),
            "creator": seed.creator, "outcome": seed.outcome, "declared_peak_cap_usd": seed.declared_peak_cap_usd,
            "declared_peak_source": seed.declared_peak_source, "error": seed.error,
            "created_at": seed.created_at, "finished_at": seed.finished_at}


def add_seed(session, body: SeedCreate) -> tuple[DiscoverySeed, bool]:
    seed = session.get(DiscoverySeed, body.mint)
    created = seed is None
    if created:
        seed = DiscoverySeed(mint=body.mint, scan_address=body.pool or pump_bonding_curve(body.mint),
                             address_kind="pool" if body.pool else "pump_bonding_curve",
                             params={"early_trades": body.early_trades, "max_pages": body.max_pages})
        session.add(seed)
    seed.label = body.label or seed.label
    if body.peak_market_cap_usd is not None:
        seed.declared_peak_cap_usd = str(body.peak_market_cap_usd)
        seed.declared_peak_source = body.peak_source_url
    if not created and seed.state in ("done", "failed"):
        # Re-adding queues a rescan; listing restarts so new evidence is included.
        seed.state, seed.error, seed.list_before, seed.reached_start, seed.oldest = "queued", None, None, False, None
    session.commit()
    return seed, created


def claim_seed(session, worker_id: str) -> DiscoverySeed | None:
    for seed in list(session.scalars(select(DiscoverySeed).where(DiscoverySeed.state == "queued")
                                     .order_by(DiscoverySeed.created_at).limit(5))):
        won = session.execute(update(DiscoverySeed).where(DiscoverySeed.mint == seed.mint,
                                                          DiscoverySeed.state == "queued")
                              .values(state="scanning", locked_by=worker_id, heartbeat_at=now_iso())).rowcount == 1
        session.commit()
        if won:
            session.refresh(seed)
            return seed
    return None


def recover_stale_seeds(session) -> int:
    cutoff = (datetime.now(timezone.utc) - STALE_AFTER).isoformat()
    stale = list(session.scalars(select(DiscoverySeed).where(DiscoverySeed.state == "scanning",
                                                             DiscoverySeed.heartbeat_at < cutoff)))
    for seed in stale:
        seed.state, seed.locked_by = "queued", None
    session.commit()
    return len(stale)


def _beat(session, seed):
    seed.heartbeat_at = now_iso()
    session.commit()


def scan_seed(session, rpc, prices, quotes, seed: DiscoverySeed) -> dict:
    """One bounded scan step. Returns a report; the seed row carries state across runs."""
    report = {"pages": 0, "transactions": 0, "early_buys": 0, "state": None, "issues": []}
    try:
        _scan(session, rpc, prices, quotes, seed, report)
    except ProviderError as exc:
        session.rollback()
        seed.state, seed.error, seed.locked_by = "failed", exc.message, None
        seed.finished_at = now_iso()
        session.commit()
        report["state"], report["error"] = "failed", exc.message
    return report


def _scan(session, rpc, prices, quotes, seed, report):
    params = seed.params
    keep = params["early_trades"] + 50  # room for failed transactions among the oldest
    budget = params["max_pages"]
    while not seed.reached_start and budget > 0:
        page = rpc.signatures(seed.scan_address, before=seed.list_before, limit=PAGE)
        budget -= 1
        seed.pages += 1
        report["pages"] += 1
        if page:
            items = [{"signature": i["signature"], "slot": i.get("slot"), "err": bool(i.get("err"))} for i in page]
            seed.oldest = ((seed.oldest or []) + items)[-keep:]  # newest first: the tail is the oldest
            seed.list_before = page[-1]["signature"]
        if len(page) < PAGE:
            seed.reached_start = True
        _beat(session, seed)
    if not seed.reached_start:
        # Very active address: continue listing in a later run from the stored cursor.
        seed.state, seed.locked_by = "queued", None
        session.commit()
        report["state"] = "partial"
        report["issues"].append("page_budget_reached")
        return
    chronological = [i for i in reversed(seed.oldest or []) if not i["err"]][:params["early_trades"]]
    if not chronological:
        raise ProviderError("Sin actividad en la dirección escaneada; para tokens fuera de pump.fun indica el pool",
                            422)
    raws = []
    for item in chronological:
        raws.append(store_transaction(session, rpc, item["signature"]))
        report["transactions"] += 1
        _beat(session, seed)
    launch = raws[0]
    keys = launch.payload["transaction"]["message"]["accountKeys"]
    seed.launch_signature, seed.launch_slot, seed.launch_time = launch.signature, launch.slot, launch.block_time
    seed.creator = keys[0]["pubkey"] if isinstance(keys[0], dict) else keys[0]
    launch_programs = {ix.get("programId") for ix in launch.payload["transaction"]["message"].get("instructions", [])}
    # Scanning the pump.fun curve PDA and finding the program in the creation transaction proves the origin.
    pump = seed.address_kind == "pump_bonding_curve" and PUMP_PROGRAM in launch_programs
    if seed.address_kind == "pump_bonding_curve" and not pump:
        report["issues"].append("launch_without_pump_program")
    facts = None
    if not pump:
        try:
            refresh_mints(session, rpc, {seed.mint})
        except ProviderError as exc:
            session.rollback()
            report["issues"].append(f"mint: {exc.message}")
        facts = session.get(TokenMint, seed.mint)

    events = []  # (raw, signer, event)
    for raw in raws:
        tx_keys = raw.payload["transaction"]["message"]["accountKeys"]
        signers = [k["pubkey"] for k in tx_keys if isinstance(k, dict) and k.get("signer")]
        for signer in signers:
            for event in normalize(raw.payload, signer):
                if event["mint"] == seed.mint and event["kind"] in ("buy", "sell"):
                    events.append((raw, signer, event))
    times = [raw.block_time for raw, _, _ in events if raw.block_time is not None]
    cache, requests, issues = load_candles(session, prices, times) if times else ({}, 0, [])
    report["issues"] += issues

    def entry_cap(raw, event):
        if event["quote_asset"] != "SOL" or event["price"] is None or raw.block_time is None:
            return None, None, "unpriced"
        usd = sol_usd(cache, raw.block_time)
        if usd is None:
            return None, None, "unpriced"
        cap, method, _supply, status = _market_cap(event["price"] * usd, event["decimals"], pump, facts)
        return cap, method, status

    caps, rows = [], []
    for raw, signer, event in events:
        cap, method, status = entry_cap(raw, event)
        if cap is not None:
            caps.append(cap)
        if event["kind"] != "buy":
            continue
        flags = []
        if signer == seed.creator:
            flags.append("creator")
        if raw.signature == launch.signature:
            flags.append("launch_tx")
        elif raw.slot == launch.slot:
            flags.append("launch_slot")
        minutes = ((raw.block_time - launch.block_time) / 60
                   if raw.block_time is not None and launch.block_time is not None else None)
        rows.append(EarlyBuy(mint=seed.mint, wallet=signer, signature=raw.signature, event_index=event["event_index"],
                             slot=raw.slot, block_time=raw.block_time, rank=len(rows) + 1, details={
                                 "sol_amount": _text(event["quote_amount"]),
                                 "token_amount": str(event["token_amount"]), "price_sol": _text(event["price"]),
                                 "entry_cap_usd": _text(cap), "cap_method": method, "cap_status": status,
                                 "minutes_after_launch": minutes, "flags": flags, "trade": event["trade"],
                                 "venues": event["venues"]}))
    current = None
    if quotes is not None:
        try:
            refresh_quotes(session, quotes, {seed.mint})
        except ProviderError as exc:
            session.rollback()
            report["issues"].append(f"cotización: {exc.message}")
        quote = session.get(TokenQuote, seed.mint)
        if quote is not None and quote.found and quote.market_cap_usd is not None:
            current = Decimal(quote.market_cap_usd)
    declared = Decimal(seed.declared_peak_cap_usd) if seed.declared_peak_cap_usd else None
    scanned = max(caps) if caps else None
    sources = [(v, name) for v, name in ((scanned, "scanned_trades"), (current, "current_quote"),
                                         (declared, "declared")) if v is not None]
    peak, basis = max(sources) if sources else (None, None)
    launch_cap = next((Decimal(r.details["entry_cap_usd"]) for r in rows if r.details["entry_cap_usd"]), None)
    session.execute(delete(EarlyBuy).where(EarlyBuy.mint == seed.mint))
    session.add_all(rows)
    seed.outcome = {"pump_origin": pump, "launch_cap_usd": _text(launch_cap), "max_scanned_cap_usd": _text(scanned),
                    "current_cap_usd": _text(current), "declared_peak_cap_usd": _text(declared),
                    "peak_cap_lower_bound_usd": _text(peak), "peak_basis": basis,
                    "peak_multiple_from_launch": _text(peak / launch_cap) if peak and launch_cap else None,
                    "early_buys": len(rows), "early_buyers": len({r.wallet for r in rows}),
                    "transactions": len(raws), "price_requests": requests, "issues": report["issues"]}
    seed.state, seed.error, seed.locked_by, seed.finished_at = "done", None, None, now_iso()
    session.commit()
    report["state"], report["early_buys"] = "done", len(rows)


def candidates(session, *, multiple: Decimal = Decimal(10), min_seeds: int = 1, include_flagged: bool = False,
               limit: int = 100) -> dict:
    """Cross-reference early buyers across scanned seeds."""
    seeds = {s.mint: s for s in session.scalars(select(DiscoverySeed).where(DiscoverySeed.state == "done"))}
    first: dict[tuple[str, str], EarlyBuy] = {}
    for row in session.scalars(select(EarlyBuy).where(EarlyBuy.mint.in_(list(seeds))).order_by(EarlyBuy.rank)):
        first.setdefault((row.wallet, row.mint), row)
    tracked = set(session.scalars(select(Wallet.address)))
    by_wallet: dict[str, list] = {}
    for (wallet, mint), row in first.items():
        outcome = seeds[mint].outcome or {}
        peak = outcome.get("peak_cap_lower_bound_usd")
        entry = row.details.get("entry_cap_usd")
        status = "unknown"
        if peak is not None and entry is not None and Decimal(entry) > 0:
            status = "hit" if Decimal(peak) >= multiple * Decimal(entry) else "miss"
        by_wallet.setdefault(wallet, []).append({
            "mint": mint, "label": seeds[mint].label, "rank": row.rank, "signature": row.signature,
            "minutes_after_launch": row.details.get("minutes_after_launch"), "entry_cap_usd": entry,
            "entry_cap_status": row.details.get("cap_status"), "peak_cap_lower_bound_usd": peak,
            "peak_basis": outcome.get("peak_basis"),
            "multiple": _text(Decimal(peak) / Decimal(entry)) if status != "unknown" else None,
            "status": status, "flags": row.details.get("flags", [])})
    items = []
    for wallet, entries in by_wallet.items():
        flags = sorted({f for e in entries for f in e["flags"]})
        if (flags and not include_flagged) or len(entries) < min_seeds:
            continue
        hits = sum(e["status"] == "hit" for e in entries)
        misses = sum(e["status"] == "miss" for e in entries)
        low, high = wilson(hits, hits + misses)
        minutes = [e["minutes_after_launch"] for e in entries if e["minutes_after_launch"] is not None]
        items.append({"wallet": wallet, "seeds": len(entries), "hits": hits, "misses": misses,
                      "unknown": len(entries) - hits - misses, "hit_rate": hits / (hits + misses) if hits + misses
                      else None, "hit_rate_ci95": [low, high],
                      "declared_peak_hits": sum(e["status"] == "hit" and e["peak_basis"] == "declared"
                                                for e in entries),
                      "median_minutes_after_launch": median(minutes) if minutes else None,
                      "median_rank": median(e["rank"] for e in entries), "flags": flags,
                      "tracked": wallet in tracked, "entries": sorted(entries, key=lambda e: e["mint"])})
    items.sort(key=lambda i: (-i["hits"], -(i["hit_rate_ci95"][0] or 0), -i["seeds"], i["wallet"]))
    return {"items": items[:limit], "total": len(items), "seeds": len(seeds), "multiple": str(multiple),
            "notice": "Candidatos, no traders validados: sincroniza cada wallet para medirla con el ranking. "
                      "El pico de cada semilla es un límite inferior; incluye también semillas fallidas."}
