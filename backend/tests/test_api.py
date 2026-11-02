"""API integration tests use a synthetic RPC and an isolated temporary database."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tracker.adapters.fomo import FomoAPI
from tracker.adapters.solana import ProviderError, SolanaRPC, observe
from tracker.db import Observation, RawTransaction
from tracker.main import create_app
from tracker.schemas import LedgerEvent

from .conftest import AS_OF, TOKEN, WALLET, b58

SIG = b58(42, 64)


def transaction():
    return {"slot": 1234, "blockTime": int(AS_OF.timestamp()), "version": 1,
            "transaction": {"signatures": [SIG], "message": {"accountKeys": [
                {"pubkey": WALLET, "signer": True, "writable": True}]}},
            "meta": {"err": None, "fee": 5000, "preBalances": [1000000000],
                     "postBalances": [999995000], "preTokenBalances": [],
                     "postTokenBalances": [{"mint": TOKEN, "owner": WALLET,
                                             "uiTokenAmount": {"amount": "1000000", "decimals": 6}}]}}


class SyntheticRPC:
    source = "https://synthetic.example.org"

    def __init__(self):
        self.payload = transaction()

    def signatures(self, wallet, before=None, limit=10):
        return [{"signature": SIG, "blockTime": int(AS_OF.timestamp()), "err": None}] if not before else []

    def transaction(self, signature):
        assert signature == SIG
        return copy.deepcopy(self.payload)


@pytest.fixture
def client(tmp_path):
    rpc = SyntheticRPC()
    app = create_app(f"sqlite:///{tmp_path}/test.db", rpc=rpc)
    with TestClient(app) as c:
        c.post("/api/wallets", json={"address": WALLET, "label": "SYNTHETIC TEST"})
        yield c, rpc
    app.state.engine.dispose()


def ledger_event(**changes):
    return {"signature": SIG, "token": TOKEN, "timestamp": AS_OF.isoformat(),
            "kind": "buy", "quantity": "1", "gross_usd": "100", "fees_usd": "1",
            "market_cap_usd": "100000", "source_url": "https://example.org/synthetic", **changes}


def test_defaults_wallet_validation_and_pagination(client):
    c, _ = client
    assert c.get("/api/health").json()["status"] == "ok"
    defaults = c.get("/api/config").json()["default_filters"]
    assert defaults == {"max_entry_market_cap_usd": "250000", "min_realized_roi_pct": "500",
                        "min_win_rate": 0.6, "min_evaluable_positions": 30,
                        "analysis_days": 90, "conservative_win_rate": True}
    assert c.post("/api/wallets", json={"address": "fake"}).status_code == 422
    assert c.post("/api/wallets", json={"address": WALLET}).status_code == 201
    assert c.get("/api/wallets").json()["total"] == 1
    assert c.get("/api/wallets?offset=1").json()["items"] == []
    assert c.get(f"/api/wallets/{b58(999)}/analysis").status_code == 404
    assert c.patch(f"/api/wallets/{WALLET}", json={"favorite": True}).json()["favorite"]


def test_realistic_rpc_capture_deduplication_and_failed_transactions(client):
    c, rpc = client
    url = f"/api/wallets/{WALLET}/sync"
    assert c.post(url, json={}).json()["signature_window_complete"]
    c.post(url, json={})
    ob = c.get(f"/api/wallets/{WALLET}/observations").json()
    assert ob["total"] == 1
    assert ob["items"][0]["classification"] == "unclassified_movement"
    assert ob["items"][0]["token_deltas"][TOKEN] == "1"
    with Session(c.app.state.engine) as s:
        assert len(list(s.scalars(select(RawTransaction)))) == 1
        assert len(list(s.scalars(select(Observation)))) == 1
    assert not c.get(f"/api/wallets/{WALLET}/analysis").json()["eligible"]
    assert observe({**rpc.payload, "meta": {**rpc.payload["meta"], "err": {"x": 1}}}, WALLET)["status"] == "failed"


def test_finalized_conflict_never_overwrites_original(client):
    c, rpc = client
    c.post(f"/api/wallets/{WALLET}/sync", json={})
    rpc.payload["slot"] = 9999
    r = c.post(f"/api/wallets/{WALLET}/sync", json={}).json()
    assert r["state"] == "partial"
    assert "Conflicto" in r["issues"][0]
    with Session(c.app.state.engine) as s:
        assert s.get(RawTransaction, SIG).slot == 1234


def test_import_is_atomic_idempotent_and_untrusted(client):
    c, _ = client
    url = f"/api/wallets/{WALLET}/ledger"
    event = ledger_event()
    assert c.post(url, json={"events": [event, event]}).json()["inserted"] == 1
    assert c.post(url, json={"events": [event]}).json()["duplicates"] == 1
    assert c.post(url, json={"events": [ledger_event(gross_usd="101")]}).status_code == 409
    r = c.get(f"/api/wallets/{WALLET}/analysis", params={"as_of": AS_OF.isoformat()}).json()
    assert not r["eligible"]
    snapshot = r["snapshot_id"]
    assert c.get(f"/api/snapshots/{snapshot}").json() == r
    forged = {**event, "evidence": {"usd": "verified"}}
    assert c.post(url, json={"events": [forged]}).status_code == 422
    # A batch with a new event followed by a conflict must roll back the whole batch.
    assert c.post(url, json={"events": [ledger_event(signature=b58(50, 64)),
                                        ledger_event(gross_usd="101")]}).status_code == 409
    assert len(c.get(f"/api/wallets/{WALLET}/analysis").json()["positions"][0]["fills"]) == 1


def test_rpc_match_does_not_verify_usd_swap_or_market_cap(client):
    c, _ = client
    c.post(f"/api/wallets/{WALLET}/ledger", json={"events": [ledger_event()]})
    verify = c.post(f"/api/wallets/{WALLET}/verify").json()
    assert verify["matched_movements"] == 1
    r = c.get(f"/api/wallets/{WALLET}/analysis").json()
    f = r["positions"][0]["fills"][0]
    assert f["evidence"]["movement"] == "matched"
    assert f["evidence"]["trade"] == "unverified"
    assert f["evidence"]["usd"] == "reported"
    assert r["score"]["verified_smart_money"] is None
    assert c.get("/api/rankings").json()["total"] == 0


def test_verification_revokes_a_stale_movement_match_on_finalized_conflict(client):
    c, rpc = client
    c.post(f"/api/wallets/{WALLET}/ledger", json={"events": [ledger_event()]})
    c.post(f"/api/wallets/{WALLET}/verify")
    rpc.payload["slot"] = 7777
    r = c.post(f"/api/wallets/{WALLET}/verify").json()
    assert r["errors"]
    evidence = c.get(f"/api/wallets/{WALLET}/analysis").json()["positions"][0]["fills"][0]["evidence"]
    assert evidence["movement"] == "unavailable"
    assert c.get(f"/api/evidence/{SIG}").json()["payload"]["slot"] == 1234


def test_chain_mismatch_and_query_validation(client):
    c, _ = client
    c.post(f"/api/wallets/{WALLET}/ledger", json={"events": [ledger_event(quantity="999")]})
    assert c.post(f"/api/wallets/{WALLET}/verify").json()["matched_movements"] == 0
    r = c.get(f"/api/wallets/{WALLET}/analysis").json()
    assert "chain_evidence_mismatch" in r["positions"][0]["issues"]
    assert c.get(f"/api/wallets/{WALLET}/analysis?analysis_days=0").status_code == 422
    assert c.get(f"/api/wallets/{WALLET}/analysis?as_of=2026-01-01T00:00:00").status_code == 422
    r = c.get(f"/api/wallets/{WALLET}/analysis?min_win_rate=0.7&analysis_days=7").json()
    assert r["filters"]["min_win_rate"] == 0.7
    assert r["filters"]["analysis_days"] == 7


def test_bounded_discovery_does_not_label_signers_smart_money(client):
    c, _ = client
    result = c.post("/api/discovery", json={"token": TOKEN, "limit": 1}).json()
    assert result["classification"] == "unverified_candidates"
    assert result["candidates"] == [WALLET]
    assert c.get("/api/rankings").json()["items"] == []


def test_concurrent_requests_share_an_immutable_snapshot(client):
    c, _ = client
    url = f"/api/wallets/{WALLET}/analysis?as_of={AS_OF.isoformat().replace('+', '%2B')}"
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: c.get(url), range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()["snapshot_id"] for r in responses}) == 1


def test_provider_failures_preserve_partial_state(client):
    c, rpc = client

    def unavailable(*args, **kwargs):
        raise ProviderError("Synthetic RPC unavailable", 503)

    rpc.transaction = unavailable
    r = c.post(f"/api/wallets/{WALLET}/sync", json={}).json()
    assert r["state"] == "partial"
    assert not r["signature_window_complete"]


def test_fomo_key_stays_server_side_and_upstream_report_is_not_proof(tmp_path, monkeypatch):
    monkeypatch.setenv("FOMO_API_KEY", "SYNTHETIC-SECRET-NOT-A-REAL-KEY")

    def transport(request):
        assert request.url.path == "/v2/users/test_user"
        assert request.headers["authorization"] == "Bearer SYNTHETIC-SECRET-NOT-A-REAL-KEY"
        return httpx.Response(200, json={"wallets": {"solana": WALLET}, "pnlUsd": 999999})

    fomo = FomoAPI(httpx.Client(transport=httpx.MockTransport(transport)))
    app = create_app(f"sqlite:///{tmp_path}/fomo.db", rpc=SyntheticRPC(), fomo=fomo)
    with TestClient(app) as c:
        w = c.post("/api/integrations/fomo/resolve", json={"handle": "@test_user"}).json()
        assert w["identity"]["verification"] == "provider_reported_not_independently_verified"
        assert "SECRET" not in c.get("/api/config").text
        assert c.get(f"/api/wallets/{WALLET}/analysis").json()["metrics"]["net_realized_pnl_usd"] is None


def test_missing_fomo_key_and_access_token(client, monkeypatch):
    c, _ = client
    c.app  # constructed without configured credentials
    monkeypatch.setenv("TRACKER_ACCESS_TOKEN", "synthetic-access-token")
    assert c.get("/api/wallets").status_code == 401
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/wallets", headers={"X-Tracker-Token": "synthetic-access-token"}).status_code == 200


@pytest.mark.parametrize("changes", [{"fees_usd": "-1"}, {"gross_usd": "NaN"},
                                     {"timestamp": "2026-10-01T00:00:00"},
                                     {"source_url": "https://example.org/?api_key=secret"},
                                     {"source_url": "javascript:alert(1)"}, {"quantity": "0"}])
def test_invalid_ledger_facts_rejected(changes):
    with pytest.raises(ValueError):
        LedgerEvent(**ledger_event(**changes))


def test_rpc_documented_parameters_and_redacted_error():
    def transport(request):
        body = json.loads(request.content)
        assert body["params"][1]["commitment"] == "finalized"
        assert body["params"][1]["maxSupportedTransactionVersion"] == 1
        return httpx.Response(200, json={"result": transaction()})

    rpc = SolanaRPC(client=httpx.Client(transport=httpx.MockTransport(transport)))
    assert rpc.transaction(SIG)["version"] == 1

    def deny(request):
        return httpx.Response(403, text="SYNTHETIC-SECRET")

    rpc = SolanaRPC("https://example.org/private-key-path?key=secret",
                    httpx.Client(transport=httpx.MockTransport(deny)))
    assert rpc.source == "https://example.org"
    with pytest.raises(ProviderError) as exc:
        rpc.transaction(SIG)
    assert "SECRET" not in str(exc.value)
