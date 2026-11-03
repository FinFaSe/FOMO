"""Alert commitment: provisional alerts from `confirmed` transactions, upgraded once the trade is finalized.

Existing alerts were all raised from finalized trades (server default `finalized`).

Revision ID: 0007_alert_commitment
Revises: 0006_ranking
Create Date: 2026-11-03 18:28:30.933539
"""

import sqlalchemy as sa
from alembic import op

revision = '0007_alert_commitment'
down_revision = '0006_ranking'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('commitment', sa.String(length=20), server_default='finalized', nullable=False))
        batch_op.add_column(sa.Column('finalized_at', sa.String(length=40), nullable=True))
        batch_op.create_index(batch_op.f('ix_alerts_commitment'), ['commitment'], unique=False)



def downgrade():
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alerts_commitment'))
        batch_op.drop_column('finalized_at')
        batch_op.drop_column('commitment')

