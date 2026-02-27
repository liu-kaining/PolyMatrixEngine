import os
import json
from datetime import datetime, timezone, timedelta

import streamlit as st
import pandas as pd
from sqlalchemy import create_engine
import requests

# Set page config
st.set_page_config(page_title="PolyMatrix Engine Dashboard", layout="wide", page_icon="📈")

# Environment variables
# We replace asyncpg with psycopg2 because pandas read_sql uses sync sqlalchemy engine
DB_URL_ASYNC = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres_password@localhost:5432/polymatrix")
DB_URL_SYNC = DB_URL_ASYNC.replace("+asyncpg", "+psycopg2") if "+asyncpg" in DB_URL_ASYNC else DB_URL_ASYNC
API_URL = os.getenv("API_URL", "http://localhost:8000")

@st.cache_resource
def get_engine():
    """Create a synchronous SQLAlchemy engine for Pandas to read from PostgreSQL"""
    return create_engine(DB_URL_SYNC)

@st.cache_data
def resolve_polymarket_link(condition_id: str) -> str:
    """
    Resolve a human-friendly Polymarket frontend URL for a given condition_id
    via Gamma API (uses slug as the path component).
    """
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"condition_ids": condition_id},
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data:
                market = data[0]
                slug = market.get("slug") or market.get("ticker")
                if slug:
                    return f"https://polymarket.com/event/{slug}"
    except Exception:
        # Best-effort enrichment only; dashboard should not crash on failures.
        return ""
    return ""

def fetch_inventory():
    engine = get_engine()
    query = """
        SELECT market_id, yes_exposure, no_exposure, realized_pnl, updated_at 
        FROM inventory_ledger
    """
    try:
        df = pd.read_sql(query, engine)
        # Convert numeric types
        for col in ['yes_exposure', 'no_exposure', 'realized_pnl']:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
        return df
    except Exception as e:
        st.error(f"Error fetching inventory: {e}")
        return pd.DataFrame()

def fetch_active_orders():
    engine = get_engine()
    query = """
        SELECT order_id, market_id, side, price, size, status, created_at 
        FROM orders_journal 
        WHERE status = 'OPEN' OR status = 'PENDING'
        ORDER BY created_at DESC
    """
    try:
        df = pd.read_sql(query, engine)
        if not df.empty:
            df["price"] = pd.to_numeric(df["price"], errors="coerce")
            df["size"] = pd.to_numeric(df["size"], errors="coerce")
            # Normalize created_at to Asia/Shanghai for display
            try:
                df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True)
                df["created_at_local"] = (
                    df["created_at"]
                    .dt.tz_convert("Asia/Shanghai")
                    .dt.strftime("%Y-%m-%d %H:%M:%S")
                )
            except Exception:
                df["created_at_local"] = df["created_at"].astype(str)
        return df
    except Exception as e:
        st.error(f"Error fetching active orders: {e}")
        return pd.DataFrame()

@st.cache_data
def resolve_condition_id(market_input: str) -> str | None:
    """
    Accepts:
    - Raw condition_id (0x...)
    - Polymarket URL (https://polymarket.com/event/<slug>...)
    - Plain slug (will-the-us-confirm-that-aliens-exist-before-2027)
    Returns a condition_id string or None on failure.
    """
    value = (market_input or "").strip()
    if not value:
        return None

    # 1) Direct condition_id
    if value.startswith("0x") and len(value) >= 66:
        return value

    # 2) Extract slug from URL if present
    slug = None
    if "polymarket.com" in value and "/event/" in value:
        try:
            slug = value.split("/event/", 1)[1].split("?", 1)[0].strip("/")
        except Exception:
            slug = None

    # 3) If still no slug and not 0x, treat whole input as slug
    if not slug and not value.startswith("0x"):
        slug = value

    if not slug:
        return None

    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug},
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data:
                return data[0].get("conditionId")
    except Exception:
        return None
    return None

# ----------------- SIDEBAR -----------------
st.sidebar.title("PolyMatrix Engine")
st.sidebar.markdown("### Control Panel")

