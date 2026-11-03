"""Background job queue with a SYNTHETIC RPC and SYNTHETIC prices."""

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from tracker import jobs as jobs_module
from tracker.db import Job
from tracker.jobs import Worker
from tracker.main import create_app

from .conftest import WALLET, b58, database_url
from .test_api import SyntheticPrices, SyntheticQuotes, SyntheticRPC
from .test_incremental_sync import client, observations, recent  # noqa: F401  (client is a fixture)

QUEUE = f"/api/wallets/{WALLET}/jobs"


def worker(c, rpc, **kwargs):
    options = {"worker_id": "test-worker", "refresh_minutes": 0, "progress_interval": 0, "lookup_funding": False,
               **kwargs}
    return Worker(c.app.state.sessions, rpc, c.app.state.prices, **options)


def drain(w, limit=20):
    ran = []
    for _ in range(limit):
        job = w.run_once()
        if job is None:
            return ran
        ran.append(job.id)
    raise AssertionError("queue did not drain")


def all_jobs(c):
    return c.get("/api/jobs?limit=100").json()["items"][::-1]  # oldest first


def set_job(c, job_id, **values):
    with Session(c.app.state.engine) as s:
        s.execute(update(Job).where(Job.id == job_id).values(**values))
        s.commit()


def test_queued_sync_runs_in_the_worker_and_is_not_duplicated(client):  # noqa: F811
    c, rpc = client
    first = c.post(QUEUE, json={})
    assert first.status_code == 202 and first.json()["created"]
    again = c.post(QUEUE, json={}).json()
    assert not again["created"] and again["job"]["id"] == first.json()["job"]["id"]
    assert c.get("/api/worker").json()["queued"] == 1
    w = worker(c, rpc)
    assert drain(w) == [first.json()["job"]["id"]]
    job = c.get(f"/api/jobs/{first.json()['job']['id']}").json()
    assert job["state"] == "done" and job["result"]["state"] == "scanned" and job["progress"]["transactions"] == 1
    assert observations(c) == 1
    status = c.get("/api/worker").json()
    assert status["alive"] and status["workers"][0]["id"] == "test-worker" and status["queued"] == 0


def test_long_history_completes_over_chained_rounds(client):  # noqa: F811
    c, rpc = client
    sigs = recent(rpc, 5)
    c.post(QUEUE, json={"max_pages": 1, "page_size": 2})
    drain(worker(c, rpc))
    rounds = all_jobs(c)
    assert [j["round"] for j in rounds] == [1, 2, 3]
    assert [j["origin"] for j in rounds] == ["manual", "chain", "chain"]
    assert rounds[1]["parent_id"] == rounds[0]["id"] and rounds[2]["parent_id"] == rounds[1]["id"]
    assert all(j["state"] == "done" for j in rounds) and rounds[-1]["result"]["state"] == "scanned"
    assert rpc.fetches == sigs and observations(c) == 5


def test_max_rounds_bounds_the_chain(client):  # noqa: F811
    c, rpc = client
    recent(rpc, 5)
    c.post(QUEUE, json={"max_pages": 1, "page_size": 2, "max_rounds": 2})
    drain(worker(c, rpc))
    assert [j["round"] for j in all_jobs(c)] == [1, 2] and observations(c) == 4


def test_provider_errors_back_off_then_fail(client, monkeypatch):  # noqa: F811
    c, rpc = client
    monkeypatch.setattr(jobs_module, "MAX_ATTEMPTS", 2)
    rpc.transaction = lambda signature: (_ for _ in ()).throw(jobs_module.ProviderError("Synthetic RPC down", 503))
    job_id = c.post(QUEUE, json={}).json()["job"]["id"]
    w = worker(c, rpc)
    assert w.run_once().id == job_id
    job = c.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "queued" and job["attempts"] == 1 and job["error"] == "Synthetic RPC down"
    assert job["run_after"] > datetime.now(timezone.utc).isoformat()  # backoff: not due yet
    assert w.run_once() is None
    set_job(c, job_id, run_after=datetime.now(timezone.utc).isoformat())
    w.run_once()
    job = c.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "failed" and job["attempts"] == 2


def test_cancel_queued_and_running_jobs(client):  # noqa: F811
    c, rpc = client
    job_id = c.post(QUEUE, json={}).json()["job"]["id"]
    assert c.post(f"/api/jobs/{job_id}/cancel").json()["state"] == "cancelled"
    assert c.post(f"/api/jobs/{job_id}/cancel").status_code == 409
    w = worker(c, rpc)
    assert w.run_once() is None

    sigs = recent(rpc, 3)
    job_id = c.post(QUEUE, json={}).json()["job"]["id"]
    original = rpc.transaction

    def cancel_after_first(signature):
        if signature == sigs[0]:
            assert c.post(f"/api/jobs/{job_id}/cancel").json()["state"] == "cancel_requested"
        return original(signature)

    rpc.transaction = cancel_after_first
    w.run_once()
    job = c.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "cancelled" and observations(c) == 1
    assert not [j for j in all_jobs(c) if j["parent_id"] == job_id]  # no follow-up after a cancel
    # A new job resumes from the stored cursor.
    rpc.transaction = original
    c.post(QUEUE, json={})
    drain(w)
    assert observations(c) == 3 and rpc.fetches == sigs


