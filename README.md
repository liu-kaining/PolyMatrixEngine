# PolyMatrix Engine

Automated high-frequency market making and statistical arbitrage system for Polymarket.

## Features

- **Market Data Gateway:** WebSocket listener with auto-reconnect, syncing orderbooks to Redis.
- **Quoting Engine:** Calculates baseline probabilities, lays out symmetrical grids on buy/sell sides, and dynamically adjusts orders based on spread margins.
- **OMS (Order Management System):** Robust state machine tracking orders through `PENDING` -> `OPEN` / `FAILED` states. Includes circuit breakers to prevent API ratelimiting cascades.
- **Watchdog Risk Monitor:** Daemon process that constantly monitors Delta exposure across markets, equipped with a kill switch to flatten positions and halt trading.

## Requirements

- Docker and Docker Compose
- [Polymarket API Keys](https://docs.polymarket.com/)

## Installation & Running

The entire application runs inside Docker.

1. **Clone the repository:**
   ```bash
   git clone https://github.com/liukaining/PolyMatrixEngine.git
   cd PolyMatrixEngine
   ```

2. **Configure Environment Variables:**
   ```bash
   cp .env.example .env
   # Edit .env and put in your Polymarket PK and FUNDER_ADDRESS.
   ```

3. **Start the Application:**
   Run the following command to build the image and start the FastAPI server, Streamlit Dashboard, PostgreSQL, and Redis in the background:
   ```bash
   docker compose up --build -d
   ```
   
   The database tables will be automatically initialized via `alembic` when the `api` container starts.

To view the application logs:
```bash
docker compose logs -f api
```

## Dashboard & Monitoring

PolyMatrix Engine comes with a built-in real-time monitoring dashboard powered by Streamlit.

Once the containers are running, open your browser and navigate to:
**[http://localhost:8501](http://localhost:8501)**

Features of the dashboard:
- **Control Panel:** Enter a Polymarket `condition_id` and start the Quoting Engine.
- **Inventory & Risk:** Real-time visual bar charts of your `yes_exposure` and `no_exposure` across all markets, including Realized PnL.
- **Active Orders:** View all resting orders (`OPEN` / `PENDING`) currently on the Polymarket CLOB.

## API Usage

**Health Check:**
```bash
curl http://localhost:8000/health
```

**Start Quoting a Market:**
```bash
curl -X POST http://localhost:8000/markets/{condition_id}/start
```

**View Market Risk/Exposure:**
```bash
curl http://localhost:8000/markets/{condition_id}/risk
```

## Disclaimer
This software is provided for educational and experimental purposes. Using it to trade on Polymarket carries significant financial risk. The developers assume no responsibility for any trading losses.
