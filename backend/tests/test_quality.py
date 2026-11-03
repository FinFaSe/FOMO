"""Data quality report over tracked wallets. SYNTHETIC data."""

import time

from .conftest import WALLET, b58
from .test_chain_trades import serve
from .test_incremental_sync import client  # noqa: F401  (fixture)
from .test_normalize import POOL, SOL, balance, pump_buy, pump_sell, tx

SYNC = f"/api/wallets/{WALLET}/sync"
UNKNOWN_PROGRAM = b58(9903)


def test_quality_counts_evidence_valuation_unknown_programs_and_analysis(client):  # noqa: F811
    c, rpc = client
    now = int(time.time())
    other = b58(9901)
    # A swap through a program the normalizer does not recognize: inferred, and the program is reported.
    ata = b58(9902)
    unknown = tx([WALLET, ata, POOL], [10 * SOL, 0, 0], [9 * SOL - 5000, 0, SOL],
                 pre_tokens=[balance(1, other, WALLET, 0)], post_tokens=[balance(1, other, WALLET, 1000)],
                 programs=[UNKNOWN_PROGRAM], block_time=now - 1800)
    serve(rpc, [pump_buy(block_time=now - 7200), pump_sell(block_time=now - 3600), unknown])
    c.post(SYNC, json={})
    q = c.get("/api/quality").json()
    assert q["wallets"] == 1 and q["sync"] == {"complete": 1}
    assert q["transactions"]["observed"] == 3 and q["transactions"]["with_token_movement"] == 3
    assert q["trades"]["by_evidence"] == {"verified": 2, "inferred": 1}
    assert q["trades"]["issues"] == {"no_recognized_venue": 1}
    assert q["unrecognized_programs"]["programs"] == {UNKNOWN_PROGRAM: 1}
    assert q["valuation"]["buy_sell_usd"] == {"verified": 3} and q["valuation"]["buy_market_cap"]["verified"] == 1
    assert q["analysis"]["eligible"] == 0 and "insufficient_evaluable_positions" in q["analysis"]["exclusion_reasons"]
    assert c.get("/api/quality", params={"wallet": WALLET}).json()["wallets"] == 1
    assert c.get("/api/quality", params={"wallet": POOL}).status_code == 404
