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

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
