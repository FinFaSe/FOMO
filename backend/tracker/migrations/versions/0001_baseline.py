"""Baseline: the schema of the imported MarkosAZ/FOMO code (six tables).

Existing databases created by that code (Base.metadata.create_all, no alembic_version table) are stamped at this
revision instead of running it, so their data is kept.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('raw_transactions',
    sa.Column('signature', sa.String(length=88), nullable=False),
    sa.Column('slot', sa.Integer(), nullable=False),
    sa.Column('block_time', sa.Integer(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.Column('source', sa.String(length=200), nullable=False),
    sa.Column('fetched_at', sa.String(length=40), nullable=False),
    sa.PrimaryKeyConstraint('signature')
    )
    op.create_index(op.f('ix_raw_transactions_block_time'), 'raw_transactions', ['block_time'], unique=False)
    op.create_table('source_captures',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=200), nullable=False),
    sa.Column('captured_at', sa.String(length=40), nullable=False),
    sa.Column('digest', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('wallets',
    sa.Column('address', sa.String(length=44), nullable=False),
    sa.Column('chain', sa.String(length=20), nullable=False),
    sa.Column('label', sa.String(length=80), nullable=False),
    sa.Column('favorite', sa.Boolean(), nullable=False),
    sa.Column('identity', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('sync', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('address')
    )
    op.create_table('analysis_snapshots',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('wallet', sa.String(length=44), nullable=False),
    sa.Column('as_of', sa.String(length=40), nullable=False),
    sa.Column('result', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['wallet'], ['wallets.address'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_analysis_snapshots_as_of'), 'analysis_snapshots', ['as_of'], unique=False)
    op.create_index(op.f('ix_analysis_snapshots_wallet'), 'analysis_snapshots', ['wallet'], unique=False)
    op.create_table('ledger_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('wallet', sa.String(length=44), nullable=False),
    sa.Column('signature', sa.String(length=88), nullable=False),
    sa.Column('event_index', sa.Integer(), nullable=False),
    sa.Column('token', sa.String(length=44), nullable=False),
    sa.Column('timestamp', sa.String(length=40), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('evidence', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['wallet'], ['wallets.address'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('wallet', 'signature', 'event_index', 'token')
    )
    op.create_index(op.f('ix_ledger_events_signature'), 'ledger_events', ['signature'], unique=False)
    op.create_index(op.f('ix_ledger_events_timestamp'), 'ledger_events', ['timestamp'], unique=False)
    op.create_index(op.f('ix_ledger_events_token'), 'ledger_events', ['token'], unique=False)
    op.create_index(op.f('ix_ledger_events_wallet'), 'ledger_events', ['wallet'], unique=False)
    op.create_table('observations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('wallet', sa.String(length=44), nullable=False),
    sa.Column('signature', sa.String(length=88), nullable=False),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['signature'], ['raw_transactions.signature'], ),
    sa.ForeignKeyConstraint(['wallet'], ['wallets.address'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('wallet', 'signature')
    )
    op.create_index(op.f('ix_observations_wallet'), 'observations', ['wallet'], unique=False)


def downgrade():
    op.drop_table("observations")
    op.drop_table("ledger_events")
    op.drop_table("analysis_snapshots")
    op.drop_table("wallets")
    op.drop_table("source_captures")
    op.drop_table("raw_transactions")