with st.sidebar.form("start_market_form"):
    st.markdown("**Start Market Making**")
    market_input = st.text_input(
        "Condition ID or Polymarket URL",
        placeholder="0x... or https://polymarket.com/event/...",
    )
    confirm_live = st.checkbox("I understand this may place real orders with current config")
    submitted = st.form_submit_button("Start Quoting")
    
    if submitted:
        if not market_input:
            st.warning("Please enter a Condition ID or Polymarket URL.")
        elif not confirm_live:
            st.warning("Please check the confirmation box before starting market making.")
        else:
            condition_id = resolve_condition_id(market_input)
            if not condition_id:
                st.error("Could not resolve a valid condition_id from the input. Please check the URL or ID.")
            else:
                try:
                    with st.spinner("Initializing Quoting Engine..."):
                        response = requests.post(f"{API_URL}/markets/{condition_id}/start")
                        if response.status_code == 200:
                            data = response.json()
                            st.success(f"Started quoting for {condition_id[:8]}...")
                            st.json(data)
                            st.rerun()
                        else:
                            st.error(f"Failed: {response.text}")
                except Exception as e:
                    st.error(f"API Connection Error: {e}")

st.sidebar.markdown("---")
st.sidebar.markdown("### Emergency Controls")

kill_condition_id = st.sidebar.text_input("Target Condition ID", placeholder="0x...", key="kill_input")

col_stop, col_liq = st.sidebar.columns(2)

with col_stop:
    if st.button("🛑 Stop", help="Soft Cancel all orders and suspend engine for this market"):
        if kill_condition_id:
            try:
                res = requests.post(f"{API_URL}/markets/{kill_condition_id}/stop")
                if res.status_code == 200:
                    st.success("Stopped")
                    st.rerun()
                else:
                    st.error(res.text)
            except Exception as e:
                st.error(f"API Error: {e}")
        else:
            st.warning("Enter ID")

with col_liq:
    if st.button("☢️ Liquidate All", help="Cancel orders and Market Dump to clear exposure"):
        if kill_condition_id:
            try:
                res = requests.post(f"{API_URL}/markets/{kill_condition_id}/liquidate")
                if res.status_code == 200:
                    st.success("Liquidating")
                    st.rerun()
                else:
                    st.error(res.text)
            except Exception as e:
                st.error(f"API Error: {e}")
        else:
            st.warning("Enter ID")

st.sidebar.markdown("---")

# Danger Zone: Wipe all local data
st.sidebar.markdown("### Danger Zone")
with st.sidebar.form("wipe_form"):
    st.markdown("**Wipe ALL local data (DB + Redis)**")
    wipe_confirm = st.text_input("Type `WIPE` to confirm", value="")
    wipe_submitted = st.form_submit_button("🔥 Wipe All Data")

    if wipe_submitted:
        if wipe_confirm.strip() != "WIPE":
            st.warning("Please type `WIPE` exactly to confirm.")
        else:
            try:
                res = requests.post(f"{API_URL}/admin/wipe")
                if res.status_code == 200:
                    st.success("All local data wiped. Please restart any running strategies if needed.")
                    st.rerun()
                else:
                    st.error(res.text)
            except Exception as e:
                st.error(f"API Error: {e}")

st.sidebar.markdown("---")
if st.sidebar.button("Refresh Data", use_container_width=True):
    st.rerun()

# ----------------- MAIN PAGE -----------------
st.title("📈 PolyMatrix Engine Dashboard")

# 1. Inventory & Risk Panel
st.header("🛡️ Inventory & Risk")
inv_df = fetch_inventory()

