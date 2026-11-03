"""Concurrent transaction downloads: same results as one by one, only the waiting overlaps. SYNTHETIC data."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from tracker.adapters.solana import ProviderError, SolanaRPC
from tracker.main import create_app

from .conftest import WALLET, database_url
from .test_api import SyntheticPrices, SyntheticQuotes, SyntheticRPC
from .test_incremental_sync import SYNC, observations, recent


class ConcurrentRPC(SyntheticRPC):
    """SyntheticRPC with the adapter's concurrent batch download (threads, errors returned as values)."""

    def __init__(self):
        super().__init__()
        self.batches, self.lock = [], threading.Lock()

    def transaction(self, signature):
        with self.lock:  # `fetches` is shared by the worker threads
            return super().transaction(signature)

    def transactions(self, signatures):
        self.batches.append(list(signatures))

        def one(signature):
            try:
                return signature, self.transaction(signature)
            except ProviderError as exc:
                return signature, exc

        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(pool.map(one, signatures))


@pytest.fixture
def client(tmp_path):
    rpc = ConcurrentRPC()
    app = create_app(database_url(tmp_path, "prefetch.db"), rpc=rpc, prices=SyntheticPrices(), quotes=SyntheticQuotes())
    with TestClient(app) as c:
        c.post("/api/wallets", json={"address": WALLET})
        yield c, rpc
    app.state.engine.dispose()


def test_backfill_downloads_each_page_ahead_with_the_same_result(client):
    c, rpc = client
    sigs = recent(rpc, 5)
    budget = {"max_pages": 1, "page_size": 2}
    reports = [c.post(SYNC, json=budget).json() for _ in range(3)]
    assert [r["transactions"] for r in reports] == [2, 2, 1]
    assert [r["state"] for r in reports] == ["partial", "partial", "scanned"]
    assert reports[0]["coverage"]["tail_signature"] == sigs[1]
    assert rpc.batches == [sigs[0:2], sigs[2:4]]  # the last page has a single transaction: fetched directly
    assert sorted(rpc.fetches) == sorted(sigs) and observations(c) == 5


def test_a_failure_mid_page_stops_at_the_same_cursor(client):
    c, rpc = client
    sigs = recent(rpc, 3)
    original = rpc.transaction

    def flaky(signature):
        if signature == sigs[1]:
            raise ProviderError("Synthetic RPC unavailable", 503)
        return original(signature)

    rpc.transaction = flaky
    first = c.post(SYNC, json={}).json()
    assert first["state"] == "partial" and first["coverage"]["tail_signature"] == sigs[0]
    assert observations(c) == 1  # the third payload was downloaded ahead but is not recorded past the failure
    rpc.transaction = original
    second = c.post(SYNC, json={}).json()
    assert second["state"] == "scanned" and second["transactions"] == 2 and observations(c) == 3


def test_stored_and_observed_transactions_are_not_downloaded_again(client):
    c, rpc = client
    c.post(SYNC, json={})
    now = int(time.time())
    new = [rpc.add(i, now - (4 - i) * 3600) for i in (1, 2, 3)]
    report = c.post(SYNC, json={}).json()
    assert report["transactions"] == 3 and rpc.batches == [list(reversed(new))]  # newest first, no stored ones
    revalidate = c.post(SYNC, json={"revalidate": True}).json()
    assert revalidate["revalidated"] == 4 and len(rpc.batches[-1]) == 4  # refresh downloads everything again


def test_adapter_overlaps_slow_responses_but_spaces_request_starts():
    starts, lock = [], threading.Lock()

    def transport(request):
        signature = json.loads(request.content)["params"][0]
        with lock:
            starts.append(time.monotonic())
        time.sleep(0.2)
        if signature == "missing":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "result": {"slot": 1, "transaction": {"signatures": [signature]}}})

    rpc = SolanaRPC("https://rpc.example.org", httpx.Client(transport=httpx.MockTransport(transport)),
                    min_interval=0.05, concurrency=4)
    signatures = [f"sig{i}" for i in range(7)] + ["missing"]
    started = time.monotonic()
    got = rpc.transactions(signatures)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0  # one by one: 8 × 0.2 s = 1.6 s
    assert all(got[s]["transaction"]["signatures"] == [s] for s in signatures[:-1])
    assert isinstance(got["missing"], ProviderError) and got["missing"].status == 409
    ordered = sorted(starts)
    assert min(b - a for a, b in zip(ordered, ordered[1:])) >= 0.045  # starts stay spaced across threads


def limited_rpc(answers):
    """SolanaRPC over a transport that replays `answers` (status, json) and then succeeds."""
    calls = []

    def transport(request):
        calls.append(time.monotonic())
        status, body = answers.pop(0) if answers else (200, {"jsonrpc": "2.0", "id": 1, "result": "ok"})
        return httpx.Response(status, json=body)

    rpc = SolanaRPC("https://rpc.example.org", httpx.Client(transport=httpx.MockTransport(transport)),
                    min_interval=0.01, concurrency=2)
    return rpc, calls


def test_rate_limits_slow_every_request_down_and_successes_relax_it():
    rpc, calls = limited_rpc([(429, {}), (200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005}})])
    assert rpc.call("getSlot", []) == "ok" and len(calls) == 3
    assert calls[1] - calls[0] >= 0.09 and calls[2] - calls[1] >= 0.19  # doubled spacing after each limit
    slowed = rpc.interval
    for _ in range(20):
        rpc.call("getSlot", [])
    assert rpc.min_interval <= rpc.interval < slowed


def test_persistent_rate_limiting_is_a_provider_error():
    rpc, calls = limited_rpc([(429, {})] * 10)
    rpc.max_interval = 0.05
    with pytest.raises(ProviderError) as error:
        rpc.call("getSlot", [])
    assert error.value.status == 503 and len(calls) == rpc.attempts


def test_full_history_is_read_from_the_first_available_block_and_cached():
    rpc, calls = limited_rpc([(200, {"jsonrpc": "2.0", "id": 1, "result": 464336369})])
    assert rpc.full_history() is False and rpc.full_history() is False and len(calls) == 1
    archival, _ = limited_rpc([(200, {"jsonrpc": "2.0", "id": 1, "result": 0})])
    assert archival.full_history() is True


def test_a_download_that_failed_ahead_gets_a_fresh_attempt_in_turn(client):
    c, rpc = client
    sigs = recent(rpc, 3)
    original, failed = rpc.transaction, []

    def once(signature):
        if signature == sigs[0] and not failed:  # rate limited while the page was downloaded ahead
            failed.append(signature)
            raise ProviderError("Synthetic rate limit", 503)
        return original(signature)

    rpc.transaction = once
    report = c.post(SYNC, json={}).json()
    assert report["state"] == "scanned" and report["transactions"] == 3 and failed == [sigs[0]]
