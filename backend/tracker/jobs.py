"""Background sync jobs. The queue lives in the database; a single worker process (or an embedded thread)
claims jobs with a conditional UPDATE, so a job is never run twice and one wallet never runs two jobs.

A sync job runs one budgeted incremental scan plus pricing. When the page or price budget ran out, the
worker queues a follow-up round (`until_complete`, up to `max_rounds`), so a long history is fetched over
several short jobs that interleave fairly with other wallets. Provider errors are retried with
exponential backoff. Watchlist wallets are refreshed periodically.
"""

import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from .adapters.solana import ProviderError
from .db import Job, Wallet, WorkerHeartbeat, now_iso
from .pricing import price_trades
from .schemas import JobRequest, SyncRequest
from .service import ingest

log = logging.getLogger("tracker.jobs")
ACTIVE = ("queued", "running", "cancel_requested")
RUNNING = ("running", "cancel_requested")
FINISHED = ("done", "failed", "cancelled")
STALE_AFTER = timedelta(minutes=10)
WORKER_ALIVE = timedelta(seconds=30)
MAX_ATTEMPTS = 5
RETRY_BASE_SECONDS = 30
PROGRESS_KEYS = ("state", "transactions", "trades", "already_known", "rpc_transaction_fetches", "pages",
                 "issues", "cursor")


class Cancelled(ProviderError):
    def __init__(self):
        super().__init__("Cancelado por el usuario", 499)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def job_json(job: Job) -> dict:
    return {"id": job.id, "kind": job.kind, "wallet": job.wallet, "state": job.state, "origin": job.origin,
            "round": job.round, "parent_id": job.parent_id, "attempts": job.attempts, "params": job.params,
            "run_after": job.run_after, "created_at": job.created_at, "started_at": job.started_at,
            "heartbeat_at": job.heartbeat_at, "finished_at": job.finished_at, "progress": job.progress,
            "result": job.result, "error": job.error}


def active_job(session, wallet: str, states=ACTIVE) -> Job | None:
    return session.scalar(select(Job).where(Job.wallet == wallet, Job.state.in_(states)).order_by(Job.id).limit(1))


def ensure_idle(session, wallet: str):
    """Refuse wallet-wide rewrites (normalize, price, verify, inline sync) while a scan is running."""
    if active_job(session, wallet, RUNNING):
        raise ProviderError("Hay una sincronización en curso para esta wallet; espera a que termine", 409)


def enqueue(session, wallet: str, request: JobRequest, *, origin: str = "manual",
            parent: Job | None = None) -> tuple[Job, bool]:
    """Queue a sync. Returns the existing active job instead of a duplicate (except for follow-ups)."""
    if parent is None:
        existing = active_job(session, wallet)
        if existing:
            return existing, False
    job = Job(kind="sync", wallet=wallet, params=request.model_dump(mode="json"), origin=origin,
              parent_id=parent.id if parent else None, round=parent.round + 1 if parent else 1,
              run_after=now_iso())
    session.add(job)
    session.commit()
    return job, True


def cancel(session, job: Job) -> Job:
    if job.state == "queued":
        job.state, job.finished_at = "cancelled", now_iso()
    elif job.state == "running":
        job.state = "cancel_requested"  # the runner stops after the transaction in progress
    elif job.state != "cancel_requested":
        raise ProviderError("El trabajo ya terminó", 409)
    session.commit()
    return job


def sync_and_price(session, wallet: Wallet, rpc, prices, request: SyncRequest, on_progress=None) -> dict:
    report = ingest(session, wallet, rpc, request, on_progress)
    try:
        report["pricing"] = price_trades(session, wallet.address, prices)
    except ProviderError as exc:
        # Prices are best effort; trades stay unpriced (and non-evaluable) until a later attempt.
        session.rollback()
        report["pricing"] = {"error": exc.message}
    wallet.sync = report
    session.commit()
    return report


