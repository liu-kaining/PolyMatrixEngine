"""add stable exchange order mapping and idempotent fill event inbox

Revision ID: 006
Revises: 005
Create Date: 2026-07-18 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inventory_ledger",
        sa.Column("accounting_version", sa.String(), server_default="v1", nullable=False),
    )
    op.add_column(
        "inventory_ledger",
        sa.Column("state_version", sa.Integer(), server_default="0", nullable=False),
    )
    # Existing rows cannot be converted without historical fills. New rows use v2 and
    # legacy rows remain blocked until an explicit offline rebuild migrates them.
    op.alter_column(
        "inventory_ledger",
        "accounting_version",
        server_default="v2",
    )

    op.add_column(
        "orders_journal",
        sa.Column("exchange_order_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_orders_journal_exchange_order_id",
        "orders_journal",
        ["exchange_order_id"],
        unique=True,
    )
    # Existing non-local primary keys were exchange order identifiers in the legacy model.
    op.execute(
        "UPDATE orders_journal SET exchange_order_id = order_id "
        "WHERE order_id NOT LIKE 'local_%' AND exchange_order_id IS NULL"
    )

    op.create_table(
        "fill_events",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("exchange_order_id", sa.String(), nullable=False),
        sa.Column("local_order_id", sa.String(), nullable=True),
        sa.Column("market_id", sa.String(), nullable=True),
        sa.Column("token_id", sa.String(), nullable=True),
        sa.Column("side", sa.String(), nullable=True),
        sa.Column("price", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("size", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("status", sa.String(), server_default="RECEIVED", nullable=False),
        sa.Column("processing_error", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["local_order_id"], ["orders_journal.order_id"]),
        sa.ForeignKeyConstraint(["market_id"], ["markets_meta.condition_id"]),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_fill_events_exchange_order_id",
        "fill_events",
        ["exchange_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_fill_events_local_order_id",
        "fill_events",
        ["local_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_fill_events_market_id",
        "fill_events",
        ["market_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_fill_events_market_id", table_name="fill_events")
    op.drop_index("ix_fill_events_local_order_id", table_name="fill_events")
    op.drop_index("ix_fill_events_exchange_order_id", table_name="fill_events")
    op.drop_table("fill_events")
    op.drop_index("ix_orders_journal_exchange_order_id", table_name="orders_journal")
    op.drop_column("orders_journal", "exchange_order_id")
    op.drop_column("inventory_ledger", "accounting_version")
    op.drop_column("inventory_ledger", "state_version")
