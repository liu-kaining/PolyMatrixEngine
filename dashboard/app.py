import os
import json
from datetime import datetime, timezone, timedelta

import streamlit as st
import pandas as pd
from sqlalchemy import create_engine
import requests

# Set page config
st.set_page_config(page_title="PolyMatrix Engine Dashboard", layout="wide", page_icon="📈")

# Hide Streamlit's default deploy button / main menu which are not used in this app
st.markdown(
    """
    <style>
    /* Newer Streamlit versions */
    .stAppDeployButton {visibility: hidden !important;}
    /* Older Streamlit versions */
    .stDeployButton {visibility: hidden !important;}
    /* Hide the default hamburger main menu if present */
    #MainMenu {visibility: hidden !important;}
    </style>
    """,
    unsafe_allow_html=True,
)

# Environment variables
# We replace asyncpg with psycopg2 because pandas read_sql uses sync sqlalchemy engine
DB_URL_ASYNC = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres_password@localhost:5432/polymatrix",
)
DB_URL_SYNC = (
    DB_URL_ASYNC.replace("+asyncpg", "+psycopg2")
    if "+asyncpg" in DB_URL_ASYNC
    else DB_URL_ASYNC
)
API_URL = os.getenv("API_URL", "http://localhost:8000")

# Path to backend trading log (can be overridden by TRADING_LOG_PATH)
LOG_FILE_PATH = os.getenv(
    "TRADING_LOG_PATH",
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "data",
        "logs",
        "trading.log",
    ),
)

# Session state for two-step confirmations
if "pending_start_condition_id" not in st.session_state:
    st.session_state["pending_start_condition_id"] = None
if "pending_screener_start_cid" not in st.session_state:
    st.session_state["pending_screener_start_cid"] = None
if "pending_screener_question" not in st.session_state:
    st.session_state["pending_screener_question"] = ""
if "pending_kill_action" not in st.session_state:
    st.session_state["pending_kill_action"] = None
if "pending_kill_condition_id" not in st.session_state:
    st.session_state["pending_kill_condition_id"] = None
if "screener_selected_idx" not in st.session_state:
    st.session_state["screener_selected_idx"] = 0

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


