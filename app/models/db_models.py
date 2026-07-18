from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func
import enum

Base = declarative_base()

class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"

class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

class MarketMeta(Base):
    __tablename__ = "markets_meta"

    condition_id = Column(String, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True)
    end_date = Column(DateTime(timezone=True))
    status = Column(String)  # active, closed, resolved
    yes_token_id = Column(String)
    no_token_id = Column(String)
    rewards_min_size = Column(Numeric(20, 4), nullable=True)
    rewards_max_spread = Column(Numeric(10, 4), nullable=True)
    reward_rate_per_day = Column(Numeric(20, 4), nullable=True)

    # Relationships
    orders = relationship("OrderJournal", back_populates="market")
    inventory = relationship("InventoryLedger", back_populates="market", uselist=False)

class OrderJournal(Base):
    __tablename__ = "orders_journal"

    order_id = Column(String, primary_key=True, index=True)
    # Stable local/client identifier. New code never mutates this primary key after submit.
    exchange_order_id = Column(String, unique=True, index=True, nullable=True)
    reservation_id = Column(String, unique=True, index=True, nullable=True)
    market_id = Column(String, ForeignKey("markets_meta.condition_id"), index=True)
    side = Column(Enum(OrderSide))
    price = Column(Numeric(10, 4))
    size = Column(Numeric(20, 4))
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING)
    payload = Column(JSON)  # Store original JSON from SDK
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    market = relationship("MarketMeta", back_populates="orders")


class FillEvent(Base):
    """Durable, idempotent user-stream fill inbox and processing record."""

    __tablename__ = "fill_events"

    event_id = Column(String, primary_key=True)
    exchange_order_id = Column(String, index=True, nullable=False)
    local_order_id = Column(String, ForeignKey("orders_journal.order_id"), index=True, nullable=True)
    market_id = Column(String, ForeignKey("markets_meta.condition_id"), index=True, nullable=True)
    token_id = Column(String, nullable=True)
    side = Column(String, nullable=True)
    price = Column(Numeric(10, 4), nullable=False)
    size = Column(Numeric(20, 4), nullable=False)
    status = Column(String, nullable=False, default="RECEIVED")
    processing_error = Column(String, nullable=True)
    # Inventory state version produced by this exact fill transaction. It makes
    # deterministic replay possible and exposes any non-fill ledger mutation.
    accounting_state_version = Column(Integer, nullable=True)
    payload = Column(JSON, nullable=False)
    received_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)


