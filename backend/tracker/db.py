import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
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


class Job(Base):
    """Background work item. Rows are claimed by a conditional UPDATE, so one runner wins each job."""

    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), default="sync")
    wallet: Mapped[str] = mapped_column(ForeignKey("wallets.address"), index=True)
    # queued → running → done | failed | cancelled; cancel_requested is honored between transactions.
    state: Mapped[str] = mapped_column(String(20), index=True, default="queued")
    params: Mapped[dict] = mapped_column(JSON)
    origin: Mapped[str] = mapped_column(String(20), default="manual")  # manual | api | schedule | chain
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    round: Mapped[int] = mapped_column(Integer, default=1)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[str] = mapped_column(String(40), default=now_iso, index=True)
    locked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    started_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkerHeartbeat(Base):
    __tablename__ = "workers"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    started_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    heartbeat_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    info: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class TokenMint(Base):
    """On-chain mint facts (getMultipleAccounts) plus proof of pump.fun origin. Refreshed periodically."""

    __tablename__ = "token_mints"
    mint: Mapped[str] = mapped_column(String(44), primary_key=True)
    exists: Mapped[bool] = mapped_column(Boolean, default=True)
    program: Mapped[str | None] = mapped_column(String(44), nullable=True)
    decimals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supply_raw: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mint_authority: Mapped[str | None] = mapped_column(String(44), nullable=True)
    freeze_authority: Mapped[str | None] = mapped_column(String(44), nullable=True)
    extensions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # pump.fun bonding-curve PDA for this mint and whether an account owned by pump.fun exists there.
    pump_bonding_curve: Mapped[str | None] = mapped_column(String(44), nullable=True)
    pump_origin: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(200))
    fetched_at: Mapped[str] = mapped_column(String(40), default=now_iso)


class TokenQuote(Base):
    """Latest market quote for a mint (mark price). Volatile: never part of an immutable snapshot."""

    __tablename__ = "token_quotes"
    mint: Mapped[str] = mapped_column(String(44), primary_key=True)
    found: Mapped[bool] = mapped_column(Boolean, default=False)
    price_usd: Mapped[str | None] = mapped_column(String(40), nullable=True)
    liquidity_usd: Mapped[str | None] = mapped_column(String(40), nullable=True)
    market_cap_usd: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fdv_usd: Mapped[str | None] = mapped_column(String(40), nullable=True)
    pair_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dex_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source: Mapped[str] = mapped_column(String(200))
    fetched_at: Mapped[str] = mapped_column(String(40), default=now_iso)


class DiscoverySeed(Base):
    """A token whose earliest buyers are scanned (backward discovery). Winners and failures alike."""

    __tablename__ = "discovery_seeds"
    mint: Mapped[str] = mapped_column(String(44), primary_key=True)
    label: Mapped[str] = mapped_column(String(80), default="")
    scan_address: Mapped[str] = mapped_column(String(44))
    address_kind: Mapped[str] = mapped_column(String(30))  # pump_bonding_curve | pool
    # queued → scanning → done | failed; a scan that ran out of page budget goes back to queued.
    state: Mapped[str] = mapped_column(String(20), index=True, default="queued")
    params: Mapped[dict] = mapped_column(JSON)
    list_before: Mapped[str | None] = mapped_column(String(88), nullable=True)
    pages: Mapped[int] = mapped_column(Integer, default=0)
    reached_start: Mapped[bool] = mapped_column(Boolean, default=False)
    oldest: Mapped[list | None] = mapped_column(JSON, nullable=True)  # oldest signatures seen, newest first
    launch_signature: Mapped[str | None] = mapped_column(String(88), nullable=True)
    launch_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    launch_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    creator: Mapped[str | None] = mapped_column(String(44), nullable=True)
    outcome: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    declared_peak_cap_usd: Mapped[str | None] = mapped_column(String(40), nullable=True)
    declared_peak_source: Mapped[str | None] = mapped_column(String(500), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    heartbeat_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class EarlyBuy(Base):
    """A buy among a seed's earliest trades. The wallet is a candidate, not necessarily a tracked wallet."""

    __tablename__ = "early_buys"
    __table_args__ = (UniqueConstraint("mint", "wallet", "signature", "event_index"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    mint: Mapped[str] = mapped_column(ForeignKey("discovery_seeds.mint"), index=True)
    wallet: Mapped[str] = mapped_column(String(44), index=True)
    signature: Mapped[str] = mapped_column(String(88))
    event_index: Mapped[int] = mapped_column(Integer)
    slot: Mapped[int] = mapped_column(Integer)
    block_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rank: Mapped[int] = mapped_column(Integer)  # 1 = first buyer after launch (ties share a slot)
    details: Mapped[dict] = mapped_column(JSON)


class WalletFunding(Base):
    """Who sent an address its first SOL (funding origin), looked up from its oldest transactions."""

    __tablename__ = "wallet_funding"
    address: Mapped[str] = mapped_column(String(44), primary_key=True)
    # queued → scanning → done | unknown (history too long, no incoming SOL) | failed
    state: Mapped[str] = mapped_column(String(20), index=True, default="queued")
    funder: Mapped[str | None] = mapped_column(String(44), nullable=True, index=True)
    # wallet | high_volume (exchange, router or bot: funds too many unrelated wallets to link them)
    funder_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    signature: Mapped[str | None] = mapped_column(String(88), nullable=True)
    block_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lamports: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(20), default="manual")
    locked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    fetched_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


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
    if url.startswith("sqlite"):
        # The API and the background worker are separate processes: wait for locks instead of failing,
        # and use WAL so readers are not blocked by the writer.
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()
    else:
        engine = create_engine(url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)