def tail_logs(filepath: str, lines: int = 500) -> str:
    """
    Efficiently read the last N lines from a potentially large log file.
    Falls back to full read if file is small.
    """
    if not os.path.exists(filepath):
        return "Log file not found."

    try:
        # Basic, robust implementation: read from end in chunks
        # to avoid loading very large files fully into memory.
        line_separator = b"\n"
        chunk_size = 8192
        buffer = b""
        line_count = 0

        with open(filepath, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            position = file_size

            while position > 0 and line_count <= lines:
                read_size = min(chunk_size, position)
                position -= read_size
                f.seek(position)
                data = f.read(read_size)
                buffer = data + buffer
                line_count = buffer.count(line_separator)

        # Decode and slice the last N lines
        text = buffer.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        return "\n".join(all_lines[-lines:])
    except Exception as e:
        return f"Error reading log file: {e}"

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
                # Defer actual API call to a second explicit confirmation step
                st.session_state["pending_start_condition_id"] = condition_id

# Sidebar confirmation for starting market making
pending_cid = st.session_state.get("pending_start_condition_id")
if pending_cid:
    st.sidebar.warning(
        f"Confirm starting quoting strategy for market {pending_cid[:8]}... "
        "This may place real orders with current config."
    )
    c_col1, c_col2 = st.sidebar.columns(2)
    with c_col1:
        if st.button("✅ Confirm Start", key="confirm_start_sidebar"):
            try:
                with st.spinner("Initializing Quoting Engine..."):
                    response = requests.post(f"{API_URL}/markets/{pending_cid}/start")
                if response.status_code == 200:
                    data = response.json()
                    st.success(f"Started quoting for {pending_cid[:8]}...")
                    st.json(data)
                    st.session_state["pending_start_condition_id"] = None
                    st.rerun()
                else:
                    st.error(f"Failed: {response.text}")
            except Exception as e:
                st.error(f"API Connection Error: {e}")
                st.session_state["pending_start_condition_id"] = None
    with c_col2:
        if st.button("Cancel", key="cancel_start_sidebar"):
            st.session_state["pending_start_condition_id"] = None

st.sidebar.markdown("---")
st.sidebar.markdown("### Emergency Controls")

kill_condition_id = st.sidebar.text_input("Target Condition ID", placeholder="0x...", key="kill_input")

col_stop, col_liq = st.sidebar.columns(2)

with col_stop:
    if st.button("🛑 Stop", help="Soft Cancel all orders and suspend engine for this market"):
        if kill_condition_id:
            st.session_state["pending_kill_action"] = "stop"
            st.session_state["pending_kill_condition_id"] = kill_condition_id
        else:
            st.warning("Enter ID")

with col_liq:
    if st.button("☢️ Liquidate All", help="Cancel orders and Market Dump to clear exposure"):
        if kill_condition_id:
            st.session_state["pending_kill_action"] = "liquidate"
            st.session_state["pending_kill_condition_id"] = kill_condition_id
        else:
            st.warning("Enter ID")

# Sidebar confirmation for Stop / Liquidate
pending_kill_action = st.session_state.get("pending_kill_action")
pending_kill_cid = st.session_state.get("pending_kill_condition_id")
if pending_kill_action and pending_kill_cid:
    label = "Stop" if pending_kill_action == "stop" else "Liquidate All"
    st.sidebar.warning(
        f"Confirm {label} for market {pending_kill_cid[:8]}... "
        "This will affect live strategy state."
    )
    k_col1, k_col2 = st.sidebar.columns(2)
    with k_col1:
        if st.button(f"✅ Confirm {label}", key="confirm_kill_action"):
            endpoint = "stop" if pending_kill_action == "stop" else "liquidate"
            try:
                res = requests.post(f"{API_URL}/markets/{pending_kill_cid}/{endpoint}")
                if res.status_code == 200:
                    st.success(f"{label} executed")
                    st.session_state["pending_kill_action"] = None
                    st.session_state["pending_kill_condition_id"] = None
                    st.rerun()
                else:
                    st.error(res.text)
                    st.session_state["pending_kill_action"] = None
                    st.session_state["pending_kill_condition_id"] = None
            except Exception as e:
                st.error(f"API Error: {e}")
                st.session_state["pending_kill_action"] = None
                st.session_state["pending_kill_condition_id"] = None
    with k_col2:
        if st.button("Cancel", key="cancel_kill_action"):
            st.session_state["pending_kill_action"] = None
            st.session_state["pending_kill_condition_id"] = None

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
    # Display top-level metrics in a compact card layout
    total_pnl = inv_df["realized_pnl"].sum()
    total_markets = len(inv_df)
    gross_exposure = float(
        inv_df["yes_exposure"].abs().sum() + inv_df["no_exposure"].abs().sum()
    )

    m_col1, m_col2, m_col3 = st.columns(3)
    m_col1.metric("Active Markets", total_markets)
    m_col2.metric("Total Realized PnL (USDC)", f"${total_pnl:.4f}")
    m_col3.metric("Total Gross Exposure (YES+NO)", f"{gross_exposure:.4f}")

    # Optional exposure chart in a collapsible panel to avoid large empty space
    with st.expander("Market Exposures (USDC)", expanded=gross_exposure > 0):
        if total_markets > 1 or gross_exposure > 0:
            plot_df = inv_df[["market_id", "yes_exposure", "no_exposure"]].copy()
            plot_df.set_index("market_id", inplace=True)
            st.bar_chart(plot_df, height=220)
        else:
            st.caption(
                "No meaningful exposure yet. Once the strategy has open positions, "
                "this panel will visualize per-market YES/NO exposure."
            )

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
        options=["Conservative", "Normal", "Aggressive", "Ultra"],
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
                    # Try to approximate full universe rather than a tiny top slice
                    "limit": 500,
                },
                timeout=5,
            )
            if resp.status_code != 200:
                st.error(f"Failed to load markets from Gamma: {resp.text}")
            else:
                raw_markets = resp.json()
                screened = []
                now = datetime.now(timezone.utc)

                # Configure thresholds based on screener mode.
                # Conservative: original strict filters for high-quality liquidity.
                # Normal: relaxed, good daily MM candidates.
                # Aggressive: very loose filters.
                # Ultra: almost full universe (only binary + category filters),
                #        no DTE/volume/liquidity/odds band constraints.
                if screener_mode == "Conservative":
                    min_dte = timedelta(days=7)
                    min_vol = 50_000.0
                    min_liq = 10_000.0
                    price_low, price_high = 0.25, 0.75
                elif screener_mode == "Normal":
                    min_dte = timedelta(days=3)
                    min_vol = 10_000.0
                    min_liq = 3_000.0
                    price_low, price_high = 0.20, 0.80
                elif screener_mode == "Aggressive":
                    min_dte = timedelta(days=1)
                    min_vol = 1_000.0
                    min_liq = 500.0
                    price_low, price_high = 0.10, 0.90
                else:  # Ultra
                    # Placeholders; we will skip these checks entirely for Ultra.
                    min_dte = timedelta(days=0)
                    min_vol = 0.0
                    min_liq = 0.0
                    price_low, price_high = 0.0, 1.0

                # V1.1 Category & Semantic Filtering
                # Hard blacklist for Sports / live event markets
                sports_blacklist = {
                    "sports",
                    "sport",
                    "nfl",
                    "nba",
                    "mlb",
                    "nhl",
                    "soccer",
                    "football",
                    "premier-league",
                    "premier league",
                    "champions-league",
                    "champions league",
                }
                question_blacklist = {
                    "win the match",
                    "wins the match",
                    "to win the match",
                    "halftime",
                    "half-time",
                    "in-play",
                    "in play",
                    "live betting",
                    "live market",
                    "live odds",
                }
                premium_keywords = {
                    "politics",
                    "elections",
                    "election",
                    "culture",
                }
                # Additional semantic keywords to infer category when API tags are missing
                politics_words = {
                    "president",
                    "presidential",
                    "election",
                    "primary",
                    "senate",
                    "governor",
                    "mayor",
                    "parliament",
                    "referendum",
                }
                culture_words = {
                    "oscars",
                    "oscar",
                    "grammy",
                    "emmy",
                    "box office",
                    "movie",
                    "film",
                    "series",
                    "season",
                    "tv show",
                    "album",
                    "song",
                    "music",
                }

                for m in raw_markets:
                    # ---------------------------
                    # 0) Category & semantic pre-filter (hard ban toxic flow)
                    # ---------------------------
                    tags_raw = m.get("tags")
                    tags_list = []
                    if tags_raw:
                        if isinstance(tags_raw, str):
                            # Try JSON first, otherwise treat as comma-separated
                            try:
                                parsed = json.loads(tags_raw)
                                if isinstance(parsed, list):
                                    tags_list = parsed
                                elif isinstance(parsed, str):
                                    tags_list = [parsed]
                            except Exception:
                                tags_list = [
                                    t.strip()
                                    for t in tags_raw.replace(";", ",").split(",")
                                    if t.strip()
                                ]
                        elif isinstance(tags_raw, list):
                            tags_list = tags_raw

                    category_raw = m.get("category") or m.get("subCategory") or ""
                    slug = (m.get("slug") or "").lower()
                    question_text = m.get("question") or ""
                    question_lower = question_text.lower()

                    # Build a text bag for sports blacklist matching
                    tag_text = " ".join(str(t) for t in tags_list)
                    category_haystack = " ".join(
                        [category_raw, tag_text, slug]
                    ).lower()

                    if any(kw in category_haystack for kw in sports_blacklist):
                        # Hard drop sports / leagues / obvious sports categories
                        continue
                    if any(kw in question_lower for kw in question_blacklist):
                        # Hard drop live / in-play style questions
                        continue

                    # Derive a display category and premium flag (Politics / Elections / Culture)
                    cat_source = category_raw.strip() or (
                        tags_list[0].strip() if tags_list else ""
                    )
                    cat_match_text = (
                        (category_raw or "") + " " + tag_text
                    ).lower()
                    is_premium_tag = any(pk in cat_match_text for pk in premium_keywords)
                    # Fallback: detect premium purely from question / slug when tags are empty
                    is_politics_semantic = any(w in question_lower for w in politics_words) or any(
                        w in slug for w in politics_words
                    )
                    is_culture_semantic = any(w in question_lower for w in culture_words) or any(
                        w in slug for w in culture_words
                    )
                    is_premium = is_premium_tag or is_politics_semantic or is_culture_semantic

                    # Choose a human-friendly category label
                    if is_premium:
                        if is_politics_semantic or "politic" in cat_match_text:
                            base_cat = "Politics"
                        elif is_culture_semantic or "culture" in cat_match_text:
                            base_cat = "Culture"
                        else:
                            base_cat = cat_source or "Premium"
                        display_category = f"⭐ {base_cat}"
                    else:
                        # Non-premium: use API category/tag if present, otherwise a generic bucket
                        display_category = cat_source or "General"

                    # ---------------------------
                    # 1) Binary ONLY: outcomes == ["Yes", "No"] (case-insensitive)
                    # ---------------------------
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

                    # 2) DTE, volume, liquidity & odds filters (skipped in Ultra mode)
                    end_raw = m.get("endDate")
                    try:
                        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    try:
                        vol_24h = float(m.get("volume24hr") or 0.0)
                        liq = float(m.get("liquidityNum") or 0.0)
                    except Exception:
                        continue

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

                    if screener_mode != "Ultra":
                        if end_dt - now < min_dte:
                            continue
                        if vol_24h <= min_vol or liq <= min_liq:
                            continue
                        if yes_price is not None and not (price_low <= yes_price <= price_high):
                            continue

                    screened.append(
                        {
                            "question": question_text,
                            "yes_price": yes_price,
                            "volume24hr": vol_24h,
                            "liquidity": liq,
                            "end_date": end_dt,
                            "condition_id": m.get("conditionId"),
                            "slug": m.get("slug"),
                            "category": display_category,
                        }
                    )

                # Sort by 24h volume descending
                screened.sort(key=lambda x: x["volume24hr"], reverse=True)
                st.session_state["screener_markets"] = screened
        except Exception as e:
            st.error(f"Gamma API error: {e}")

