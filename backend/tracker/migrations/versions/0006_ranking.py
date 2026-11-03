"""Precomputed ranking: one entry per wallet and filter set, and the wallet revision that invalidates it.

Existing wallets start at revision 0 with no entries: the first read or worker pass computes them.

Revision ID: 0006_ranking
Revises: 0005_price_history
Create Date: 2026-11-03 13:47:06.895711
"""

import sqlalchemy as sa
from alembic import op

revision = '0006_ranking'
down_revision = '0005_price_history'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('ranking_entries',
    sa.Column('wallet', sa.String(length=44), nullable=False),
    sa.Column('filters_key', sa.String(length=64), nullable=False),
    sa.Column('filters', sa.JSON(), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('engine', sa.String(length=100), nullable=False),
    sa.Column('as_of', sa.String(length=40), nullable=False),
    sa.Column('eligible', sa.Boolean(), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('summary', sa.JSON(), nullable=False),
    sa.Column('result', sa.JSON(), nullable=False),
    sa.Column('snapshot_id', sa.String(length=64), nullable=False),
    sa.Column('computed_at', sa.String(length=40), nullable=False),
    sa.Column('compute_ms', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['wallet'], ['wallets.address'], ),
    sa.PrimaryKeyConstraint('wallet', 'filters_key')
    )
    with op.batch_alter_table('ranking_entries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ranking_entries_computed_at'), ['computed_at'], unique=False)
        batch_op.create_index('ix_ranking_entries_order', ['filters_key', 'eligible', 'score'], unique=False)
        batch_op.create_index(batch_op.f('ix_ranking_entries_snapshot_id'), ['snapshot_id'], unique=False)

    with op.batch_alter_table('wallets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('analysis_revision', sa.Integer(), server_default='0', nullable=False))



def downgrade():
    with op.batch_alter_table('wallets', schema=None) as batch_op:
        batch_op.drop_column('analysis_revision')

    with op.batch_alter_table('ranking_entries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ranking_entries_snapshot_id'))
        batch_op.drop_index('ix_ranking_entries_order')
        batch_op.drop_index(batch_op.f('ix_ranking_entries_computed_at'))

    op.drop_table('ranking_entries')