def test_interrupted_job_is_recovered_and_resumed(client):  # noqa: F811
    c, rpc = client
    job_id = c.post(QUEUE, json={}).json()["job"]["id"]
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    set_job(c, job_id, state="running", locked_by="crashed-worker", heartbeat_at=old, attempts=1)
    assert c.post(f"/api/wallets/{WALLET}/sync", json={}).status_code == 409  # still looks busy
    w = worker(c, rpc)
    assert w.run_once().id == job_id
    job = c.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "done" and job["attempts"] == 2


def test_watchlist_wallets_are_refreshed_on_schedule(client):  # noqa: F811
    c, rpc = client
    other = b58(4242)
    c.post("/api/wallets", json={"address": other})
    c.patch(f"/api/wallets/{WALLET}", json={"favorite": True})
    w = worker(c, rpc, refresh_minutes=30)
    w.run_once()
    scheduled = all_jobs(c)
    assert [(j["wallet"], j["origin"], j["state"]) for j in scheduled] == [(WALLET, "schedule", "done")]
    w.next_schedule = 0
    assert w.run_once() is None  # refreshed recently: nothing new
    set_job(c, scheduled[0]["id"], finished_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    w.next_schedule = 0
    assert w.run_once() is not None and len(all_jobs(c)) == 2


def test_inline_sync_is_recorded_and_wallet_rewrites_wait_for_running_jobs(client):  # noqa: F811
    c, rpc = client
    report = c.post(f"/api/wallets/{WALLET}/sync", json={}).json()
    [job] = all_jobs(c)
    assert job["id"] == report["job_id"] and job["origin"] == "api" and job["state"] == "done"
    job_id = c.post(QUEUE, json={}).json()["job"]["id"]
    set_job(c, job_id, state="running", heartbeat_at=datetime.now(timezone.utc).isoformat())
    for method, path in (("post", "sync"), ("post", "normalize"), ("post", "price"), ("post", "verify")):
        r = getattr(c, method)(f"/api/wallets/{WALLET}/{path}", json={})
        assert r.status_code == 409, path


def test_claim_skips_wallets_that_already_run_a_job(client):  # noqa: F811
    c, rpc = client
    with Session(c.app.state.engine) as s:
        s.add_all([Job(wallet=WALLET, state="running", params={}, heartbeat_at=datetime.now(timezone.utc).isoformat()),
                   Job(wallet=WALLET, state="queued", params={})])
        s.commit()
    with c.app.state.sessions() as s:
        assert jobs_module.claim(s, "w") is None
        assert s.scalar(select(Job.state).where(Job.state == "queued")) == "queued"


def test_embedded_worker_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKER_EMBEDDED_WORKER", "1")
    monkeypatch.setenv("TRACKER_REFRESH_MINUTES", "0")
    app = create_app(database_url(tmp_path, "embedded.db"), rpc=SyntheticRPC(), prices=SyntheticPrices(),
                     quotes=SyntheticQuotes())
    with TestClient(app) as c:
        c.post("/api/wallets", json={"address": WALLET})
        job_id = c.post(QUEUE, json={}).json()["job"]["id"]
        deadline = time.monotonic() + 15
        while c.get(f"/api/jobs/{job_id}").json()["state"] != "done":
            assert time.monotonic() < deadline, "embedded worker did not run the job"
            time.sleep(0.2)
    app.state.engine.dispose()


@pytest.mark.parametrize("payload", [{"max_rounds": 0}, {"page_size": 101}, {"unknown": 1}])
def test_job_request_validation(client, payload):  # noqa: F811
    c, _ = client
    assert c.post(QUEUE, json=payload).status_code == 422


def test_rounds_that_store_progress_are_not_failed_attempts(client, monkeypatch):  # noqa: F811
    """A long history on a rate-limited RPC: every round stores some transactions and then fails."""
    c, rpc = client
    monkeypatch.setattr(jobs_module, "MAX_ATTEMPTS", 2)
    sigs = recent(rpc, 6)
    original, served = rpc.transaction, []

    def limited(signature):
        if len(served) % 2 == 1 and signature not in served:
            served.append(None)  # every other new download is refused
            raise jobs_module.ProviderError("Synthetic rate limit", 503)
        served.append(signature)
        return original(signature)

    rpc.transaction = limited
    job_id = c.post(QUEUE, json={}).json()["job"]["id"]
    w = worker(c, rpc)
    for _ in range(12):
        set_job(c, job_id, run_after=datetime.now(timezone.utc).isoformat())
        w.run_once()
        if c.get(f"/api/jobs/{job_id}").json()["state"] != "queued":
            break
    job = c.get(f"/api/jobs/{job_id}").json()
    assert job["state"] == "done" and observations(c) == 6  # far more rounds than MAX_ATTEMPTS, never failed
    assert sorted(s for s in served if s) == sorted(sigs)
