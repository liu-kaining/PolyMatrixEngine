"""add_token_ids

Revision ID: 002
Revises: 001
Create Date: 2026-02-22 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('markets_meta', sa.Column('yes_token_id', sa.String(), nullable=True))
    op.add_column('markets_meta', sa.Column('no_token_id', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('markets_meta', 'no_token_id')
    op.drop_column('markets_meta', 'yes_token_id')
