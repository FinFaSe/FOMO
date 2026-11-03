"""Incremental ingestion with a SYNTHETIC RPC: no repeated getTransaction calls, resumable cursors."""

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracker.adapters.solana import ProviderError
from tracker.db import Observation
from tracker.main import create_app

from .conftest import WALLET, database_url
from .test_api import SIG, SyntheticPrices, SyntheticQuotes, SyntheticRPC, ledger_event

HOUR = 3600
SYNC = f"/api/wallets/{WALLET}/sync"


@pytest.fixture
def client(tmp_path):
    rpc = SyntheticRPC()
    app = create_app(database_url(tmp_path, "incremental.db"), rpc=rpc, prices=SyntheticPrices(),
                     quotes=SyntheticQuotes())
    with TestClient(app) as c:
        c.post("/api/wallets", json={"address": WALLET, "label": "SYNTHETIC TEST"})
        yield c, rpc
    app.state.engine.dispose()


def observations(c):
    with Session(c.app.state.engine) as s:
        return s.scalar(select(func.count()).select_from(Observation))


def recent(rpc, count, *, start=1, hours_ago=1):
    """Replace history with `count` synthetic transactions, newest first, inside the window."""
    rpc.history = []
    now = int(time.time())
    return [rpc.add(start + i, now - (hours_ago + i) * HOUR, newest=False) for i in range(count)]


def test_resync_makes_no_transaction_calls(client):
    c, rpc = client
    first = c.post(SYNC, json={}).json()
    assert first["rpc_transaction_fetches"] == 1 and first["coverage"]["history_start_reached"]
    second = c.post(SYNC, json={}).json()
    assert second["state"] == "scanned" and second["signature_window_complete"]
    assert second["rpc_transaction_fetches"] == 0 and second["transactions"] == 0
    assert rpc.fetches == [SIG]
    assert observations(c) == 1


def test_backfill_resumes_from_cursor_across_runs(client):
    c, rpc = client
    sigs = recent(rpc, 5)
    budget = {"max_pages": 1, "page_size": 2}
    r1 = c.post(SYNC, json=budget).json()
    assert r1["state"] == "partial" and "page_budget_reached" in r1["issues"]
    assert r1["transactions"] == 2 and r1["coverage"]["tail_signature"] == sigs[1]
    r2 = c.post(SYNC, json=budget).json()
    assert r2["transactions"] == 2 and r2["state"] == "partial"
    r3 = c.post(SYNC, json=budget).json()
    assert r3["transactions"] == 1 and r3["state"] == "scanned"
    assert r3["coverage"]["history_start_reached"]
    assert rpc.fetches == sigs  # each transaction downloaded exactly once, in order
    assert observations(c) == 5


def test_catch_up_fetches_only_new_signatures_and_resumes_a_gap(client):
    c, rpc = client
    c.post(SYNC, json={})
    now = int(time.time())
    older_new = rpc.add(1, now - 3 * HOUR)
    middle_new = rpc.add(2, now - 2 * HOUR)
    newest = rpc.add(3, now - HOUR)
    budget = {"max_pages": 1, "page_size": 2}
    r1 = c.post(SYNC, json=budget).json()
    assert r1["state"] == "partial" and r1["coverage"]["catchup_pending"]
    assert rpc.fetches == [SIG, newest, middle_new]
    late = rpc.add(4, now - 60)  # arrives while the gap is still pending
    r2 = c.post(SYNC, json=budget).json()
    assert r2["state"] == "scanned" and not r2["coverage"]["catchup_pending"]
    assert r2["coverage"]["head_signature"] == newest
    assert rpc.fetches[-1] == older_new and late not in rpc.fetches
    r3 = c.post(SYNC, json=budget).json()
    assert r3["transactions"] == 1 and rpc.fetches[-1] == late
    assert len(rpc.fetches) == len(set(rpc.fetches)) == 5
    assert observations(c) == 5


def test_window_boundary_and_wider_window_resume(client):
    c, rpc = client
    rpc.history = []
    now = int(time.time())
    day = 24 * HOUR
    a, b = rpc.add(1, now - day, newest=False), rpc.add(2, now - 10 * day, newest=False)
    rpc.add(3, now - 20 * day, newest=False)
    narrow = c.post(SYNC, json={"days": 5}).json()
    assert narrow["state"] == "scanned" and rpc.fetches == [a]
    assert not narrow["coverage"]["history_start_reached"]
    assert c.post(SYNC, json={"days": 5}).json()["pages"] == 1  # only the catch-up listing
    wider = c.post(SYNC, json={"days": 15}).json()
    assert wider["state"] == "scanned" and rpc.fetches == [a, b]


def test_sync_reuses_payload_stored_by_verification(client):
    c, rpc = client
    c.post(f"/api/wallets/{WALLET}/ledger", json={"events": [ledger_event()]})
    c.post(f"/api/wallets/{WALLET}/verify")
    r = c.post(SYNC, json={}).json()
    assert r["transactions"] == 1 and r["reused_stored_payloads"] == 1
    assert r["rpc_transaction_fetches"] == 0 and rpc.fetches == [SIG]


def test_interrupted_scan_keeps_committed_progress(client):
    c, rpc = client
    sigs = recent(rpc, 3)
    original = rpc.transaction

    def flaky(signature):
        if signature == sigs[1]:
            raise ProviderError("Synthetic RPC unavailable", 503)
        return original(signature)

    rpc.transaction = flaky
    r1 = c.post(SYNC, json={}).json()
    assert r1["state"] == "partial" and r1["coverage"]["tail_signature"] == sigs[0]
    assert observations(c) == 1
    rpc.transaction = original
    r2 = c.post(SYNC, json={}).json()
    assert r2["state"] == "scanned" and r2["transactions"] == 2
    assert rpc.fetches == sigs


def test_revalidation_does_not_move_cursors(client):
    c, rpc = client
    sigs = recent(rpc, 3)
    c.post(SYNC, json={"max_pages": 1, "page_size": 2})
    r = c.post(SYNC, json={"revalidate": True}).json()
    assert r["mode"] == "revalidate" and r["revalidated"] == 2 and r["transactions"] == 1
    assert r["coverage"]["tail_signature"] == sigs[1]
    # The incremental scan still resumes from its own cursor; the third payload is not re-downloaded.
    after = c.post(SYNC, json={}).json()
    assert after["state"] == "scanned" and after["rpc_transaction_fetches"] == 0


def test_a_provider_without_full_history_never_completes_the_window(client):
    """A non-archival RPC lists only recent days: the end of its listing is its horizon, not the wallet's start."""
    c, rpc = client
    recent(rpc, 2)
    rpc.full_history = lambda: False
    report = c.post(SYNC, json={}).json()
    assert report["transactions"] == 2 and report["state"] == "partial"
    assert "rpc_history_horizon" in report["issues"] and not report["coverage"]["history_start_reached"]
    assert not c.get(f"/api/wallets/{WALLET}/analysis").json()["history_complete"]

    def unavailable():
        raise ProviderError("Synthetic: method not found", 502)

    rpc.full_history = unavailable
    assert "rpc_history_unverified" in c.post(SYNC, json={}).json()["issues"]
    rpc.full_history = lambda: True  # an archival provider confirms the start
    final = c.post(SYNC, json={}).json()
    assert final["state"] == "scanned" and final["coverage"]["history_start_reached"]
