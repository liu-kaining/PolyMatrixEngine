from sqlalchemy import Column, String, Integer, Numeric, JSON, DateTime, Enum, ForeignKey
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

class InventoryLedger(Base):
    __tablename__ = "inventory_ledger"

    market_id = Column(String, ForeignKey("markets_meta.condition_id"), primary_key=True)
    yes_exposure = Column(Numeric(20, 4), default=0)
    no_exposure = Column(Numeric(20, 4), default=0)
    realized_pnl = Column(Numeric(20, 4), default=0)

    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    market = relationship("MarketMeta", back_populates="inventory")
