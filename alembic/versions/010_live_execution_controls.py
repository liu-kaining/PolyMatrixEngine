"""add V2 exchange metadata and persistent execution controls

Revision ID: 010
Revises: 009
Create Date: 2026-08-26 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "markets_meta", sa.Column("minimum_order_size", sa.Numeric(24, 8), nullable=True)
    )
    op.add_column(
        "markets_meta", sa.Column("tick_size", sa.Numeric(10, 8), nullable=True)
    )
    op.add_column("markets_meta", sa.Column("neg_risk", sa.Boolean(), nullable=True))
    op.add_column("fill_events", sa.Column("liquidity_role", sa.String(), nullable=True))
    op.add_column(
        "fill_events", sa.Column("fee_rate_bps", sa.Numeric(20, 8), nullable=True)
    )
    op.create_check_constraint(
        "ck_fill_event_liquidity_role",
        "fill_events",
        "liquidity_role IS NULL OR liquidity_role IN ('MAKER', 'TAKER')",
    )
    op.create_check_constraint(
        "ck_fill_event_fee_rate_bounds",
        "fill_events",
        "fee_rate_bps IS NULL OR (fee_rate_bps >= 0 AND fee_rate_bps <= 10000)",
    )

    for table, column, nullable in (
        ("orders_journal", "size", True),
        ("fill_events", "size", False),
        ("inventory_ledger", "yes_exposure", True),
        ("inventory_ledger", "no_exposure", True),
        ("inventory_ledger", "yes_capital_used", True),
        ("inventory_ledger", "no_capital_used", True),
        ("inventory_ledger", "realized_pnl", True),
        ("risk_reservations", "original_size", False),
        ("risk_reservations", "remaining_size", False),
        ("risk_reservations", "reserved_notional", False),
    ):
        op.alter_column(
            table,
            column,
            existing_type=sa.Numeric(20, 4),
            type_=sa.Numeric(24, 8),
            existing_nullable=nullable,
        )

    op.create_table(
        "trading_control_state",
        sa.Column("control_id", sa.String(), nullable=False),
        sa.Column("halted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("incident_id", sa.String(), nullable=True),
        sa.Column("state_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("halted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("control_id"),
    )
    op.create_table(
        "execution_leases",
        sa.Column("wallet_id", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("fencing_token", sa.Integer(), server_default="1", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("wallet_id"),
        sa.CheckConstraint(
            "fencing_token > 0", name="ck_execution_lease_fence_positive"
        ),
    )


def downgrade() -> None:
    op.drop_table("execution_leases")
    op.drop_table("trading_control_state")
    for table, column, nullable in (
        ("risk_reservations", "reserved_notional", False),
        ("risk_reservations", "remaining_size", False),
        ("risk_reservations", "original_size", False),
        ("inventory_ledger", "realized_pnl", True),
        ("inventory_ledger", "no_capital_used", True),
        ("inventory_ledger", "yes_capital_used", True),
        ("inventory_ledger", "no_exposure", True),
        ("inventory_ledger", "yes_exposure", True),
        ("fill_events", "size", False),
        ("orders_journal", "size", True),
    ):
        op.alter_column(
            table,
            column,
            existing_type=sa.Numeric(24, 8),
            type_=sa.Numeric(20, 4),
            existing_nullable=nullable,
        )
    op.drop_constraint(
        "ck_fill_event_fee_rate_bounds", "fill_events", type_="check"
    )
    op.drop_constraint(
        "ck_fill_event_liquidity_role", "fill_events", type_="check"
    )
    op.drop_column("fill_events", "fee_rate_bps")
    op.drop_column("fill_events", "liquidity_role")
    op.drop_column("markets_meta", "neg_risk")
    op.drop_column("markets_meta", "tick_size")
    op.drop_column("markets_meta", "minimum_order_size")