if not inv_df.empty:
    # Display top-level metrics
    total_pnl = inv_df['realized_pnl'].sum()
    total_markets = len(inv_df)
    
    col1, col2 = st.columns(2)
    col1.metric("Active Markets", total_markets)
    col2.metric("Total Realized PnL (USDC)", f"${total_pnl:.4f}")
    
    # Plot Exposure Chart
    st.subheader("Market Exposures (USDC)")
    plot_df = inv_df[['market_id', 'yes_exposure', 'no_exposure']].copy()
    plot_df.set_index('market_id', inplace=True)
    st.bar_chart(plot_df, height=300)
    
    # Raw Data Table
    st.subheader("Inventory Ledger")
    # Enrich with external links for better drill-down
    inv_display_df = inv_df.copy()
    # Reorder for nicer display
    inv_display_df = inv_display_df[
        ["market_id", "yes_exposure", "no_exposure", "realized_pnl", "updated_at"]
    ]
    inv_display_df["gamma_link"] = inv_display_df["market_id"].apply(
        lambda cid: f"https://gamma-api.polymarket.com/markets?condition_ids={cid}"
    )
    inv_display_df["polymarket_link"] = inv_display_df["market_id"].apply(
        resolve_polymarket_link
    )
    st.dataframe(
        inv_display_df,
        column_config={
            "market_id": st.column_config.TextColumn("Market ID"),
            "yes_exposure": st.column_config.NumberColumn("YES Exposure", format="%.4f"),
            "no_exposure": st.column_config.NumberColumn("NO Exposure", format="%.4f"),
            "realized_pnl": st.column_config.NumberColumn("Realized PnL", format="%.4f"),
            "gamma_link": st.column_config.LinkColumn(
                "Gamma",
                # Show condition_id extracted from the URL query
                display_text=r"condition_ids=(.*)$",
            ),
            "polymarket_link": st.column_config.LinkColumn(
                "Polymarket",
                # Show the slug part from the event URL
                display_text=r"/event/(.*)$",
            ),
        },
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("No inventory data found. Add a market condition ID in the sidebar to start.")

st.markdown("---")

# 2. Market Screener (Gamma)
st.header("🧭 Market Screener (Gamma)")

mode_col, _ = st.columns([1, 3])
with mode_col:
    screener_mode = st.selectbox(
        "Screener mode",
        options=["Conservative", "Normal", "Aggressive"],
        index=0,
        help="Conservative = strict filters; Aggressive = looser filters showing more markets.",
    )

col_load, _ = st.columns([1, 3])
with col_load:
    if st.button("Load & Screen Markets", use_container_width=True):
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 200,
                },
                timeout=5,
            )
            if resp.status_code != 200:
                st.error(f"Failed to load markets from Gamma: {resp.text}")
            else:
                raw_markets = resp.json()
                screened = []
                now = datetime.now(timezone.utc)

                # Configure thresholds based on screener mode
                if screener_mode == "Conservative":
                    min_dte = timedelta(days=7)
                    min_vol = 50_000.0
                    min_liq = 10_000.0
                    price_low, price_high = 0.25, 0.75
                elif screener_mode == "Normal":
                    min_dte = timedelta(days=5)
                    min_vol = 20_000.0
                    min_liq = 5_000.0
                    price_low, price_high = 0.20, 0.80
                else:  # Aggressive
                    min_dte = timedelta(days=3)
                    min_vol = 5_000.0
                    min_liq = 2_000.0
                    price_low, price_high = 0.15, 0.85

                for m in raw_markets:
                    # 1) Binary ONLY: outcomes == ["Yes", "No"] (case-insensitive)
                    outcomes_raw = m.get("outcomes")
                    outcomes = []
                    if outcomes_raw:
                        if isinstance(outcomes_raw, str):
                            try:
                                outcomes = json.loads(outcomes_raw)
                            except Exception:
                                outcomes = []
                        elif isinstance(outcomes_raw, list):
                            outcomes = outcomes_raw
                    outcomes_lower = {str(o).strip().lower() for o in outcomes}
                    if outcomes_lower != {"yes", "no"}:
                        continue

                    # 2) DTE >= 7 days
                    end_raw = m.get("endDate")
                    try:
                        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    if end_dt - now < min_dte:
                        continue

                    # 3) Volume & Liquidity thresholds
                    try:
                        vol_24h = float(m.get("volume24hr") or 0.0)
                        liq = float(m.get("liquidityNum") or 0.0)
                    except Exception:
                        continue
                    if vol_24h <= min_vol or liq <= min_liq:
                        continue

                    # 4) Goldilocks YES price band, if prices are present
                    yes_price = None
                    prices_raw = m.get("outcomePrices")
                    if prices_raw:
                        if isinstance(prices_raw, str):
                            try:
                                prices = json.loads(prices_raw)
                            except Exception:
                                prices = []
                        elif isinstance(prices_raw, list):
                            prices = prices_raw
                        else:
                            prices = []
                        if prices:
                            try:
                                yes_price = float(prices[0])
                            except Exception:
                                yes_price = None
                    if yes_price is not None and not (price_low <= yes_price <= price_high):
                        continue

                    screened.append(
                        {
                            "question": m.get("question", ""),
                            "yes_price": yes_price,
                            "volume24hr": vol_24h,
                            "liquidity": liq,
                            "end_date": end_dt,
                            "condition_id": m.get("conditionId"),
                            "slug": m.get("slug"),
                        }
                    )

                # Sort by 24h volume descending
                screened.sort(key=lambda x: x["volume24hr"], reverse=True)
                st.session_state["screener_markets"] = screened
        except Exception as e:
            st.error(f"Gamma API error: {e}")

