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
    
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres_password@localhost:5432/polymatrix"
    
    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    
    # Alchemy RPC (Kill Switch)
    ALCHEMY_RPC_URL: str = ""
    
    # Trading params
    MAX_EXPOSURE_PER_MARKET: float = 50.0 # 50 USDC max exposure before kill switch
    BASE_ORDER_SIZE: float = 10.0         # Default $ size per order (can be overridden via .env)
    GRID_LEVELS: int = 2                  # Default number of grid levels per side
    # Strategy: 少而精 — we only get filled when the market hits our price (below fair value). No chasing fills.
    QUOTE_BASE_SPREAD: float = 0.02       # Edge per fill: BUY at fair_value - spread/2 (wider = more edge, fewer fills)
    QUOTE_PRICE_OFFSET_THRESHOLD: float = 0.005  # Refresh grid when mid moves this much (keep quotes in line with fair value)

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