class Progress:
    """Throttled progress + heartbeat for a running job; raises Cancelled when a cancel was requested."""

    def __init__(self, session, job: Job, interval: float = 1.0, beat=None):
        self.session, self.job, self.interval, self.beat, self.last = session, job, interval, beat, 0.0

    def __call__(self, report: dict):
        if time.monotonic() - self.last < self.interval:
            return
        self.last = time.monotonic()
        self.session.refresh(self.job)  # see a cancel requested from another connection or process
        if self.job.state == "cancel_requested":
            raise Cancelled()
        self.job.progress = {k: report.get(k) for k in PROGRESS_KEYS}
        self.job.heartbeat_at = now_iso()
        self.session.commit()
        if self.beat:
            self.beat()


def finish(session, job: Job, report: dict, *, chain: bool) -> Job | None:
    """Record the outcome. Returns a follow-up job when another round was queued."""
    session.refresh(job)
    job.result = report
    job.progress = {k: report.get(k) for k in PROGRESS_KEYS}
    job.heartbeat_at = now_iso()
    pricing = report.get("pricing") or {}
    params = JobRequest(**job.params)
    follow_up = None
    if job.state == "cancel_requested" or report.get("error") == Cancelled().message:
        job.state, job.error = "cancelled", None
    elif report.get("error"):
        job.error = report["error"]
        if not chain or job.attempts >= MAX_ATTEMPTS:
            job.state = "failed"
        else:
            # Transient provider failure: back off and retry; stored progress is kept by the cursors.
            delay = RETRY_BASE_SECONDS * 2 ** (job.attempts - 1)
            job.state, job.locked_by = "queued", None
            job.run_after = (_now() + timedelta(seconds=delay)).isoformat()
    else:
        job.state, job.error = "done", None
        more_pages = report.get("state") == "partial" and "page_budget_reached" in report.get("issues", [])
        more_prices = "price_request_budget_reached" in (pricing.get("issues") or [])
        if (chain and params.until_complete and not params.revalidate and (more_pages or more_prices)
                and job.round < params.max_rounds):
            follow_up = Job(kind="sync", wallet=job.wallet, params=job.params, origin="chain",
                            parent_id=job.id, round=job.round + 1, run_after=now_iso())
            session.add(follow_up)
    if job.state != "queued":
        job.finished_at = now_iso()
    session.commit()
    return follow_up


def run_inline(session, wallet: Wallet, rpc, prices, request: SyncRequest) -> dict:
    """Synchronous sync recorded as a running job, so the background worker never overlaps it."""
    ensure_idle(session, wallet.address)
    job = Job(kind="sync", wallet=wallet.address, state="running", params=JobRequest(
        **request.model_dump(), until_complete=False).model_dump(mode="json"), origin="api", locked_by="api",
              attempts=1, started_at=now_iso(), heartbeat_at=now_iso())
    session.add(job)
    session.commit()
    report = sync_and_price(session, wallet, rpc, prices, request, Progress(session, job))
    finish(session, job, report, chain=False)
    report["job_id"] = job.id
    return report


def claim(session, worker_id: str) -> Job | None:
    """Atomically take the oldest due job whose wallet has nothing running."""
    now = now_iso()
    busy = select(Job.wallet).where(Job.state.in_(RUNNING))
    candidates = list(session.scalars(select(Job).where(Job.state == "queued", Job.run_after <= now,
                                                        Job.wallet.not_in(busy))
                                      .order_by(Job.run_after, Job.id).limit(5)))
    for job in candidates:
        won = session.execute(update(Job).where(Job.id == job.id, Job.state == "queued")
                              .values(state="running", locked_by=worker_id, started_at=now, heartbeat_at=now,
                                      attempts=Job.attempts + 1)).rowcount == 1
        session.commit()
        if won:
            session.refresh(job)
            return job
    return None


def recover_stale(session) -> int:
    """Jobs whose runner stopped heartbeating (crash, restart) go back to the queue or end."""
    cutoff = (_now() - STALE_AFTER).isoformat()
    stale = list(session.scalars(select(Job).where(Job.state.in_(RUNNING), Job.heartbeat_at < cutoff)))
    for job in stale:
        if job.state == "cancel_requested":
            job.state = "cancelled"
        elif job.locked_by == "api" or job.attempts >= MAX_ATTEMPTS:
            job.state, job.error = "failed", "Interrumpido sin finalizar"
        else:
            job.state, job.locked_by, job.error = "queued", None, "Reanudado tras una interrupción"
            continue
        job.finished_at = now_iso()
    session.commit()
    return len(stale)


