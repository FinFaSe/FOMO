"""Real RPC smoke test. No synthetic trade, price or performance data is generated."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from tracker.adapters.solana import SolanaRPC, digest, observe  # noqa: E402
from tracker.main import create_app  # noqa: E402
from tracker.service import store_transaction  # noqa: E402


def main():
    rpc = SolanaRPC()
    # Public transaction observed during source audit; signer is a research candidate, not a proven FOMO user.
    signature = "k5QYVE2czvAA5bP8wjPF5rdgxEsCzzN4XBQaLJW5VkGTxsbv5yedb5hpodSRBjmRfhPDvAGJeiNM9mYrcCyX2oh"
    tx = rpc.transaction(signature)
    signers = observe(tx, "So11111111111111111111111111111111111111112")["signers"]
    if not signers:
        raise RuntimeError("No signer in observed transaction")
    wallet = signers[0]
    with tempfile.TemporaryDirectory(prefix="fomo-live-smoke-") as temp:
        app = create_app(f"sqlite:///{temp}/live.db", rpc=rpc)
        with Session(app.state.engine) as session:
            original = store_transaction(session, rpc, signature)
            session.commit()
            repeated = store_transaction(session, rpc, signature)
            assert repeated.digest == original.digest
            session.commit()
        with TestClient(app) as client:
            assert client.post("/api/wallets", json={"address": wallet, "label": "Public RPC candidate"}).status_code == 201
            request = {"max_pages": 1, "page_size": 1, "days": 90}
            first = client.post(f"/api/wallets/{wallet}/sync", json=request).json()
            if first["transactions"] == 0:
                raise RuntimeError(f"No transactions ingested: {first['issues']}")
            observations = client.get(f"/api/wallets/{wallet}/observations").json()
            analysis = client.get(f"/api/wallets/{wallet}/analysis").json()
            assert observations["total"] >= 1
            assert not analysis["eligible"]
            assert analysis["score"]["verified_smart_money"] is None
            assert analysis["metrics"]["net_realized_pnl_usd"] is None
            output = {"real_data": True, "audited_signature": signature, "audited_slot": tx["slot"],
                      "audited_payload_sha256": digest(tx), "wallet": wallet,
                      "ingestion": first, "observations": observations["total"],
                      "snapshot_id": analysis["snapshot_id"], "verified_performance": False}
            output["deduplication_verified"] = True
            print(json.dumps(output, indent=2))
        app.state.engine.dispose()


if __name__ == "__main__":
    main()
