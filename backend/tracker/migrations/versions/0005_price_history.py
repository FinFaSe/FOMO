"""Price history: swap prices seen in stored transactions, pool candles and their download state.

Revision ID: 0005_price_history
Revises: 0004_alert_quotes
Create Date: 2026-11-03 03:47:34.438292
"""

import sqlalchemy as sa
from alembic import op

revision = '0005_price_history'
down_revision = '0004_alert_quotes'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('pool_candles',
    sa.Column('pool', sa.String(length=64), nullable=False),
    sa.Column('timeframe', sa.String(length=10), nullable=False),
    sa.Column('ts', sa.BigInteger(), nullable=False),
    sa.Column('mint', sa.String(length=44), nullable=False),
    sa.Column('quote_mint', sa.String(length=44), nullable=True),
    sa.Column('open', sa.String(length=60), nullable=False),
    sa.Column('high', sa.String(length=60), nullable=False),
    sa.Column('low', sa.String(length=60), nullable=False),
    sa.Column('close', sa.String(length=60), nullable=False),
    sa.Column('volume', sa.String(length=60), nullable=True),
    sa.Column('source', sa.String(length=200), nullable=False),
    sa.PrimaryKeyConstraint('pool', 'timeframe', 'ts')
    )
    with op.batch_alter_table('pool_candles', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pool_candles_mint'), ['mint'], unique=False)

    op.create_table('price_histories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('mint', sa.String(length=44), nullable=False),
    sa.Column('timeframe', sa.String(length=10), nullable=False),
    sa.Column('requested_from', sa.BigInteger(), nullable=False),
    sa.Column('requested_to', sa.BigInteger(), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('pool', sa.String(length=64), nullable=True),
    sa.Column('quote_mint', sa.String(length=44), nullable=True),
    sa.Column('dex', sa.String(length=40), nullable=True),
    sa.Column('cursor', sa.BigInteger(), nullable=True),
    sa.Column('candles', sa.Integer(), nullable=False),
    sa.Column('requests', sa.Integer(), nullable=False),
    sa.Column('requested_by', sa.String(length=20), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('locked_by', sa.String(length=80), nullable=True),
    sa.Column('heartbeat_at', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.String(length=40), nullable=False),
    sa.Column('finished_at', sa.String(length=40), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('price_histories', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_price_histories_mint'), ['mint'], unique=False)
        batch_op.create_index(batch_op.f('ix_price_histories_state'), ['state'], unique=False)

    op.create_table('price_points',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('mint', sa.String(length=44), nullable=False),
    sa.Column('signature', sa.String(length=88), nullable=False),
    sa.Column('event_index', sa.Integer(), nullable=False),
    sa.Column('signer', sa.String(length=44), nullable=False),
    sa.Column('side', sa.String(length=10), nullable=False),
    sa.Column('slot', sa.BigInteger(), nullable=False),
    sa.Column('block_time', sa.BigInteger(), nullable=True),
    sa.Column('price_sol', sa.String(length=60), nullable=False),
    sa.Column('sol_amount', sa.String(length=60), nullable=False),
    sa.Column('token_amount', sa.String(length=60), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('signature', 'event_index', 'signer')
    )
    with op.batch_alter_table('price_points', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_price_points_block_time'), ['block_time'], unique=False)
        batch_op.create_index(batch_op.f('ix_price_points_mint'), ['mint'], unique=False)


def downgrade():
    with op.batch_alter_table('price_points', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_price_points_mint'))
        batch_op.drop_index(batch_op.f('ix_price_points_block_time'))

    op.drop_table('price_points')
    with op.batch_alter_table('price_histories', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_price_histories_state'))
        batch_op.drop_index(batch_op.f('ix_price_histories_mint'))

    op.drop_table('price_histories')
    with op.batch_alter_table('pool_candles', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pool_candles_mint'))

    op.drop_table('pool_candles')