def schedule_refresh(session, every: timedelta, request: JobRequest) -> int:
    """Queue a sync for each watchlist wallet whose last finished sync is older than `every`."""
    cutoff = (_now() - every).isoformat()
    queued = 0
    for wallet in session.scalars(select(Wallet).where(Wallet.favorite.is_(True)).order_by(Wallet.address)):
        if active_job(session, wallet.address):
            continue
        last = session.scalar(select(Job.finished_at).where(Job.wallet == wallet.address, Job.state == "done")
                              .order_by(Job.finished_at.desc()).limit(1))
        if last is None or last < cutoff:
            enqueue(session, wallet.address, request, origin="schedule")
            queued += 1
    return queued


def worker_status(session) -> dict:
    alive_after = (_now() - WORKER_ALIVE).isoformat()
    workers = list(session.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc()).limit(5)))
    counts = {state: session.scalar(select(func.count()).select_from(Job).where(Job.state == state))
              for state in ("queued", "running")}
    return {"alive": any(w.heartbeat_at >= alive_after for w in workers), **counts,
            "workers": [{"id": w.id, "started_at": w.started_at, "heartbeat_at": w.heartbeat_at,
                         "alive": w.heartbeat_at >= alive_after, "info": w.info} for w in workers]}


class Worker:
    def __init__(self, sessions, rpc, prices, *, worker_id: str | None = None,
                 refresh_minutes: float | None = None, poll_seconds: float = 2.0, progress_interval: float = 1.0):
        self.sessions, self.rpc, self.prices = sessions, rpc, prices
        self.id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        if refresh_minutes is None:
            refresh_minutes = float(os.getenv("TRACKER_REFRESH_MINUTES", "30"))
        self.refresh = timedelta(minutes=refresh_minutes) if refresh_minutes > 0 else None
        self.poll_seconds, self.progress_interval = poll_seconds, progress_interval
        self.next_schedule = 0.0

    def beat(self, session):
        row = session.get(WorkerHeartbeat, self.id)
        if row is None:
            session.add(WorkerHeartbeat(id=self.id, info={"refresh_minutes": self.refresh.total_seconds() / 60
                                                          if self.refresh else 0}))
        else:
            row.heartbeat_at = now_iso()
        session.commit()

    def run_once(self) -> Job | None:
        """Housekeeping plus at most one job. Returns the job that ran, if any."""
        with self.sessions() as session:
            self.beat(session)
            recover_stale(session)
            if self.refresh and time.monotonic() >= self.next_schedule:
                schedule_refresh(session, self.refresh, JobRequest())
                self.next_schedule = time.monotonic() + 60
            job = claim(session, self.id)
            if job is None:
                return None
            self.run(session, job)
            return job

    def run(self, session, job: Job):
        wallet = session.get(Wallet, job.wallet)
        params = JobRequest(**job.params)
        progress = Progress(session, job, self.progress_interval, beat=lambda: self.beat(session))
        log.info("job %s: sync %s round %s", job.id, job.wallet, job.round)
        try:
            report = sync_and_price(session, wallet, self.rpc, self.prices, params.sync_request(), progress)
        except Exception as exc:  # unexpected bug: record it, never leave the job running
            session.rollback()
            log.exception("job %s failed", job.id)
            report = {"state": "partial", "issues": [], "error": f"Error interno: {type(exc).__name__}"}
        follow_up = finish(session, job, report, chain=True)
        log.info("job %s: %s%s", job.id, job.state, f", round {follow_up.round} queued" if follow_up else "")

    def run_forever(self, stop: threading.Event):
        log.info("worker %s started (refresh: %s)", self.id, self.refresh or "off")
        while not stop.is_set():
            try:
                ran = self.run_once()
            except Exception:
                log.exception("worker loop error")
                ran = None
            if ran is None:
                stop.wait(self.poll_seconds)
