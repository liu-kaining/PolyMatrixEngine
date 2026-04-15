from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # App
    PROJECT_NAME: str = "PolyMatrix Engine"
    DEBUG: bool = False
    LIVE_TRADING_ENABLED: bool = False  # Set to True to allow actual real money order placements
    
    # Polymarket API
    PM_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    PM_API_URL: str = "https://clob.polymarket.com"
    PM_CHAIN_ID: int = 137 # Polygon
    
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
    AUTO_TUNE_FOR_REWARDS: bool = True    # 开启全自动赏金猎人模式
    MAX_EXPOSURE_PER_MARKET: float = 40.0 # Per-market cap (USDC). Lowered for capital preservation.
    # Categorical / multi-outcome markets (>2 CLOB tokens): stricter per-condition ceiling (MTM + pending BUY path)
    MAX_EXPOSURE_CATEGORICAL: float = 30.0
    GLOBAL_MAX_BUDGET: float = 280.0      # 绝对的资金红线 — 留 $20 安全垫
    EXPOSURE_TOLERANCE: float = 0.01  # Ledger vs API diff above this triggers reconciliation overwrite (e.g. 0.01 so 5.0 vs 4.3 is corrected)
    RECONCILIATION_BUFFER_SECONDS: float = 8.0  # Skip REST overwrite shortly after local fills
    RECONCILIATION_INTERVAL_SEC: int = 300  # Periodic REST positions sync (watchdog); 5min for small capital safety
    # V6.4: Periodic Hard Reset — wallet-wide CLOB cancel_all + settle sleep before local cleanup
    HARD_RESET_CLOB_CANCEL_ALL_ENABLED: bool = True
    HARD_RESET_CLOB_CANCEL_ALL_SLEEP_SEC: float = 3.0  # Let matching engine release USDC before new quotes
    HARD_RESET_CLOB_CANCEL_ALL_TIMEOUT_SEC: float = 45.0
    HARD_RESET_CLOB_BALANCE_FETCH_TIMEOUT_SEC: float = 20.0
    # Skip wallet-level cancel_all if another engine just ran it (two engines per market).
    HARD_RESET_CLOB_WALLET_DEDUP_SEC: float = 15.0
    BASE_ORDER_SIZE: float = 10.0         # Default order size in OUTCOME SHARES (not USDC); min 5 per CLOB
    GRID_LEVELS: int = 2                  # Default number of grid levels per side
    QUOTE_BASE_SPREAD: float = 0.02       # 兜底（Fallback）默认值
    QUOTE_PRICE_OFFSET_THRESHOLD: float = 0.01   # Refresh grid when mid moves this much; larger = orders sit longer, more chance to get filled
    # When True, first bid is at most 1 tick below best_bid (more fills, still ~1¢ edge). When False, strictly at bid_1 only.
    QUOTE_BID_ONE_TICK_BELOW_TOUCH: bool = True

    # Auto-Router (V4.0): global scheduling / portfolio manager
    AUTO_ROUTER_ENABLED: bool = False
    AUTO_ROUTER_MAX_MARKETS: int = 8           # More markets for diversification at smaller size
    AUTO_ROUTER_SCAN_INTERVAL_SEC: int = 3600
    AUTO_ROUTER_MIN_HOLD_HOURS: float = 2.0    # Fast rotation — exit bad markets quickly
    # V7.0 Auto-Router: minimum daily reward pool (USD); markets below are skipped entirely
    AUTO_ROUTER_MIN_REWARD_POOL: float = 50.0

    # V7.1 Official Builder API credentials for Polymarket order attribution
    POLY_BUILDER_API_KEY: str = ""
    POLY_BUILDER_SECRET: str = ""
    POLY_BUILDER_PASSPHRASE: str = ""

    # V6.2 Sector & Event Horizon risk controls
    MAX_EXPOSURE_PER_SECTOR: float = 300.0   # Max USD per category/tag (prevent over-concentration)
    EVENT_HORIZON_HOURS: float = 72.0        # Markets resolving within 72h → avoid (was 24h, too late)
    MAX_SLOTS_PER_SECTOR: int = 2            # Max active markets per tag (simpler sector cap)

    # V8.0 Conservative Strategy: Single-sided cheap-side quoting
    SINGLE_SIDE_CHEAP_ONLY: bool = True      # Only BUY on the cheap side (price < CHEAP_SIDE_MAX_PRICE)
    CHEAP_SIDE_MAX_PRICE: float = 0.45       # Token FV must be below this to place BUY orders
    HEDGE_ON_FILL: bool = True               # Immediately place SELL hedge after BUY fill
    HEDGE_MARGIN_CENTS: float = 0.02         # Hedge SELL price = fill_price + this
    HEDGE_DECAY_TICKS: int = 10              # After N ticks, start lowering hedge price toward breakeven
    PER_MARKET_STOP_LOSS_USD: float = 5.0    # Per-market unrealized loss threshold → graceful_exit

    # V8.0 Auto-Router market quality filters
    ROUTER_MIN_BOOK_DEPTH_USD: float = 50.0  # Minimum orderbook depth (top-3 bid+ask notional)
    ROUTER_MAX_BOOK_SPREAD: float = 0.08     # Max bid-ask spread to accept a market
    ROUTER_AVOID_MIDPOINT_BAND: float = 0.10 # Skip markets with midpoint in [0.40, 0.60]

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
