"""add durable order reconciliation audit records

Revision ID: 008
Revises: 007
Create Date: 2026-07-18 02:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_reconciliation_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="STARTED", nullable=False),
        sa.Column("local_order_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("exchange_open_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("blocker_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "exchange_order_snapshots",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("exchange_order_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["order_reconciliation_runs.run_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "exchange_order_id"),
    )
    op.create_index(
        "ix_exchange_order_snapshots_exchange_order_id",
        "exchange_order_snapshots",
        ["exchange_order_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_exchange_order_snapshots_exchange_order_id",
        table_name="exchange_order_snapshots",
    )
    op.drop_table("exchange_order_snapshots")
    op.drop_table("order_reconciliation_runs")
