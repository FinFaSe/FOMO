"""Alert quotes for prospective tracking: market quote at each buy/sell alert and 5 and 60 minutes later.

Revision ID: 0004_alert_quotes
Revises: 0003_bigint_times
Create Date: 2026-11-03 03:33:03.611756
"""

import sqlalchemy as sa
from alembic import op

revision = '0004_alert_quotes'
down_revision = '0003_bigint_times'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('alert_quotes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('alert_id', sa.Integer(), nullable=False),
    sa.Column('mint', sa.String(length=44), nullable=False),
    sa.Column('offset_seconds', sa.Integer(), nullable=False),
    sa.Column('due_at', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('fetched_at', sa.String(length=40), nullable=True),
    sa.Column('price_usd', sa.String(length=40), nullable=True),
    sa.Column('liquidity_usd', sa.String(length=40), nullable=True),
    sa.Column('source', sa.String(length=200), nullable=True),
    sa.ForeignKeyConstraint(['alert_id'], ['alerts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('alert_id', 'offset_seconds')
    )
    with op.batch_alter_table('alert_quotes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alert_quotes_alert_id'), ['alert_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_alert_quotes_due_at'), ['due_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_alert_quotes_mint'), ['mint'], unique=False)
        batch_op.create_index(batch_op.f('ix_alert_quotes_status'), ['status'], unique=False)



def downgrade():
    with op.batch_alter_table('alert_quotes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alert_quotes_status'))
        batch_op.drop_index(batch_op.f('ix_alert_quotes_mint'))
        batch_op.drop_index(batch_op.f('ix_alert_quotes_due_at'))
        batch_op.drop_index(batch_op.f('ix_alert_quotes_alert_id'))

    op.drop_table('alert_quotes')
