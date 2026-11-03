import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class Wallet(Base):
    __tablename__ = "wallets"
    address: Mapped[str] = mapped_column(String(44), primary_key=True)
    chain: Mapped[str] = mapped_column(String(20), default="solana")
    label: Mapped[str] = mapped_column(String(80), default="")
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    identity: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    sync: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class SyncState(Base):
    """Incremental scan cursors. Every signature between head and tail has been processed.

    Signatures are listed newest first, so coverage grows in two directions: catch-up (newer than
    head, bounded by `until`) and backfill (older than tail, bounded by `before`). An interrupted
    catch-up keeps its own cursor so the gap between it and head is never silently skipped.
    """

    __tablename__ = "sync_state"
    wallet: Mapped[str] = mapped_column(ForeignKey("wallets.address"), primary_key=True)
    head_signature: Mapped[str | None] = mapped_column(String(88), nullable=True)
    tail_signature: Mapped[str | None] = mapped_column(String(88), nullable=True)
    tail_block_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Every signature with block_time >= covered_from has been processed (between head and tail).
    covered_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    history_start_reached: Mapped[bool] = mapped_column(Boolean, default=False)
    catchup_before: Mapped[str | None] = mapped_column(String(88), nullable=True)
    catchup_head: Mapped[str | None] = mapped_column(String(88), nullable=True)
    missing_block_times: Mapped[int] = mapped_column(Integer, default=0)
    # Normalizer version that produced this wallet's trades; a mismatch triggers a local rebuild.
    normalizer: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Every finalized signature with block_time in [covered_from, caught_up_at] is stored.
    caught_up_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_iso)


class RawTransaction(Base):
    __tablename__ = "raw_transactions"
    signature: Mapped[str] = mapped_column(String(88), primary_key=True)
    slot: Mapped[int] = mapped_column(Integer)
    block_time: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(200))
    fetched_at: Mapped[str] = mapped_column(String(40), default=now_iso)


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (UniqueConstraint("wallet", "signature"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    wallet: Mapped[str] = mapped_column(ForeignKey("wallets.address"), index=True)
    signature: Mapped[str] = mapped_column(ForeignKey("raw_transactions.signature"))
    details: Mapped[dict] = mapped_column(JSON)


class EventRecord(Base):
    __tablename__ = "ledger_events"
    __table_args__ = (UniqueConstraint("wallet", "signature", "event_index", "token"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    wallet: Mapped[str] = mapped_column(ForeignKey("wallets.address"), index=True)
    signature: Mapped[str] = mapped_column(String(88), index=True)
    event_index: Mapped[int] = mapped_column(Integer)
    token: Mapped[str] = mapped_column(String(44), index=True)
    timestamp: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    # Owned by the server, deliberately absent from the public import contract.
    evidence: Mapped[dict] = mapped_column(JSON)


class Trade(Base):
    """Derived from stored RawTransaction evidence by tracker.normalize; rebuildable without RPC."""

    __tablename__ = "trades"
    __table_args__ = (UniqueConstraint("wallet", "signature", "event_index"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    wallet: Mapped[str] = mapped_column(ForeignKey("wallets.address"), index=True)
    signature: Mapped[str] = mapped_column(ForeignKey("raw_transactions.signature"), index=True)
    event_index: Mapped[int] = mapped_column(Integer)
    mint: Mapped[str] = mapped_column(String(44), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    slot: Mapped[int] = mapped_column(Integer)
    block_time: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Decimal amounts are stored as strings to keep exact values across SQLite and PostgreSQL.
    details: Mapped[dict] = mapped_column(JSON)
    normalizer: Mapped[str] = mapped_column(String(40))


class PriceCandle(Base):
    """Cached 1-minute candle from a public price source; prices are exact decimal strings."""

    __tablename__ = "price_candles"
    source: Mapped[str] = mapped_column(String(200), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    minute: Mapped[int] = mapped_column(Integer, primary_key=True)
    open: Mapped[str] = mapped_column(String(40))
    high: Mapped[str] = mapped_column(String(40))
    low: Mapped[str] = mapped_column(String(40))
    close: Mapped[str] = mapped_column(String(40))
    fetched_at: Mapped[str] = mapped_column(String(40), default=now_iso)


class AnalysisSnapshot(Base):
    __tablename__ = "analysis_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    wallet: Mapped[str] = mapped_column(ForeignKey("wallets.address"), index=True)
    as_of: Mapped[str] = mapped_column(String(40), index=True)
    result: Mapped[dict] = mapped_column(JSON)


class SourceCapture(Base):
    """Provider response retained privately; never re-served as a raw FOMO dataset."""

    __tablename__ = "source_captures"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(200))
    captured_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)


def database(url: str | None = None):
    url = url or os.getenv("DATABASE_URL", "sqlite:///./.data/tracker.db")
    if url.startswith("sqlite"):
        Path(".data").mkdir(exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)
