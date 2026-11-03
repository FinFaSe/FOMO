import hmac
import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update

from .adapters.fomo import FomoAPI
from .adapters.geckoterminal import GeckoTerminal
from .adapters.prices import CoinbaseCandles
from .adapters.quotes import DexScreenerQuotes
from .adapters.solana import ProviderError, SolanaRPC, digest, observe
from .alerts import KINDS as ALERT_KINDS
from .alerts import Notifier, alert_json, unread_count
from .alerts import settings as alert_settings
from .backtest import historical_copy, prospective_report
from .db import (
    Alert,
    AnalysisSnapshot,
    DiscoverySeed,
    EarlyBuy,
    Job,
    Observation,
    RawTransaction,
    SourceCapture,
    TokenMint,
    TokenQuote,
    Trade,
    Wallet,
    WalletFunding,
    database,
    now_iso,
)
from .discovery import add_seed, candidates, scan_seed, seed_json
from .entities import enqueue_funding, entities, entity_index, funding_json, lookup_funding
from .history import chart, enqueue_history, history_json
from .jobs import Worker, cancel, enqueue, ensure_idle, job_json, run_inline, worker_status
from .portfolio import open_positions
from .pricing import price_trades
from .quality import quality
from .ranking import current_analysis, ranking
from .schemas import (
    AlertsRead,
    BacktestRequest,
    DiscoveryRequest,
    Filters,
    FomoResolve,
    JobRequest,
    LedgerImport,
    SeedCreate,
    SyncRequest,
    WalletCreate,
    WalletUpdate,
    address,
    hook_signatures,
    utc,
)
from .service import chain_positions, import_ledger, renormalize, verify_ledger, wallet_analysis, wallet_fills
from .tokens import PUMP_PROGRAM, pump_bonding_curve
from .watch import Watcher, push_signatures


