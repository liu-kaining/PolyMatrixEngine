import os
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
            df['price'] = pd.to_numeric(df['price'], errors='coerce')
            df['size'] = pd.to_numeric(df['size'], errors='coerce')
        return df
    except Exception as e:
        st.error(f"Error fetching active orders: {e}")
        return pd.DataFrame()

# ----------------- SIDEBAR -----------------
st.sidebar.title("PolyMatrix Engine")
st.sidebar.markdown("### Control Panel")

with st.sidebar.form("start_market_form"):
    st.markdown("**Start Market Making**")
    condition_id = st.text_input("Condition ID", placeholder="0x...")
    submitted = st.form_submit_button("Start Quoting")
    
    if submitted:
        if condition_id:
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
        else:
            st.warning("Please enter a Condition ID.")

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
    st.dataframe(inv_df, use_container_width=True)
else:
    st.info("No inventory data found. Add a market condition ID in the sidebar to start.")

st.markdown("---")

# 2. Active Orders Panel
st.header("📋 Active Orders")
orders_df = fetch_active_orders()

if not orders_df.empty:
    st.metric("Total Active Orders", len(orders_df))
    # Format for display
    st.dataframe(
        orders_df.style.format({
            "price": "{:.4f}",
            "size": "{:.2f}"
        }), 
        use_container_width=True,
        hide_index=True
    )
else:
    st.info("No active orders found (No OPEN or PENDING orders currently resting on the CLOB).")

st.markdown("---")

# 3. System Status
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
