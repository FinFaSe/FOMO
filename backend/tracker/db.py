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
