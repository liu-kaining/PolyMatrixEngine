"""add reward_rate_per_day to markets_meta

Revision ID: 004_add_reward_rate
Revises: 003_add_rewards_fields
Create Date: 2024-03-24 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '004_add_reward_rate'
down_revision = '003_add_rewards_fields'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('markets_meta', sa.Column('reward_rate_per_day', sa.Numeric(20, 4), nullable=True))


def downgrade():
    op.drop_column('markets_meta', 'reward_rate_per_day')
