"""Tracker tables: incremental sync, trades, prices, jobs, tokens, discovery, funding and alerts.

Tolerant of development databases created by intermediate versions with Base.metadata.create_all: a table that
already exists is kept; only its missing columns and indexes are added and 32-bit integers declared as BigInteger
are widened, so no data is dropped.
Later schema changes must be new revisions (``alembic revision --autogenerate -m "..."``), not edits here.

Revision ID: 0002_tracker
Revises: 0001_baseline
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_tracker"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _create_table(name, *items, **kw):
    inspector = _inspector()
    if not inspector.has_table(name):
        op.create_table(name, *items, **kw)
        return
    existing = {c["name"]: c for c in inspector.get_columns(name)}
    columns = [i for i in items if isinstance(i, sa.Column)]
    missing = [c for c in columns if c.name not in existing]
    # Earlier versions stored slots and times as 32-bit integers: widen them (the data is kept).
    widen = [c for c in columns if c.name in existing and isinstance(c.type, sa.BigInteger)
             and not isinstance(existing[c.name]["type"], sa.BigInteger)]
    if not missing and not widen:
        return
    with op.batch_alter_table(name) as batch:
        for column in missing:
            # Rows already exist: a required column without a server default is added as nullable.
            if not column.nullable and column.server_default is None:
                column.nullable = True
            batch.add_column(column)
        for column in widen:
            batch.alter_column(column.name, existing_type=existing[column.name]["type"], type_=sa.BigInteger(),
                               existing_nullable=existing[column.name]["nullable"])


def _create_index(index_name, table, columns, unique=False):
    if index_name not in {i["name"] for i in _inspector().get_indexes(table)}:
        op.create_index(index_name, table, columns, unique=unique)


def upgrade():
    _create_table('alert_state',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('last_trade_id', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    _create_table('alerts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('wallet', sa.String(length=44), nullable=True),
    sa.Column('mint', sa.String(length=44), nullable=False),
    sa.Column('signature', sa.String(length=88), nullable=True),
    sa.Column('event_time', sa.BigInteger(), nullable=True),
    sa.Column('detected_at', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('delivery_state', sa.String(length=20), nullable=False),
    sa.Column('delivery_attempts', sa.Integer(), nullable=False),
    sa.Column('delivery_error', sa.String(length=300), nullable=True),
    sa.Column('delivered_at', sa.String(length=40), nullable=True),
    sa.Column('read', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )
    _create_index('ix_alerts_created_at', 'alerts', ['created_at'], unique=False)
    _create_index('ix_alerts_delivery_state', 'alerts', ['delivery_state'], unique=False)
    _create_index('ix_alerts_kind', 'alerts', ['kind'], unique=False)
    _create_index('ix_alerts_mint', 'alerts', ['mint'], unique=False)
    _create_index('ix_alerts_wallet', 'alerts', ['wallet'], unique=False)
    _create_table('discovery_seeds',
    sa.Column('mint', sa.String(length=44), nullable=False),
    sa.Column('label', sa.String(length=80), nullable=False),
    sa.Column('scan_address', sa.String(length=44), nullable=False),
    sa.Column('address_kind', sa.String(length=30), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('params', sa.JSON(), nullable=False),
    sa.Column('list_before', sa.String(length=88), nullable=True),
    sa.Column('pages', sa.Integer(), nullable=False),
    sa.Column('reached_start', sa.Boolean(), nullable=False),
    sa.Column('oldest', sa.JSON(), nullable=True),
    sa.Column('launch_signature', sa.String(length=88), nullable=True),
    sa.Column('launch_slot', sa.BigInteger(), nullable=True),
    sa.Column('launch_time', sa.BigInteger(), nullable=True),
    sa.Column('creator', sa.String(length=44), nullable=True),
    sa.Column('outcome', sa.JSON(), nullable=True),
    sa.Column('declared_peak_cap_usd', sa.String(length=40), nullable=True),
    sa.Column('declared_peak_source', sa.String(length=500), nullable=True),
    sa.Column('locked_by', sa.String(length=80), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('heartbeat_at', sa.String(length=40), nullable=True),
    sa.Column('finished_at', sa.String(length=40), nullable=True),
    sa.PrimaryKeyConstraint('mint')
    )
    _create_index('ix_discovery_seeds_state', 'discovery_seeds', ['state'], unique=False)
    _create_table('pending_signatures',
    sa.Column('signature', sa.String(length=88), nullable=False),
    sa.Column('source', sa.String(length=40), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.PrimaryKeyConstraint('signature')
    )
    _create_table('price_candles',
    sa.Column('source', sa.String(length=200), nullable=False),
    sa.Column('symbol', sa.String(length=20), nullable=False),
    sa.Column('minute', sa.BigInteger(), nullable=False),
    sa.Column('open', sa.String(length=40), nullable=False),
    sa.Column('high', sa.String(length=40), nullable=False),
    sa.Column('low', sa.String(length=40), nullable=False),
    sa.Column('close', sa.String(length=40), nullable=False),
    sa.Column('fetched_at', sa.String(length=40), nullable=False),
    sa.PrimaryKeyConstraint('source', 'symbol', 'minute')
    )
    _create_table('token_mints',
    sa.Column('mint', sa.String(length=44), nullable=False),
    sa.Column('exists', sa.Boolean(), nullable=False),
    sa.Column('program', sa.String(length=44), nullable=True),
    sa.Column('decimals', sa.Integer(), nullable=True),
    sa.Column('supply_raw', sa.String(length=40), nullable=True),
    sa.Column('mint_authority', sa.String(length=44), nullable=True),
    sa.Column('freeze_authority', sa.String(length=44), nullable=True),
    sa.Column('extensions', sa.JSON(), nullable=True),
    sa.Column('pump_bonding_curve', sa.String(length=44), nullable=True),
    sa.Column('pump_origin', sa.Boolean(), nullable=False),
    sa.Column('source', sa.String(length=200), nullable=False),
    sa.Column('fetched_at', sa.String(length=40), nullable=False),
    sa.PrimaryKeyConstraint('mint')
    )
    _create_table('token_quotes',
    sa.Column('mint', sa.String(length=44), nullable=False),
    sa.Column('found', sa.Boolean(), nullable=False),
    sa.Column('price_usd', sa.String(length=40), nullable=True),
    sa.Column('liquidity_usd', sa.String(length=40), nullable=True),
    sa.Column('market_cap_usd', sa.String(length=40), nullable=True),
    sa.Column('fdv_usd', sa.String(length=40), nullable=True),
    sa.Column('pair_address', sa.String(length=64), nullable=True),
    sa.Column('dex_id', sa.String(length=40), nullable=True),
    sa.Column('symbol', sa.String(length=40), nullable=True),
    sa.Column('source', sa.String(length=200), nullable=False),
    sa.Column('fetched_at', sa.String(length=40), nullable=False),
    sa.PrimaryKeyConstraint('mint')
    )
    _create_table('wallet_funding',
    sa.Column('address', sa.String(length=44), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('funder', sa.String(length=44), nullable=True),
    sa.Column('funder_kind', sa.String(length=20), nullable=True),
    sa.Column('signature', sa.String(length=88), nullable=True),
    sa.Column('block_time', sa.BigInteger(), nullable=True),
    sa.Column('lamports', sa.String(length=40), nullable=True),
    sa.Column('reason', sa.String(length=200), nullable=True),
    sa.Column('requested_by', sa.String(length=20), nullable=False),
    sa.Column('locked_by', sa.String(length=80), nullable=True),
    sa.Column('heartbeat_at', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('fetched_at', sa.String(length=40), nullable=True),
    sa.PrimaryKeyConstraint('address')
    )
    _create_index('ix_wallet_funding_funder', 'wallet_funding', ['funder'], unique=False)
    _create_index('ix_wallet_funding_state', 'wallet_funding', ['state'], unique=False)
    _create_table('workers',
    sa.Column('id', sa.String(length=80), nullable=False),
    sa.Column('started_at', sa.String(length=40), nullable=False),
    sa.Column('heartbeat_at', sa.String(length=40), nullable=False),
    sa.Column('info', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    _create_table('early_buys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('mint', sa.String(length=44), nullable=False),
    sa.Column('wallet', sa.String(length=44), nullable=False),
    sa.Column('signature', sa.String(length=88), nullable=False),
    sa.Column('event_index', sa.Integer(), nullable=False),
    sa.Column('slot', sa.BigInteger(), nullable=False),
    sa.Column('block_time', sa.BigInteger(), nullable=True),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['mint'], ['discovery_seeds.mint'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('mint', 'wallet', 'signature', 'event_index')
    )
    _create_index('ix_early_buys_mint', 'early_buys', ['mint'], unique=False)
    _create_index('ix_early_buys_wallet', 'early_buys', ['wallet'], unique=False)
    _create_table('jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('wallet', sa.String(length=44), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('params', sa.JSON(), nullable=False),
    sa.Column('origin', sa.String(length=20), nullable=False),
    sa.Column('parent_id', sa.Integer(), nullable=True),
    sa.Column('round', sa.Integer(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('run_after', sa.String(length=40), nullable=False),
    sa.Column('locked_by', sa.String(length=80), nullable=True),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('started_at', sa.String(length=40), nullable=True),
    sa.Column('heartbeat_at', sa.String(length=40), nullable=True),
    sa.Column('finished_at', sa.String(length=40), nullable=True),
    sa.Column('progress', sa.JSON(), nullable=True),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['wallet'], ['wallets.address'], ),
    sa.PrimaryKeyConstraint('id')
    )
    _create_index('ix_jobs_run_after', 'jobs', ['run_after'], unique=False)
    _create_index('ix_jobs_state', 'jobs', ['state'], unique=False)
    _create_index('ix_jobs_wallet', 'jobs', ['wallet'], unique=False)
    _create_table('sync_state',
    sa.Column('wallet', sa.String(length=44), nullable=False),
    sa.Column('head_signature', sa.String(length=88), nullable=True),
    sa.Column('tail_signature', sa.String(length=88), nullable=True),
    sa.Column('tail_block_time', sa.BigInteger(), nullable=True),
    sa.Column('covered_from', sa.BigInteger(), nullable=True),
    sa.Column('history_start_reached', sa.Boolean(), nullable=False),
    sa.Column('catchup_before', sa.String(length=88), nullable=True),
    sa.Column('catchup_head', sa.String(length=88), nullable=True),
    sa.Column('missing_block_times', sa.Integer(), nullable=False),
    sa.Column('normalizer', sa.String(length=40), nullable=True),
    sa.Column('caught_up_at', sa.String(length=40), nullable=True),
    sa.Column('watch_head', sa.String(length=88), nullable=True),
    sa.Column('updated_at', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['wallet'], ['wallets.address'], ),
    sa.PrimaryKeyConstraint('wallet')
    )
    _create_table('trades',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('wallet', sa.String(length=44), nullable=False),
    sa.Column('signature', sa.String(length=88), nullable=False),
    sa.Column('event_index', sa.Integer(), nullable=False),
    sa.Column('mint', sa.String(length=44), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('slot', sa.BigInteger(), nullable=False),
    sa.Column('block_time', sa.BigInteger(), nullable=True),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('normalizer', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['signature'], ['raw_transactions.signature'], ),
    sa.ForeignKeyConstraint(['wallet'], ['wallets.address'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('wallet', 'signature', 'event_index')
    )
    _create_index('ix_trades_block_time', 'trades', ['block_time'], unique=False)
    _create_index('ix_trades_mint', 'trades', ['mint'], unique=False)
    _create_index('ix_trades_signature', 'trades', ['signature'], unique=False)
    _create_index('ix_trades_wallet', 'trades', ['wallet'], unique=False)

def downgrade():
    op.drop_table("trades")
    op.drop_table("sync_state")
    op.drop_table("jobs")
    op.drop_table("early_buys")
    op.drop_table("workers")
    op.drop_table("wallet_funding")
    op.drop_table("token_quotes")
    op.drop_table("token_mints")
    op.drop_table("price_candles")
    op.drop_table("pending_signatures")
    op.drop_table("discovery_seeds")
    op.drop_table("alerts")
    op.drop_table("alert_state")
