"""64-bit slots and block times in raw_transactions.

Integer is 32-bit on PostgreSQL: Unix timestamps overflow it in 2038 and slots would eventually too (found by
running the app against PostgreSQL). The tracker tables already use BigInteger; this aligns the original table.
SQLite stores INTEGER as 64-bit already, so there it only rebuilds the table (batch mode), keeping the data.

Revision ID: 0003_bigint_times
Revises: 0002_tracker
Create Date: 2026-11-03 02:42:28.697932
"""

import sqlalchemy as sa
from alembic import op

revision = '0003_bigint_times'
down_revision = '0002_tracker'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('raw_transactions', schema=None) as batch_op:
        batch_op.alter_column('slot',
               existing_type=sa.INTEGER(),
               type_=sa.BigInteger(),
               existing_nullable=False)
        batch_op.alter_column('block_time',
               existing_type=sa.INTEGER(),
               type_=sa.BigInteger(),
               existing_nullable=True)


def downgrade():
    with op.batch_alter_table('raw_transactions', schema=None) as batch_op:
        batch_op.alter_column('block_time',
               existing_type=sa.BigInteger(),
               type_=sa.INTEGER(),
               existing_nullable=True)
        batch_op.alter_column('slot',
               existing_type=sa.BigInteger(),
               type_=sa.INTEGER(),
               existing_nullable=False)