screened_markets = st.session_state.get("screener_markets", [])
if screened_markets:
    # Structured table view with interactive selection
    current_idx = st.session_state.get("screener_selected_idx", 0)
    current_idx = max(0, min(current_idx, len(screened_markets) - 1))

    df_screen = pd.DataFrame(
        [
            {
                "Question": m["question"],
                "Category/Tag": m.get("category", ""),
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

    # Add a selectable checkbox column synchronized with dropdown selection
    df_screen["Selected"] = False
    if 0 <= current_idx < len(df_screen):
        df_screen.loc[current_idx, "Selected"] = True

    edited_df = st.data_editor(
        df_screen[
            [
                "Selected",
                "Question",
                "Category/Tag",
                "YES Price",
                "Volume 24h",
                "Liquidity",
                "End Date",
                "Condition ID",
            ]
        ],
        use_container_width=True,
        hide_index=True,
        key="screener_table",
        column_config={
            "Selected": st.column_config.CheckboxColumn(
                "Select",
                help="Click to select this market for quoting.",
            ),
            "Category/Tag": st.column_config.TextColumn("Category/Tag"),
            "YES Price": st.column_config.NumberColumn("YES Price", format="%.3f"),
            "Volume 24h": st.column_config.NumberColumn("Volume 24h", format="%.0f"),
            "Liquidity": st.column_config.NumberColumn("Liquidity", format="%.0f"),
        },
        disabled=[
            "Question",
            "Category/Tag",
            "YES Price",
            "Volume 24h",
            "Liquidity",
            "End Date",
            "Condition ID",
        ],
    )

    # Derive selected index from the checkbox column (table → state)
    selected_rows = edited_df.index[edited_df["Selected"]].tolist()
    if selected_rows:
        st.session_state["screener_selected_idx"] = selected_rows[0]
    else:
        # If user unselects all, fall back to previous selection
        st.session_state["screener_selected_idx"] = current_idx

    st.markdown("#### Launch Quoting from Screener")

    # Nicely formatted "card" preview for the currently selected market
    sel_idx = st.session_state.get("screener_selected_idx", 0)
    sel_idx = max(0, min(sel_idx, len(screened_markets) - 1))
    selected_market = screened_markets[sel_idx]
    yes_p = selected_market["yes_price"]

    st.markdown("##### Selected market")
    card_html = f"""
    <div style="
        border-radius: 8px;
        padding: 12px 16px;
        margin-bottom: 8px;
        background-color: #f5f5fb;
        border: 1px solid #d0d0ea;
    ">
      <div style="font-weight: 600; margin-bottom: 4px;">
        {selected_market['question']}
      </div>
      <div style="font-size: 12px; color: #444;">
        <div>Condition ID: <code>{selected_market['condition_id']}</code></div>
        <div style="margin-top: 2px;">
          YES Price: <b>{yes_p if yes_p is not None else 'N/A'}</b>
          &nbsp;&nbsp; 24h Volume: <b>${selected_market['volume24hr']:.0f}</b>
          &nbsp;&nbsp; Liquidity: <b>${selected_market['liquidity']:.0f}</b>
        </div>
        <div style="margin-top: 2px;">
          End Date: <b>{selected_market['end_date'].strftime('%Y-%m-%d')}</b>
        </div>
      </div>
    </div>
    """
    st.markdown(card_html, unsafe_allow_html=True)

    col_start, col_info = st.columns([1, 3])
    with col_start:
        if st.button(
            "✅ Start from Screener",
            key="start_from_screener",
            help="Prepare to start quoting for the selected market (will show a confirmation box).",
        ):
            m_sel = screened_markets[sel_idx]
            if not m_sel["condition_id"]:
                st.error("Missing conditionId for this market.")
            else:
                st.session_state["pending_screener_start_cid"] = m_sel["condition_id"]
                st.session_state["pending_screener_question"] = m_sel["question"]
    with col_info:
        st.write(f"Total screened markets: **{len(screened_markets)}**")

    # Compact confirmation panel for screener-based starts
    pending_screener_cid = st.session_state.get("pending_screener_start_cid")
    if pending_screener_cid:
        # Re-locate the selected market details from current screened_markets
        m_confirm = next(
            (m for m in screened_markets if m["condition_id"] == pending_screener_cid),
            None,
        )
        if not m_confirm:
            st.warning(
                "Selected market is no longer in the current screener results. Please select again."
            )
            st.session_state["pending_screener_start_cid"] = None
            st.session_state["pending_screener_question"] = ""
        else:
            yes_p = m_confirm["yes_price"]
            st.info("Please confirm starting the quoting strategy for the selected market:")
            st.markdown(f"**{m_confirm['question']}**")
            st.markdown(
                f"- Condition ID: `{m_confirm['condition_id']}`  \n"
                f"- YES Price: `{yes_p if yes_p is not None else 'N/A'}`  \n"
                f"- 24h Volume: `${m_confirm['volume24hr']:.0f}`  \n"
                f"- Liquidity: `${m_confirm['liquidity']:.0f}`  \n"
                f"- End Date: `{m_confirm['end_date'].strftime('%Y-%m-%d')}`"
            )
            c1, c2 = st.columns(2)
            with c1:
                if st.button(
                    "✅ Confirm Screener Start", key="confirm_screener_start_inline"
                ):
                    try:
                        with st.spinner("Starting quoting for selected market..."):
                            res = requests.post(
                                f"{API_URL}/markets/{pending_screener_cid}/start"
                            )
                        if res.status_code == 200:
                            st.success(
                                f"Started quoting for {pending_screener_cid[:8]}..."
                            )
                            st.json(res.json())
                            st.session_state["pending_screener_start_cid"] = None
                            st.session_state["pending_screener_question"] = ""
                            st.rerun()
                        else:
                            st.error(res.text)
                            st.session_state["pending_screener_start_cid"] = None
                            st.session_state["pending_screener_question"] = ""
                    except Exception as e:
                        st.error(f"API Error: {e}")
                        st.session_state["pending_screener_start_cid"] = None
                        st.session_state["pending_screener_question"] = ""
            with c2:
                if st.button("Cancel", key="cancel_screener_start_inline"):
                    st.session_state["pending_screener_start_cid"] = None
                    st.session_state["pending_screener_question"] = ""
else:
    st.info("Click 'Load & Screen Markets' to fetch binary, liquid, medium-term markets from Gamma.")

st.markdown("---")

# 2.5 System Logs (Tail & Search)
st.header("📝 System Logs (Tail & Search)")
with st.expander("View trading engine logs", expanded=False):
    col_search, col_level, col_refresh = st.columns([3, 1, 1])
    with col_search:
        log_filter = st.text_input(
            "Search Filter (substring match)",
            value="",
            placeholder="keyword, condition_id, token_id, etc.",
        )
    with col_level:
        level_filter = st.selectbox(
            "Level",
            options=["ALL", "INFO", "WARNING", "ERROR"],
            index=0,
        )
    with col_refresh:
        refresh = st.button("🔄 Refresh Logs", use_container_width=True)

    # Always render current tail of the log; pressing the button simply triggers a rerun
    raw_logs = tail_logs(LOG_FILE_PATH, lines=500)

    # If tail_logs returned an error message, show directly
    if raw_logs.startswith("Error reading log file") or raw_logs.startswith(
        "Log file not found"
    ):
        st.code(raw_logs, language="log")
    else:
        filtered_lines = []
        level_filter_upper = level_filter.upper()
        keyword = (log_filter or "").strip().lower()

        for line in raw_logs.splitlines():
            line_strip = line.strip()
            if not line_strip:
                continue

            # Level filtering: naive contains check on " | LEVEL | "
            if level_filter_upper != "ALL":
                level_tag = f"| {level_filter_upper} |"
                if level_tag not in line_strip:
                    continue

            # Keyword filtering
            if keyword and keyword not in line_strip.lower():
                continue

            filtered_lines.append(line_strip)

        display_text = "\n".join(filtered_lines) if filtered_lines else "No logs matched the current filters."
        st.code(display_text, language="log")

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