def create_app(db_url: str | None = None, rpc=None, fomo=None, prices=None, quotes=None, history=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        # Optional single-process mode for local use; production runs `python -m tracker.worker`.
        stop = threading.Event()
        if os.getenv("TRACKER_EMBEDDED_WORKER") == "1":
            worker = Worker(sessions, rpc, prices, quotes=quotes, history=history)
            threading.Thread(target=worker.run_forever, args=(stop,), daemon=True, name="tracker-worker").start()
            watcher = Watcher(sessions, rpc, prices, quotes=quotes)
            if watcher.interval > 0:
                threading.Thread(target=watcher.run_forever, args=(stop,), daemon=True,
                                 name="tracker-watcher").start()
        yield
        stop.set()

    app = FastAPI(title="FOMO Smart Money Tracker", version="0.1.0", lifespan=lifespan)
    engine, sessions = database(db_url)
    app.state.engine = engine
    app.state.sessions = sessions
    rpc = rpc or SolanaRPC()
    fomo = fomo or FomoAPI()
    prices = prices or CoinbaseCandles()
    quotes = quotes or DexScreenerQuotes()
    history = history or GeckoTerminal()
    app.state.prices, app.state.quotes, app.state.history = prices, quotes, history
    write_lock = threading.Lock()
    allowed_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins,
                      allow_methods=["GET", "POST", "PATCH"], allow_headers=["Content-Type", "X-Tracker-Token"])

    @app.middleware("http")
    async def body_and_access_limits(request: Request, call_next):
        token = os.getenv("TRACKER_ACCESS_TOKEN")
        # Provider webhooks cannot send the dashboard token; they authenticate with their own secret.
        exempt = request.url.path == "/api/health" or request.url.path.startswith("/api/hooks/")
        if token and not exempt and request.method != "OPTIONS":
            actual = request.headers.get("x-tracker-token", "")
            if not hmac.compare_digest(actual, token):
                return JSONResponse(status_code=401, content={"detail": "Acceso no autorizado"})
        if request.method in ("POST", "PATCH"):
            try:
                if int(request.headers.get("content-length", "0")) > 3_000_000:
                    return JSONResponse(status_code=413, content={"detail": "Límite de importación: 3 MB"})
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Content-Length inválido"})
            body = await request.body()
            if len(body) > 3_000_000:
                return JSONResponse(status_code=413, content={"detail": "Límite de importación: 3 MB"})
            # Serialize write operations in the single-process local deployment (including provider calls).
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    def session():
        with sessions() as s:
            yield s

    def write_guard():
        if not write_lock.acquire(blocking=False):
            raise HTTPException(409, "Hay otra operación de escritura en curso; espera a que termine")
        try:
            yield
        finally:
            write_lock.release()

    def get_wallet(s, value):
        try:
            address(value)
        except ValueError:
            raise HTTPException(422, "Dirección Solana inválida") from None
        row = s.get(Wallet, value)
        if not row:
            raise HTTPException(404, "Wallet no encontrada")
        return row

    def wallet_json(w):
        return {"address": w.address, "chain": w.chain, "label": w.label, "favorite": w.favorite,
                "identity": w.identity, "created_at": w.created_at, "sync": w.sync}

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    @app.get("/api/health")
    def health():
        with sessions() as s:
            s.execute(select(1)).scalar_one()
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/api/config")
    def config():
        return {"default_filters": Filters().model_dump(mode="json"), "chain": "solana",
                "fomo_api_configured": bool(fomo.key), "rpc_source": rpc.source,
                "price_source": getattr(prices, "source", None),
                "alerts": {**alert_settings(), "webhook_configured": Notifier().configured,
                           "watch_seconds": float(os.getenv("TRACKER_WATCH_SECONDS", "20")),
                           "inbound_hook_configured": bool(os.getenv("TRACKER_HOOK_SECRET"))},
                "data_mode": "real_only", "ranking_requires": ["complete_normalized_history",
                  "verified_swaps", "verified_historical_usd_and_fees", "verified_historical_market_cap"],
                "limitations": ["FOMO API es independiente y no oficial; su historial es incompleto.",
                                "RPC público conserva movimientos; no inventa precios históricos.",
                                "SOL/USD por vela de 1 minuto de Coinbase; market cap verificado solo para mints de pump.fun."]}

    @app.get("/api/wallets")
    def wallets(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), s=Depends(session)):
        rows = s.scalars(select(Wallet).order_by(Wallet.created_at, Wallet.address).limit(limit).offset(offset))
        return {"total": s.scalar(select(func.count()).select_from(Wallet)),
                "items": [wallet_json(w) for w in rows], "limit": limit, "offset": offset}

    @app.post("/api/wallets", status_code=201, dependencies=[Depends(write_guard)])
    def add_wallet(body: WalletCreate, s=Depends(session)):
        w = s.get(Wallet, body.address)
        if not w:
            w = Wallet(address=body.address, label=body.label)
            s.add(w)
            s.commit()
        enqueue_funding(s, [body.address], "tracked")
        return wallet_json(w)

    @app.patch("/api/wallets/{wallet}", dependencies=[Depends(write_guard)])
    def favorite(wallet: str, body: WalletUpdate, s=Depends(session)):
        w = get_wallet(s, wallet)
        w.favorite = body.favorite
        s.commit()
        return wallet_json(w)

    @app.post("/api/wallets/{wallet}/sync", dependencies=[Depends(write_guard)])
    def sync(wallet: str, body: SyncRequest, s=Depends(session)):
        # Synchronous, bounded run (scripts, tests). The dashboard queues background jobs instead.
        return run_inline(s, get_wallet(s, wallet), rpc, prices, body)

    @app.post("/api/wallets/{wallet}/jobs", status_code=202)
    def queue_sync(wallet: str, body: JobRequest, s=Depends(session)):
        get_wallet(s, wallet)
        job, created = enqueue(s, wallet, body)
        return {"job": job_json(job), "created": created}

    @app.get("/api/jobs")
    def jobs(wallet: str | None = None, state: str | None = None, limit: int = Query(20, ge=1, le=100),
             s=Depends(session)):
        query = select(Job).order_by(Job.id.desc()).limit(limit)
        if wallet:
            query = query.where(Job.wallet == wallet)
        if state:
            query = query.where(Job.state == state)
        return {"items": [job_json(j) for j in s.scalars(query)], "worker": worker_status(s)}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: int, s=Depends(session)):
        row = s.get(Job, job_id)
        if not row:
            raise HTTPException(404, "Trabajo no encontrado")
        return job_json(row)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int, s=Depends(session)):
        row = s.get(Job, job_id)
        if not row:
            raise HTTPException(404, "Trabajo no encontrado")
        return job_json(cancel(s, row))

    @app.get("/api/worker")
    def worker(s=Depends(session)):
        return worker_status(s)

    @app.post("/api/wallets/{wallet}/price", dependencies=[Depends(write_guard)])
    def price(wallet: str, s=Depends(session)):
        get_wallet(s, wallet)
        ensure_idle(s, wallet)
        return price_trades(s, wallet, prices)

    @app.post("/api/wallets/{wallet}/ledger", dependencies=[Depends(write_guard)])
    def ledger(wallet: str, body: LedgerImport, s=Depends(session)):
        get_wallet(s, wallet)
        return import_ledger(s, wallet, body)

    @app.post("/api/wallets/{wallet}/verify", dependencies=[Depends(write_guard)])
    def verify(wallet: str, limit: int = Query(50, ge=1, le=50), offset: int = Query(0, ge=0),
               s=Depends(session)):
        get_wallet(s, wallet)
        ensure_idle(s, wallet)
        return verify_ledger(s, wallet, rpc, limit=limit, offset=offset)

    @app.get("/api/wallets/{wallet}/observations")
    def observations(wallet: str, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                     s=Depends(session)):
        get_wallet(s, wallet)
        rows = s.scalars(select(Observation).where(Observation.wallet == wallet)
                         .order_by(Observation.id.desc()).limit(limit).offset(offset))
        count = s.scalar(select(func.count()).select_from(Observation).where(Observation.wallet == wallet))
        return {"total": count, "items": [{"signature": r.signature, "transaction_url":
                  f"https://solscan.io/tx/{r.signature}", **r.details} for r in rows]}

    @app.get("/api/wallets/{wallet}/trades")
    def trades(wallet: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               mint: str | None = None, s=Depends(session)):
        get_wallet(s, wallet)
        where = [Trade.wallet == wallet]
        if mint:
            try:
                where.append(Trade.mint == address(mint))
            except ValueError:
                raise HTTPException(422, "Mint Solana inválido") from None
        rows = s.scalars(select(Trade).where(*where)
                         .order_by(Trade.block_time.desc(), Trade.signature.desc(), Trade.event_index)
                         .limit(limit).offset(offset))
        count = s.scalar(select(func.count()).select_from(Trade).where(*where))
        return {"total": count, "items": [
            {"signature": t.signature, "event_index": t.event_index, "mint": t.mint, "kind": t.kind,
             "slot": t.slot, "block_time": t.block_time, "normalizer": t.normalizer,
             "transaction_url": f"https://solscan.io/tx/{t.signature}", **t.details} for t in rows]}

    @app.post("/api/wallets/{wallet}/normalize", dependencies=[Depends(write_guard)])
    def normalize_wallet(wallet: str, s=Depends(session)):
        get_wallet(s, wallet)
        ensure_idle(s, wallet)
        rebuilt = renormalize(s, wallet)
        rebuilt["pricing"] = price_trades(s, wallet, None)  # cached candles only: no network
        return rebuilt

    @app.get("/api/wallets/{wallet}/chain-positions")
    def wallet_chain_positions(wallet: str, days: int = Query(90, ge=1, le=3650), as_of: datetime | None = None,
                               s=Depends(session)):
        try:
            timestamp = utc(as_of) if as_of else datetime.now(timezone.utc).replace(microsecond=0)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        get_wallet(s, wallet)
        return chain_positions(s, wallet, timestamp, days)

    @app.get("/api/wallets/{wallet}/open-positions")
    def wallet_open_positions(wallet: str, refresh: bool = True, s=Depends(session)):
        get_wallet(s, wallet)
        return open_positions(s, wallet, wallet_fills(s, wallet), quotes, refresh=refresh)

    @app.get("/api/tokens/{mint}")
    def token(mint: str, s=Depends(session)):
        try:
            address(mint)
        except ValueError:
            raise HTTPException(422, "Mint Solana inválido") from None
        facts, quote = s.get(TokenMint, mint), s.get(TokenQuote, mint)
        if facts is None and quote is None:
            raise HTTPException(404, "Token sin datos almacenados")
        fields = ("exists", "program", "decimals", "supply_raw", "mint_authority", "freeze_authority",
                  "extensions", "pump_bonding_curve", "pump_origin", "source", "fetched_at")
        quote_fields = ("found", "price_usd", "liquidity_usd", "market_cap_usd", "fdv_usd", "pair_address",
                        "dex_id", "symbol", "source", "fetched_at")
        return {"mint": mint, "facts": {k: getattr(facts, k) for k in fields} if facts else None,
                "quote": {k: getattr(quote, k) for k in quote_fields} if quote else None}

    @app.get("/api/wallets/{wallet}/analysis")
    def analysis(wallet: str, filters: Annotated[Filters, Depends()], as_of: datetime | None = None,
                 s=Depends(session)):
        try:
            timestamp = utc(as_of) if as_of else None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        row = get_wallet(s, wallet)
        if timestamp is None:
            # Default: as of the last completed sync, served from the precomputed entry when current.
            return current_analysis(s, row, filters)
        return wallet_analysis(s, row, filters, timestamp)

    @app.get("/api/quality")
    def data_quality(wallet: Annotated[list[str], Query()] = [], days: int = Query(90, ge=1, le=3650),  # noqa: B006
                     s=Depends(session)):
        """What the tracker sees and cannot see in the tracked wallets' data (local data only)."""
        for address_ in wallet:
            get_wallet(s, address_)
        return quality(s, wallet or None, days=days)

    @app.get("/api/rankings")
    def rankings(filters: Annotated[Filters, Depends()], limit: int = Query(20, ge=1, le=100),
                 offset: int = Query(0, ge=0), s=Depends(session)):
        # Bounded local research dataset; no source-reported leaderboard PnL is used. Each wallet is analyzed
        # as of its own last completed sync (precomputed; anything outdated is recomputed before answering).
        return ranking(s, filters, limit=limit, offset=offset)

    @app.get("/api/snapshots/{snapshot_id}")
    def snapshot(snapshot_id: str, s=Depends(session)):
        result = s.get(AnalysisSnapshot, snapshot_id)
        if not result:
            raise HTTPException(404, "Snapshot no encontrado")
        return result.result

    @app.get("/api/evidence/{signature}")
    def transaction_evidence(signature: str, s=Depends(session)):
        raw = s.get(RawTransaction, signature)
        if not raw:
            raise HTTPException(404, "Evidencia no almacenada")
        return {"signature": signature, "source": raw.source, "fetched_at": raw.fetched_at,
                "payload_sha256": raw.digest, "payload": raw.payload}

    @app.post("/api/discovery/seeds", status_code=201)
    def add_discovery_seed(body: SeedCreate, s=Depends(session)):
        seed, created = add_seed(s, body)
        return {"seed": seed_json(seed), "created": created}

    @app.get("/api/discovery/seeds")
    def discovery_seeds(s=Depends(session)):
        rows = s.scalars(select(DiscoverySeed).order_by(DiscoverySeed.created_at.desc()))
        return {"items": [seed_json(r) for r in rows]}

    @app.post("/api/discovery/seeds/{mint}/scan", dependencies=[Depends(write_guard)])
    def scan_discovery_seed(mint: str, s=Depends(session)):
        # Synchronous, bounded scan (scripts, tests); the worker scans queued seeds in the background.
        seed = s.get(DiscoverySeed, mint)
        if not seed:
            raise HTTPException(404, "Semilla no encontrada")
        if seed.state == "scanning":
            raise HTTPException(409, "La semilla se está escaneando")
        seed.state = "scanning"
        s.commit()
        report = scan_seed(s, rpc, prices, quotes, seed)
        return {"report": report, "seed": seed_json(seed)}

    @app.get("/api/discovery/seeds/{mint}/buyers")
    def discovery_buyers(mint: str, limit: int = Query(100, ge=1, le=300), s=Depends(session)):
        if not s.get(DiscoverySeed, mint):
            raise HTTPException(404, "Semilla no encontrada")
        rows = s.scalars(select(EarlyBuy).where(EarlyBuy.mint == mint).order_by(EarlyBuy.rank).limit(limit))
        return {"items": [{"wallet": r.wallet, "rank": r.rank, "signature": r.signature, "slot": r.slot,
                           "block_time": r.block_time, "transaction_url": f"https://solscan.io/tx/{r.signature}",
                           **r.details} for r in rows]}

    @app.get("/api/discovery/candidates")
    def discovery_candidates(multiple: Decimal = Query(Decimal(10), gt=1, le=10000),
                             min_seeds: int = Query(1, ge=1, le=100), include_flagged: bool = False,
                             limit: int = Query(100, ge=1, le=500), s=Depends(session)):
        return candidates(s, multiple=multiple, min_seeds=min_seeds, include_flagged=include_flagged, limit=limit)

    @app.post("/api/discovery/candidates/{wallet}/study", status_code=202)
    def study_candidate(wallet: str, s=Depends(session)):
        try:
            address(wallet)
        except ValueError:
            raise HTTPException(422, "Dirección Solana inválida") from None
        row = s.get(Wallet, wallet)
        if row is None:
            seeds = len(set(s.scalars(select(EarlyBuy.mint).where(EarlyBuy.wallet == wallet))))
            row = Wallet(address=wallet, label=f"Descubierta · {seeds} semillas")
            s.add(row)
            s.commit()
        job, created = enqueue(s, wallet, JobRequest())
        enqueue_funding(s, [wallet], "tracked")
        return {"wallet": wallet_json(row), "job": job_json(job), "created": created}

    @app.get("/api/alerts")
    def alerts(since_id: int = Query(0, ge=0), kind: str | None = None, unread: bool = False,
               limit: int = Query(50, ge=1, le=200), s=Depends(session)):
        query = select(Alert).where(Alert.id > since_id).order_by(Alert.id.desc()).limit(limit)
        if kind:
            if kind not in ALERT_KINDS:
                raise HTTPException(422, "Tipo de alerta inválido")
            query = query.where(Alert.kind == kind)
        if unread:
            query = query.where(Alert.read.is_(False))
        return {"items": [alert_json(a) for a in s.scalars(query)], "unread": unread_count(s),
                "settings": alert_settings(), "webhook_configured": Notifier().configured}

    @app.post("/api/alerts/read")
    def mark_alerts_read(body: AlertsRead, s=Depends(session)):
        query = update(Alert).where(Alert.read.is_(False))
        if not body.all:
            query = query.where(Alert.id.in_(body.ids))
        changed = s.execute(query.values(read=True)).rowcount
        s.commit()
        return {"marked": changed, "unread": unread_count(s)}

    @app.post("/api/backtest/historical")
    def backtest_historical(body: BacktestRequest, s=Depends(session)):
        wallets = body.wallets or list(s.scalars(select(Wallet.address).where(Wallet.favorite.is_(True)))) \
            or list(s.scalars(select(Wallet.address)))
        if not wallets:
            raise HTTPException(422, "No hay wallets para simular: añade alguna o indícalas")
        return historical_copy(s, wallets, delays=body.delays, size_sol=body.size_sol, slippage_bps=body.slippage_bps,
                               impact_bps_per_sol=body.impact_bps_per_sol, tx_cost_sol=body.tx_cost_sol,
                               max_staleness_seconds=body.max_staleness_seconds, since_days=body.since_days,
                               use_candles=body.use_candles)

    @app.post("/api/backtest/prices", status_code=202)
    def backtest_prices(body: BacktestRequest, s=Depends(session)):
        """Queue pool candles around every traded token of the wallets (the worker downloads them)."""
        wallets = body.wallets or list(s.scalars(select(Wallet.address).where(Wallet.favorite.is_(True)))) \
            or list(s.scalars(select(Wallet.address)))
        since = int(datetime.now(timezone.utc).timestamp()) - body.since_days * 86400
        clusters: dict[str, list[list[int]]] = {}
        for t in s.scalars(select(Trade).where(Trade.wallet.in_(wallets), Trade.kind.in_(("buy", "sell")),
                                               Trade.block_time >= since).order_by(Trade.block_time)):
            groups = clusters.setdefault(t.mint, [])
            if groups and t.block_time - groups[-1][1] <= 86400:
                groups[-1][1] = t.block_time
            else:
                groups.append([t.block_time, t.block_time])
        queued = []
        for mint, groups in clusters.items():
            for start, end in groups:
                row = enqueue_history(s, mint, start - 1800, end + 6 * 3600, "backtest")
                if row is not None:
                    queued.append(history_json(row))
        return {"mints": len(clusters), "requests": queued}

    @app.get("/api/prices/{mint}/chart")
    def price_chart(mint: str, wallet: str | None = None, start: int | None = Query(None, alias="from"),
                    end: int | None = Query(None, alias="to"), s=Depends(session)):
        try:
            address(mint)
            if wallet:
                address(wallet)
        except ValueError:
            raise HTTPException(422, "Dirección Solana inválida") from None
        return chart(s, mint, wallet, start, end)

    @app.post("/api/prices/{mint}/history", status_code=202)
    def queue_price_history(mint: str, start: int = Query(..., alias="from"), end: int = Query(..., alias="to"),
                            s=Depends(session)):
        try:
            address(mint)
        except ValueError:
            raise HTTPException(422, "Dirección Solana inválida") from None
        if end <= start:
            raise HTTPException(422, "El rango de tiempo es inválido")
        row = enqueue_history(s, mint, start, end, "manual")
        if row is None:
            raise HTTPException(422, "Rango fuera del historial disponible (últimos ~6 meses)")
        return history_json(row)

    @app.get("/api/backtest/prospective")
    def backtest_prospective(slippage_bps: Decimal = Query(Decimal(100), ge=0, le=5000), s=Depends(session)):
        return prospective_report(s, slippage_bps=slippage_bps)

    @app.post("/api/hooks/transactions")
    def hook_transactions(payload: Annotated[list | dict, Body()], request: Request, s=Depends(session)):
        secret = os.getenv("TRACKER_HOOK_SECRET")
        if not secret:
            raise HTTPException(404, "Webhook entrante no configurado (TRACKER_HOOK_SECRET)")
        if not hmac.compare_digest(request.headers.get("x-tracker-hook-secret", ""), secret):
            raise HTTPException(401, "Secreto de webhook inválido")
        signatures = hook_signatures(payload)
        if not signatures:
            raise HTTPException(422, "No hay firmas Solana válidas en el cuerpo")
        return push_signatures(s, rpc, prices, signatures, "webhook")

    @app.get("/api/funding/{target}")
    def funding(target: str, s=Depends(session)):
        row = s.get(WalletFunding, target)
        if not row:
            raise HTTPException(404, "Sin búsqueda de financiación para esta dirección")
        return funding_json(row)

    @app.post("/api/funding/{target}", status_code=202)
    def queue_funding(target: str, s=Depends(session)):
        try:
            address(target)
        except ValueError:
            raise HTTPException(422, "Dirección Solana inválida") from None
        enqueue_funding(s, [target], "manual")
        return funding_json(s.get(WalletFunding, target))

    @app.post("/api/funding/{target}/lookup", dependencies=[Depends(write_guard)])
    def lookup_funding_now(target: str, s=Depends(session)):
        # Synchronous, bounded lookup (scripts, tests); the worker processes queued lookups in the background.
        try:
            address(target)
        except ValueError:
            raise HTTPException(422, "Dirección Solana inválida") from None
        row = s.get(WalletFunding, target) or WalletFunding(address=target, requested_by="manual")
        row.state, row.funder, row.funder_kind, row.reason = "scanning", None, None, None
        s.add(row)
        s.commit()
        return funding_json(lookup_funding(s, rpc, row))

    @app.get("/api/entities")
    def linked_entities(min_size: int = Query(2, ge=2, le=1000), s=Depends(session)):
        items = entities(s, min_size=min_size)
        return {"items": items, "total": len(items),
                "notice": "Vínculos = indicios de control común (financiación o compras juntas), no prueba."}

    @app.get("/api/wallets/{wallet}/links")
    def wallet_links(wallet: str, s=Depends(session)):
        get_wallet(s, wallet)
        entity = entity_index(s).get(wallet)
        funded = [funding_json(r) for r in s.scalars(select(WalletFunding).where(WalletFunding.funder == wallet))]
        return {"funding": funding_json(s.get(WalletFunding, wallet)), "entity": entity, "funded": funded}

    @app.post("/api/discovery", dependencies=[Depends(write_guard)])
    def discovery(body: DiscoveryRequest, s=Depends(session)):
        candidates = set()
        evidence = []
        # pump.fun trades touch the bonding curve, not necessarily the mint: scan the curve when it exists.
        curve = pump_bonding_curve(body.token)
        account = rpc.accounts([curve], encoding="base64", data_slice={"offset": 0, "length": 0})[0]
        scanned = curve if account and account.get("owner") == PUMP_PROGRAM else body.token
        for item in rpc.signatures(scanned, limit=body.limit):
            if item.get("err"):
                continue
            tx = rpc.transaction(item["signature"])
            details = observe(tx, body.token)
            if details["status"] != "finalized":
                continue
            for candidate in details["signers"]:
                try:
                    address(candidate)
                except ValueError:
                    continue
                candidates.add(candidate)
            evidence.append(item["signature"])
        for candidate in sorted(candidates):
            if not s.get(Wallet, candidate):
                s.add(Wallet(address=candidate, label="Candidato on-chain"))
        s.commit()
        return {"candidates": sorted(candidates), "evidence_signatures": evidence, "scanned_address": scanned,
                "classification": "unverified_candidates", "coverage": "bounded_seed_scan",
                "notice": "Firmantes recientes, no ganadores demostrados. Para compradores tempranos y su cruce "
                          "entre tokens usa las semillas de descubrimiento (/api/discovery/seeds)."}

    @app.post("/api/integrations/fomo/resolve", dependencies=[Depends(write_guard)])
    def fomo_resolve(body: FomoResolve, s=Depends(session)):
        data = fomo.resolve(body.handle)
        try:
            wallet_address = address(data["wallets"]["solana"])
        except (ValueError, KeyError, TypeError):
            raise ProviderError("Wallet del proveedor inválida") from None
        w = s.get(Wallet, wallet_address)
        if not w:
            w = Wallet(address=wallet_address, label=body.handle.lstrip("@"))
            s.add(w)
        w.identity = {"handle": body.handle.lstrip("@"), "source": "fomoapi.io (independiente)",
                      "verification": "provider_reported_not_independently_verified", "captured_at": now_iso()}
        s.add(SourceCapture(source="https://api.fomoapi.io/v2/users/" + body.handle.lstrip("@"),
                            digest=digest(data), payload=json.dumps(data)))
        s.commit()
        return wallet_json(w)

    return app


app = create_app()
