"""add atomic portfolio risk reservations

Revision ID: 007
Revises: 006
Create Date: 2026-07-18 00:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL enum additions are intentionally one-way; UNKNOWN preserves risk when an
    # exchange reports "already matched" before the durable fill event is processed.
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'UNKNOWN'")

    op.create_table(
        "portfolio_risk_state",
        sa.Column("wallet_id", sa.String(), nullable=False),
        sa.Column(
            "reserved_buy_notional",
            sa.Numeric(precision=20, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("state_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reserved_buy_notional >= 0",
            name="ck_portfolio_reserved_nonnegative",
        ),
        sa.PrimaryKeyConstraint("wallet_id"),
    )
    op.execute(
        "INSERT INTO portfolio_risk_state "
        "(wallet_id, reserved_buy_notional, state_version) VALUES ('default', 0, 0) "
        "ON CONFLICT (wallet_id) DO NOTHING"
    )

    op.create_table(
        "risk_reservations",
        sa.Column("reservation_id", sa.String(), nullable=False),
        sa.Column("client_order_id", sa.String(), nullable=False),
        sa.Column("exchange_order_id", sa.String(), nullable=True),
        sa.Column("market_id", sa.String(), nullable=False),
        sa.Column("token_id", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("limit_price", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("original_size", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("remaining_size", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("reserved_notional", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("status", sa.String(), server_default="RESERVED", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["market_id"], ["markets_meta.condition_id"]),
        sa.CheckConstraint(
            "limit_price > 0 AND limit_price < 1",
            name="ck_risk_reservation_price",
        ),
        sa.CheckConstraint(
            "original_size > 0",
            name="ck_risk_reservation_original_size",
        ),
        sa.CheckConstraint(
            "remaining_size >= 0",
            name="ck_risk_reservation_remaining_size",
        ),
        sa.CheckConstraint(
            "reserved_notional >= 0",
            name="ck_risk_reservation_notional",
        ),
        sa.CheckConstraint(
            "side IN ('BUY', 'SELL')",
            name="ck_risk_reservation_side",
        ),
        sa.PrimaryKeyConstraint("reservation_id"),
    )
    op.create_index(
        "ix_risk_reservations_client_order_id",
        "risk_reservations",
        ["client_order_id"],
        unique=True,
    )
    op.create_index(
        "ix_risk_reservations_exchange_order_id",
        "risk_reservations",
        ["exchange_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_risk_reservations_market_id",
        "risk_reservations",
        ["market_id"],
        unique=False,
    )

    op.add_column(
        "orders_journal",
        sa.Column("reservation_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_orders_journal_reservation_id",
        "orders_journal",
        ["reservation_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_orders_journal_reservation_id", table_name="orders_journal")
    op.drop_column("orders_journal", "reservation_id")
    op.drop_index("ix_risk_reservations_market_id", table_name="risk_reservations")
    op.drop_index("ix_risk_reservations_exchange_order_id", table_name="risk_reservations")
    op.drop_index("ix_risk_reservations_client_order_id", table_name="risk_reservations")
    op.drop_table("risk_reservations")
    op.drop_table("portfolio_risk_state")
    # PostgreSQL cannot safely remove an enum value in-place. UNKNOWN remains available.
