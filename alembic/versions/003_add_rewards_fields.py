"""add rewards_min_size and rewards_max_spread to markets_meta

Revision ID: 003
Revises: 002
Create Date: 2026-03-03 23:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('markets_meta', sa.Column('rewards_min_size', sa.Numeric(20, 4), nullable=True))
    op.add_column('markets_meta', sa.Column('rewards_max_spread', sa.Numeric(10, 4), nullable=True))


def downgrade() -> None:
    op.drop_column('markets_meta', 'rewards_max_spread')
    op.drop_column('markets_meta', 'rewards_min_size')
