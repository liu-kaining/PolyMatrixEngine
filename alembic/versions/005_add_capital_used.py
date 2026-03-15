"""add yes_capital_used and no_capital_used to inventory_ledger

Revision ID: 005
Revises: 004_add_reward_rate
Create Date: 2025-03-14 12:00:00.000000

Shares vs Dollars fix: track USDC cost basis per side for budget/risk checks.
"""
from alembic import op
import sqlalchemy as sa


revision = '005'
down_revision = '004_add_reward_rate'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('inventory_ledger', sa.Column('yes_capital_used', sa.Numeric(20, 4), server_default='0', nullable=False))
    op.add_column('inventory_ledger', sa.Column('no_capital_used', sa.Numeric(20, 4), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('inventory_ledger', 'no_capital_used')
    op.drop_column('inventory_ledger', 'yes_capital_used')
