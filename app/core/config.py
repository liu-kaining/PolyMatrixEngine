from typing import Literal

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # App
    PROJECT_NAME: str = "PolyMatrix Engine"
    DEBUG: bool = False
    # Safety interlock: the legacy boolean can no longer enable real orders by itself.
    # Real execution requires TRADING_MODE=live plus the short-lived arm controls below.
    TRADING_MODE: Literal["disabled", "paper", "live"] = "disabled"
    LIVE_TRADING_ENABLED: bool = False  # Deprecated secondary confirmation; never sufficient alone.
    LIVE_ARM_TOKEN: str = ""
    LIVE_ARM_EXPIRES_AT: str = ""  # ISO-8601; must be in the next 24 hours.
    LIVE_ALLOWED_FUNDER_ADDRESSES: str = ""  # Comma-separated wallet allow-list.
    LIVE_BUDGET_CAP_USD: float = 100.0  # Explicit ceiling for GLOBAL_MAX_BUDGET in live mode.
    # Removed controls retained only so an older .env fails at the safety gate instead of import.
    AUTO_ROUTER_LIVE_ARMED: bool = False  # Ignored; reward-ranked router is always paper-only.
    # Deprecated compatibility field. Fee accounting is now enforced in code from
    # the pinned SDK's role/rate facts and the documented five-decimal USDC rule.
    LIVE_FEE_ACCOUNTING_VALIDATED: bool = False
    APP_CODE_COMMIT: str = ""  # Full build commit; must match alpha evidence in live mode.

    # Mutating control-plane endpoints are disabled unless a strong bearer token is configured.
    ADMIN_API_TOKEN: str = ""
    ENABLE_ADMIN_WIPE: bool = False
    
    # Polymarket API
    PM_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    PM_API_URL: str = "https://clob.polymarket.com"
    PM_CHAIN_ID: int = 137 # Polygon
    MARKET_DATA_MAX_AGE_SEC: float = 5.0
    MARKET_DATA_MAX_FUTURE_SKEW_SEC: float = 2.0
    # The documented CLOB market stream has timestamps/hashes but no sequence.
    # Enabling sequence is supported for future contracts; current safety relies
    # on snapshot hashes, strict timestamps and periodic authoritative REST resync.
    MARKET_DATA_REQUIRE_SEQUENCE_LIVE: bool = False
    MARKET_DATA_REQUIRE_EXCHANGE_TIMESTAMP_LIVE: bool = True
    MARKET_DATA_REQUIRE_SNAPSHOT_ID_LIVE: bool = True
    MARKET_DATA_REST_RESYNC_SEC: float = 30.0
    EXECUTION_LEASE_TTL_SEC: float = 15.0
    GEOBLOCK_URL: str = "https://polymarket.com/api/geoblock"
    GEOBLOCK_RECHECK_SEC: float = 300.0
    ORDER_RECONCILIATION_INTERVAL_SEC: float = 60.0
    
    # Credentials (Load from .env)
    PK: str = ""
    FUNDER_ADDRESS: str = ""
    
    # Database (DB_PORT is for docker-compose host mapping only; app uses DATABASE_URL)
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres_password@localhost:5432/polymatrix"
    DB_PORT: str = "5433"

    # Redis (REDIS_PORT is for docker-compose host mapping only; app uses REDIS_URL)
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_PORT: str = "6380"
    
    # Alchemy RPC (Kill Switch)
    ALCHEMY_RPC_URL: str = ""
    
    # Trading params
    AUTO_TUNE_FOR_REWARDS: bool = False  # Ignored by execution; True blocks live as stale config.
    OFFLINE_VALIDATED_ALPHA_ENABLED: bool = False
    ALPHA_STRATEGY_ID: str = "maker-alpha-v2"
    ALPHA_VALIDATION_REPORT_PATH: str = ""
    ALPHA_VALIDATION_REPORT_SHA256: str = ""
    ALPHA_EVIDENCE_MIN_FILLS: int = 1000
    ALPHA_EVIDENCE_MIN_MARKETS: int = 20
    ALPHA_EVIDENCE_MIN_DATASET_DAYS: float = 30.0
    ALPHA_EVIDENCE_MAX_AGE_DAYS: float = 30.0
    ALPHA_EVIDENCE_MAX_DRAWDOWN_FRACTION: float = 0.25
    MIN_EXPECTED_NET_EDGE: float = 0.02
    EXECUTION_COST_BUFFER: float = 0.002
    ADVERSE_SELECTION_BUFFER: float = 0.01
    MAX_EXPOSURE_PER_MARKET: float = 40.0  # Conservative per-market USDC ceiling.
    # Categorical / multi-outcome markets (>2 CLOB tokens): stricter per-condition ceiling (MTM + pending BUY path)
    MAX_EXPOSURE_CATEGORICAL: float = 30.0
    GLOBAL_MAX_BUDGET: float = 280.0  # Conservative wallet-wide USDC ceiling.
    EXPOSURE_TOLERANCE: float = 0.01  # Ledger vs API diff above this triggers reconciliation overwrite (e.g. 0.01 so 5.0 vs 4.3 is corrected)
    RECONCILIATION_BUFFER_SECONDS: float = 8.0  # Skip REST overwrite shortly after local fills
    RECONCILIATION_INTERVAL_SEC: int = 300  # Periodic comparison; errors remain fail-closed.
    BASE_ORDER_SIZE: float = 10.0         # Default order size in OUTCOME SHARES (not USDC); min 5 per CLOB
    GRID_LEVELS: int = 2                  # Default number of grid levels per side
    QUOTE_BASE_SPREAD: float = 0.02       # 兜底（Fallback）默认值
    QUOTE_PRICE_OFFSET_THRESHOLD: float = 0.01   # Refresh grid when mid moves this much; larger = orders sit longer, more chance to get filled
    ALPHA_BOOK_DEPTH_LEVELS: int = 3
    ALPHA_BOOK_DEPTH_DECAY: float = 0.65
    ALPHA_MAX_BINARY_PARITY_ERROR: float = 0.03
    ALPHA_MAX_PAIR_SKEW_SEC: float = 2.0
    ALPHA_MAX_INVENTORY_SKEW: float = 0.02
    ALPHA_VOLATILITY_EWMA_ALPHA: float = 0.20
    ALPHA_MAX_TICK_MOVE: float = 0.05
    ALPHA_MAX_EWMA_ABS_MOVE: float = 0.02
    ALPHA_VOLATILITY_COOLDOWN_SEC: float = 5.0
    ALPHA_VOLATILITY_SPREAD_MULTIPLIER: float = 2.0
    EXIT_MAX_BOOK_IMPACT: float = 0.02
    EXIT_MAX_REALIZED_LOSS_FRACTION: float = 0.10
    # When True, first bid is at most 1 tick below best_bid (more fills, still ~1¢ edge). When False, strictly at bid_1 only.
    QUOTE_BID_ONE_TICK_BELOW_TOUCH: bool = True
    PAPER_MAKER_PARTICIPATION_RATE: float = 0.25
    PAPER_TAKER_FEE_RATE: float = 0.25

    # Reward-ranked research router. It is hard-blocked in live mode.
    AUTO_ROUTER_ENABLED: bool = False
    AUTO_ROUTER_MAX_MARKETS: int = 8
    AUTO_ROUTER_SCAN_INTERVAL_SEC: int = 3600
    AUTO_ROUTER_MIN_HOLD_HOURS: float = 2.0
    # V7.0 Auto-Router: minimum daily reward pool (USD); markets below are skipped entirely
    AUTO_ROUTER_MIN_REWARD_POOL: float = 50.0

    # Unified SDK attribution uses a public builder code per order. The legacy
    # local builder credential triplet is retained only to detect stale .env files.
    POLY_BUILDER_CODE: str = ""
    POLY_BUILDER_API_KEY: str = ""
    POLY_BUILDER_SECRET: str = ""
    POLY_BUILDER_PASSPHRASE: str = ""

    # V6.2 Sector & Event Horizon risk controls
    MAX_EXPOSURE_PER_SECTOR: float = 300.0   # Max USD per category/tag (prevent over-concentration)
    EVENT_HORIZON_HOURS: float = 72.0
    MAX_SLOTS_PER_SECTOR: int = 2            # Max active markets per tag (simpler sector cap)

    # Paper-only Router book-quality research filters. Missing/invalid books reject
    # candidates; the reward-ranked Router remains unavailable in live mode.
    ROUTER_MIN_BOOK_DEPTH_USD: float = 50.0
    ROUTER_MAX_BOOK_SPREAD: float = 0.08
    ROUTER_AVOID_MIDPOINT_BAND: float = 0.10

    # Removed V8 execution controls are accepted only for old .env compatibility.
    # Runtime execution ignores them; enabling either flag is also a live blocker.
    SINGLE_SIDE_CHEAP_ONLY: bool = False
    CHEAP_SIDE_MAX_PRICE: float = 0.45
    HEDGE_ON_FILL: bool = False
    HEDGE_MARGIN_CENTS: float = 0.02
    HEDGE_DECAY_TICKS: int = 10
    PER_MARKET_STOP_LOSS_USD: float = 0.0

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
