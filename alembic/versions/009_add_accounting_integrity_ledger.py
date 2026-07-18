"""add immutable fill cash facts and accounting audit records

Revision ID: 009
Revises: 008
Create Date: 2026-07-18 03:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fill_events",
        sa.Column("accounting_state_version", sa.Integer(), nullable=True),
    )
    op.create_table(
        "fill_cash_ledger",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("market_id", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("gross_cash_delta", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("fee_amount", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("net_cash_delta", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("fee_status", sa.String(), server_default="UNKNOWN", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_fill_cash_side"),
        sa.CheckConstraint(
            "fee_status IN ('KNOWN', 'UNKNOWN')",
            name="ck_fill_cash_fee_status",
        ),
        sa.CheckConstraint(
            "fee_amount IS NULL OR fee_amount >= 0",
            name="ck_fill_cash_fee_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["fill_events.event_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["market_id"], ["markets_meta.condition_id"]),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_fill_cash_ledger_market_id",
        "fill_cash_ledger",
        ["market_id"],
        unique=False,
    )
    op.create_table(
        "accounting_audit_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="STARTED", nullable=False),
        sa.Column("inventory_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fill_count", sa.Integer(), server_default="0", nullable=False),
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


def downgrade() -> None:
    op.drop_table("accounting_audit_runs")
    op.drop_index("ix_fill_cash_ledger_market_id", table_name="fill_cash_ledger")
    op.drop_table("fill_cash_ledger")
    op.drop_column("fill_events", "accounting_state_version")
