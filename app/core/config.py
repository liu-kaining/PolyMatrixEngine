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
    MAX_EXPOSURE_PER_MARKET: float = 50.0 # 50 USDC max exposure before kill switch
    EXPOSURE_TOLERANCE: float = 0.01  # Ledger vs API diff above this triggers reconciliation overwrite (e.g. 0.01 so 5.0 vs 4.3 is corrected)
    RECONCILIATION_BUFFER_SECONDS: float = 8.0  # Skip REST overwrite shortly after local fills
    BASE_ORDER_SIZE: float = 10.0         # Default $ size per order (can be overridden via .env)
    GRID_LEVELS: int = 2                  # Default number of grid levels per side
    # Strategy: 少而精 — we only get filled when the market hits our price (below fair value). No chasing fills.
    QUOTE_BASE_SPREAD: float = 0.02       # Edge per fill: BUY at fair_value - spread/2 (wider = more edge, fewer fills)
    QUOTE_PRICE_OFFSET_THRESHOLD: float = 0.01   # Refresh grid when mid moves this much; larger = orders sit longer, more chance to get filled
    # When True, first bid is at most 1 tick below best_bid (more fills, still ~1¢ edge). When False, strictly at bid_1 only.
    QUOTE_BID_ONE_TICK_BELOW_TOUCH: bool = True

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
