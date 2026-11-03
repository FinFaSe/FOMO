"""All fixtures in this directory are SYNTHETIC; none describe real traders."""

import os
import tempfile

# `tracker.main` builds its module-level app on import, which opens (and migrates) DATABASE_URL. Tests must
# never touch a developer's real database, so point it at a throwaway one before any test module imports it.
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp(prefix='fomo-tests-')}/import.db"

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tracker.analytics import Fill

# Optional: run the app and migration tests on this database instead of one SQLite file per test (CI sets a
# PostgreSQL URL). Its `public` schema is dropped for every test, so the database name must contain "test".
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def database_url(tmp_path, name: str = "test.db") -> str:
    """A fresh, empty database for one test: a SQLite file, or TEST_DATABASE_URL with its schema recreated.
    Call it once per test and reuse the URL (each call empties the shared database again)."""
    if not TEST_DATABASE_URL:
        return f"sqlite:///{tmp_path}/{name}"
    url = make_url(TEST_DATABASE_URL)
    if url.get_backend_name() != "postgresql" or "test" not in (url.database or ""):
        raise RuntimeError("TEST_DATABASE_URL must be a PostgreSQL database whose name contains 'test'")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return TEST_DATABASE_URL

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
