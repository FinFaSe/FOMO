import hmac
import json
import os
import threading
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from .adapters.fomo import FomoAPI
from .adapters.solana import ProviderError, SolanaRPC, digest, observe
from .db import AnalysisSnapshot, Observation, RawTransaction, SourceCapture, Wallet, database, now_iso
from .schemas import (
    DiscoveryRequest,
    Filters,
    FomoResolve,
    LedgerImport,
    SyncRequest,
    WalletCreate,
    WalletUpdate,
    address,
    utc,
)
from .service import import_ledger, ingest, verify_ledger, wallet_analysis


def create_app(db_url: str | None = None, rpc=None, fomo=None) -> FastAPI:
    app = FastAPI(title="FOMO Smart Money Tracker", version="0.1.0")
    engine, sessions = database(db_url)
    app.state.engine = engine
    rpc = rpc or SolanaRPC()
    fomo = fomo or FomoAPI()
    write_lock = threading.Lock()
    allowed_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins,
                      allow_methods=["GET", "POST", "PATCH"], allow_headers=["Content-Type", "X-Tracker-Token"])

    @app.middleware("http")
    async def body_and_access_limits(request: Request, call_next):
        token = os.getenv("TRACKER_ACCESS_TOKEN")
        if token and request.url.path != "/api/health" and request.method != "OPTIONS":
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
                "data_mode": "real_only", "ranking_requires": ["complete_normalized_history",
                  "verified_swaps", "verified_historical_usd_and_fees", "verified_historical_market_cap"],
                "limitations": ["FOMO API es independiente y no oficial; su historial es incompleto.",
                                "RPC público conserva movimientos; no inventa precios históricos."]}

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
        return wallet_json(w)

    @app.patch("/api/wallets/{wallet}", dependencies=[Depends(write_guard)])
    def favorite(wallet: str, body: WalletUpdate, s=Depends(session)):
        w = get_wallet(s, wallet)
        w.favorite = body.favorite
        s.commit()
        return wallet_json(w)

    @app.post("/api/wallets/{wallet}/sync", dependencies=[Depends(write_guard)])
    def sync(wallet: str, body: SyncRequest, s=Depends(session)):
        return ingest(s, get_wallet(s, wallet), rpc, body)

    @app.post("/api/wallets/{wallet}/ledger", dependencies=[Depends(write_guard)])
    def ledger(wallet: str, body: LedgerImport, s=Depends(session)):
        get_wallet(s, wallet)
        return import_ledger(s, wallet, body)

    @app.post("/api/wallets/{wallet}/verify", dependencies=[Depends(write_guard)])
    def verify(wallet: str, limit: int = Query(50, ge=1, le=50), offset: int = Query(0, ge=0),
               s=Depends(session)):
        get_wallet(s, wallet)
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

    @app.get("/api/wallets/{wallet}/analysis")
    def analysis(wallet: str, filters: Annotated[Filters, Depends()], as_of: datetime | None = None,
                 s=Depends(session)):
        try:
            timestamp = utc(as_of) if as_of else datetime.now(timezone.utc).replace(microsecond=0)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return wallet_analysis(s, get_wallet(s, wallet), filters, timestamp)

    @app.get("/api/rankings")
    def rankings(filters: Annotated[Filters, Depends()], limit: int = Query(20, ge=1, le=100),
                 offset: int = Query(0, ge=0), s=Depends(session)):
        timestamp = datetime.now(timezone.utc).replace(microsecond=0)
        results = []
        # Bounded local research dataset. No source-reported leaderboard PnL is used.
        for wallet in s.scalars(select(Wallet).order_by(Wallet.address)):
            r = wallet_analysis(s, wallet, filters, timestamp)
            if r["eligible"]:
                results.append({"address": wallet.address, "label": wallet.label,
                                "score": r["score"], "metrics": r["metrics"], "snapshot_id": r["snapshot_id"]})
        results.sort(key=lambda x: (-(x["score"]["verified_smart_money"] or 0), x["address"]))
        return {"items": results[offset:offset+limit], "total": len(results), "as_of": timestamp.isoformat(),
                "notice": "Solo historial completo y evidencia verificada; las importaciones no se autoverifican."}

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

    @app.post("/api/discovery", dependencies=[Depends(write_guard)])
    def discovery(body: DiscoveryRequest, s=Depends(session)):
        candidates = set()
        evidence = []
        for item in rpc.signatures(body.token, limit=body.limit):
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
        return {"candidates": sorted(candidates), "evidence_signatures": evidence,
                "classification": "unverified_candidates", "coverage": "bounded_seed_scan",
                "notice": "Firmantes, no ganadores demostrados. Incluye semillas fallidas y wallets manuales; "
                          "el escaneo por mint no enumera todos sus swaps ni elimina sesgo de supervivencia."}

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