class FillCashLedger(Base):
    """Immutable cash fact paired one-to-one with a processed fill event."""

    __tablename__ = "fill_cash_ledger"
    __table_args__ = (
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_fill_cash_side"),
        CheckConstraint("fee_status IN ('KNOWN', 'UNKNOWN')", name="ck_fill_cash_fee_status"),
        CheckConstraint("fee_amount IS NULL OR fee_amount >= 0", name="ck_fill_cash_fee_nonnegative"),
    )

    event_id = Column(
        String,
        ForeignKey("fill_events.event_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    market_id = Column(String, ForeignKey("markets_meta.condition_id"), index=True, nullable=False)
    side = Column(String, nullable=False)
    # Gross cash is negative for BUY and positive for SELL.
    gross_cash_delta = Column(Numeric(24, 8), nullable=False)
    fee_amount = Column(Numeric(24, 8), nullable=True)
    net_cash_delta = Column(Numeric(24, 8), nullable=True)
    fee_status = Column(String, nullable=False, default="UNKNOWN")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

class InventoryLedger(Base):
    __tablename__ = "inventory_ledger"

    market_id = Column(String, ForeignKey("markets_meta.condition_id"), primary_key=True)
    yes_exposure = Column(Numeric(20, 4), default=0)
    no_exposure = Column(Numeric(20, 4), default=0)
    yes_capital_used = Column(Numeric(20, 4), default=0)  # USDC spent on YES (cost basis)
    no_capital_used = Column(Numeric(20, 4), default=0)   # USDC spent on NO (cost basis)
    realized_pnl = Column(Numeric(20, 4), default=0)
    # v1 stored net trade cash flow here; verified v2 uses fee-aware average-cost net PnL.
    accounting_version = Column(String, nullable=False, default="v2")
    state_version = Column(Integer, nullable=False, default=0)

    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    market = relationship("MarketMeta", back_populates="inventory")


class PortfolioRiskState(Base):
    """Singleton row used to serialize wallet-wide reservation decisions."""

    __tablename__ = "portfolio_risk_state"
    __table_args__ = (
        CheckConstraint(
            "reserved_buy_notional >= 0",
            name="ck_portfolio_reserved_nonnegative",
        ),
    )

    wallet_id = Column(String, primary_key=True, default="default")
    reserved_buy_notional = Column(Numeric(20, 4), nullable=False, default=0)
    state_version = Column(Integer, nullable=False, default=0)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class RiskReservation(Base):
    """Durable BUY-capital or SELL-inventory reservation acquired before submit."""

    __tablename__ = "risk_reservations"
    __table_args__ = (
        CheckConstraint(
            "limit_price > 0 AND limit_price < 1",
            name="ck_risk_reservation_price",
        ),
        CheckConstraint(
            "original_size > 0",
            name="ck_risk_reservation_original_size",
        ),
        CheckConstraint(
            "remaining_size >= 0",
            name="ck_risk_reservation_remaining_size",
        ),
        CheckConstraint(
            "reserved_notional >= 0",
            name="ck_risk_reservation_notional",
        ),
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_risk_reservation_side"),
    )

    reservation_id = Column(String, primary_key=True)
    client_order_id = Column(String, unique=True, index=True, nullable=False)
    exchange_order_id = Column(String, index=True, nullable=True)
    market_id = Column(String, ForeignKey("markets_meta.condition_id"), index=True, nullable=False)
    token_id = Column(String, nullable=False)
    side = Column(String, nullable=False)
    limit_price = Column(Numeric(10, 4), nullable=False)
    original_size = Column(Numeric(20, 4), nullable=False)
    remaining_size = Column(Numeric(20, 4), nullable=False)
    reserved_notional = Column(Numeric(20, 4), nullable=False)
    status = Column(String, nullable=False, default="RESERVED")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OrderReconciliationRun(Base):
    """Durable audit record for one authoritative open-order reconciliation pass."""

    __tablename__ = "order_reconciliation_runs"

    run_id = Column(String, primary_key=True)
    status = Column(String, nullable=False, default="STARTED")
    local_order_count = Column(Integer, nullable=False, default=0)
    exchange_open_count = Column(Integer, nullable=False, default=0)
    blocker_count = Column(Integer, nullable=False, default=0)
    summary = Column(JSON, nullable=False, default=dict)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class ExchangeOrderSnapshot(Base):
    """Raw exchange order fact captured during an order reconciliation run."""

    __tablename__ = "exchange_order_snapshots"

    run_id = Column(
        String,
        ForeignKey("order_reconciliation_runs.run_id"),
        primary_key=True,
    )
    exchange_order_id = Column(String, primary_key=True, index=True)
    source = Column(String, nullable=False)
    status = Column(String, nullable=True)
    payload = Column(JSON, nullable=False)
    captured_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AccountingAuditRun(Base):
    """Durable result of an offline/local accounting-integrity pass."""

    __tablename__ = "accounting_audit_runs"

    run_id = Column(String, primary_key=True)
    status = Column(String, nullable=False, default="STARTED")
    inventory_count = Column(Integer, nullable=False, default=0)
    fill_count = Column(Integer, nullable=False, default=0)
    blocker_count = Column(Integer, nullable=False, default=0)
    summary = Column(JSON, nullable=False, default=dict)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