screened_markets = st.session_state.get("screener_markets", [])
if screened_markets:
    # Structured table view
    df_screen = pd.DataFrame(
        [
            {
                "Question": m["question"],
                "YES Price": m["yes_price"],
                "Volume 24h": m["volume24hr"],
                "Liquidity": m["liquidity"],
                "End Date": m["end_date"].strftime("%Y-%m-%d"),
                "Condition ID": m["condition_id"],
                "Slug": m["slug"],
            }
            for m in screened_markets
        ]
    )

    st.dataframe(
        df_screen[["Question", "YES Price", "Volume 24h", "Liquidity", "End Date", "Condition ID"]],
        use_container_width=True,
        hide_index=True,
        column_config={
            "YES Price": st.column_config.NumberColumn("YES Price", format="%.3f"),
            "Volume 24h": st.column_config.NumberColumn("Volume 24h", format="%.0f"),
            "Liquidity": st.column_config.NumberColumn("Liquidity", format="%.0f"),
        },
    )

    st.markdown("#### Launch Quoting from Screener")
    for m in screened_markets:
        cid = m["condition_id"]
        cols = st.columns([6, 1])
        with cols[0]:
            st.markdown(f"**{m['question']}**")
            st.markdown(
                f"- YES Price: `{m['yes_price'] if m['yes_price'] is not None else 'N/A'}`  "
                f"- 24h Volume: `${m['volume24hr']:.0f}`  "
                f"- Liquidity: `${m['liquidity']:.0f}`  "
                f"- End Date: `{m['end_date'].strftime('%Y-%m-%d')}`"
            )
        with cols[1]:
            if st.button(
                "✅ Start",
                key=f"screener_start_{cid}",
                help="Use current .env config to start quoting this market",
            ):
                if not cid:
                    st.error("Missing conditionId for this market.")
                else:
                    try:
                        with st.spinner("Starting quoting for selected market..."):
                            res = requests.post(f"{API_URL}/markets/{cid}/start")
                            if res.status_code == 200:
                                st.success(f"Started quoting for {cid[:8]}...")
                                st.json(res.json())
                                st.rerun()
                            else:
                                st.error(res.text)
                    except Exception as e:
                        st.error(f"API Error: {e}")
else:
    st.info("Click 'Load & Screen Markets' to fetch binary, liquid, medium-term markets from Gamma.")

st.markdown("---")

# 3. Active Orders Panel
st.header("📋 Active Orders")
orders_df = fetch_active_orders()

if not orders_df.empty:
    st.metric("Total Active Orders", len(orders_df))
    # Nicer display: select and format key columns
    orders_display = orders_df[
        ["order_id", "market_id", "side", "price", "size", "status", "created_at_local"]
    ].rename(columns={"created_at_local": "created_at (Asia/Shanghai)"})
    st.dataframe(
        orders_display,
        column_config={
            "price": st.column_config.NumberColumn("price", format="%.4f"),
            "size": st.column_config.NumberColumn("size", format="%.2f"),
        },
        use_container_width=True,
        hide_index=True
    )
else:
    st.info("No active orders found (No OPEN or PENDING orders currently resting on the CLOB).")

st.markdown("---")

# 4. System Status
st.header("⚙️ System Status")
st.markdown("API & Watchdog Health")

try:
    health = requests.get(f"{API_URL}/health", timeout=2)
    if health.status_code == 200:
        st.success("FastAPI Backend: **ONLINE**")
        st.json(health.json())
    else:
        st.warning("FastAPI Backend: **UNKNOWN STATUS**")
except Exception as e:
    st.error("FastAPI Backend: **OFFLINE** (Is the API container running?)")
    
st.caption("Tip: Use `docker compose logs -f api` to view real-time tick execution and QuotingEngine algorithmic logs.")
