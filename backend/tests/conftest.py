"""All fixtures in this directory are SYNTHETIC; none describe real traders."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tracker.analytics import Fill

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
AS_OF = datetime(2026, 10, 31, 16, tzinfo=timezone.utc)


def b58(value: int, size: int = 32) -> str:
    raw = value.to_bytes(size, "big")
    n = int.from_bytes(raw, "big")
    result = ""
    while n:
        n, r = divmod(n, 58)
        result = ALPHABET[r] + result
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + result


WALLET = b58(111)
TOKEN = b58(222)


@pytest.fixture
def fill():
    counter = [0]

    def create(kind, quantity, gross, *, token=TOKEN, fees="0", days_ago=2, cap="100000", **kwargs):
        counter[0] += 1
        return Fill(signature=b58(counter[0], 64), event_index=0, token=token,
                    timestamp=AS_OF - timedelta(days=days_ago, seconds=-counter[0]),
                    kind=kind, quantity=Decimal(str(quantity)),
                    gross_usd=Decimal(str(gross)) if gross is not None else None,
                    fees_usd=Decimal(str(fees)) if fees is not None else None,
                    market_cap_usd=Decimal(cap) if cap is not None else None,
                    source_url="https://example.org/synthetic-test-only",
                    evidence=kwargs.pop("evidence", {"trade": "verified", "usd": "verified",
                                                      "market_cap": "verified"}), **kwargs)
    return create
