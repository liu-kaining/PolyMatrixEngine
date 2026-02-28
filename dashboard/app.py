import os
import json
import time
from datetime import datetime, timezone, timedelta

import streamlit as st
import pandas as pd
from sqlalchemy import create_engine
import requests

try:
    from dashboard.i18n import t, TRANSLATIONS
except ImportError:
    from i18n import t, TRANSLATIONS

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
if "screener_loading" not in st.session_state:
    st.session_state["screener_loading"] = False
if "screener_load_page" not in st.session_state:
    st.session_state["screener_load_page"] = 0
if "screener_raw_markets" not in st.session_state:
    st.session_state["screener_raw_markets"] = []
if "screener_load_mode" not in st.session_state:
    st.session_state["screener_load_mode"] = "Ultra"

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


def _filter_and_score_screener(raw_markets: list, screener_mode: str) -> list:
    """Filter raw Gamma markets (binary, blacklist, DTE/vol/liq) and compute recommendation score. Returns list of screened dicts."""
    now = datetime.now(timezone.utc)
    if screener_mode == "Conservative":
        min_dte = timedelta(days=7)
        min_vol, min_liq = 50_000.0, 10_000.0
        price_low, price_high = 0.25, 0.75
    elif screener_mode == "Normal":
        min_dte = timedelta(days=3)
        min_vol, min_liq = 10_000.0, 3_000.0
        price_low, price_high = 0.20, 0.80
    elif screener_mode == "Aggressive":
        min_dte = timedelta(days=1)
        min_vol, min_liq = 1_000.0, 500.0
        price_low, price_high = 0.10, 0.90
    else:
        min_dte = timedelta(days=0)
        min_vol, min_liq = 0.0, 0.0
        price_low, price_high = 0.0, 1.0

    sports_blacklist = {"sports", "sport", "nfl", "nba", "mlb", "nhl", "soccer", "football", "premier-league", "premier league", "champions-league", "champions league"}
    question_blacklist = {"win the match", "wins the match", "to win the match", "halftime", "half-time", "in-play", "in play", "live betting", "live market", "live odds"}
    premium_keywords = {"politics", "elections", "election", "culture"}
    politics_words = {"president", "presidential", "election", "primary", "senate", "governor", "mayor", "parliament", "referendum"}
    culture_words = {"oscars", "oscar", "grammy", "emmy", "box office", "movie", "film", "series", "season", "tv show", "album", "song", "music"}

    screened = []
    for m in raw_markets:
        tags_raw = m.get("tags")
        tags_list = []
        if tags_raw:
            if isinstance(tags_raw, str):
                try:
                    parsed = json.loads(tags_raw)
                    tags_list = parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, str) else []
                except Exception:
                    tags_list = [t.strip() for t in tags_raw.replace(";", ",").split(",") if t.strip()]
            elif isinstance(tags_raw, list):
                tags_list = tags_raw
        category_raw = m.get("category") or m.get("subCategory") or ""
        slug = (m.get("slug") or "").lower()
        question_text = m.get("question") or ""
        question_lower = question_text.lower()
        tag_text = " ".join(str(x) for x in tags_list)
        category_haystack = " ".join([category_raw, tag_text, slug]).lower()
        if any(kw in category_haystack for kw in sports_blacklist) or any(kw in question_lower for kw in question_blacklist):
            continue
        cat_source = category_raw.strip() or (tags_list[0].strip() if tags_list else "")
        cat_match_text = (category_raw or "") + " " + tag_text
        is_politics_semantic = any(w in question_lower for w in politics_words) or any(w in slug for w in politics_words)
        is_culture_semantic = any(w in question_lower for w in culture_words) or any(w in slug for w in culture_words)
        is_premium = any(pk in cat_match_text.lower() for pk in premium_keywords) or is_politics_semantic or is_culture_semantic
        if is_premium:
            base_cat = "Politics" if is_politics_semantic or "politic" in cat_match_text.lower() else "Culture" if is_culture_semantic else cat_source or "Premium"
            display_category = f"⭐ {base_cat}"
        else:
            display_category = cat_source or "General"

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
        if {str(o).strip().lower() for o in outcomes} != {"yes", "no"}:
            continue
        try:
            end_dt = datetime.fromisoformat((m.get("endDate") or "").replace("Z", "+00:00"))
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
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw if isinstance(prices_raw, list) else []
            if prices:
                try:
                    yes_price = float(prices[0])
                except Exception:
                    pass
        if screener_mode != "Ultra":
            if end_dt - now < min_dte or vol_24h <= min_vol or liq <= min_liq:
                continue
            if yes_price is not None and not (price_low <= yes_price <= price_high):
                continue
        screened.append({
            "question": question_text,
            "yes_price": yes_price,
            "volume24hr": vol_24h,
            "liquidity": liq,
            "end_date": end_dt,
            "condition_id": m.get("conditionId"),
            "slug": m.get("slug"),
            "category": display_category,
        })

    if screened:
        max_vol_liq = max((x["volume24hr"] * x["liquidity"] for x in screened), default=1.0) or 1.0
        for m in screened:
            vol, liq = m["volume24hr"], m["liquidity"]
            yp = m["yes_price"] if m["yes_price"] is not None else 0.5
            dte_days = (m["end_date"] - now).total_seconds() / 86400.0
            cat = m.get("category", "")
            fill_score = 20.0 * min(1.0, vol / 50000.0) + 20.0 * min(1.0, liq / 15000.0)
            dte_score = 12.0 * min(1.0, dte_days / 30.0)
            price_score = 12.0 if 0.20 <= yp <= 0.80 else (6.0 if 0.10 <= yp <= 0.90 else 0.0)
            premium_bonus = 11.0 if "⭐" in cat else 0.0
            risk_score = dte_score + price_score + premium_bonus
            opp_score = 25.0 * (vol * liq) / max_vol_liq if max_vol_liq else 0.0
            total = fill_score + risk_score + opp_score
            m["recommendation_score"] = round(total, 1)
            m["stars"] = max(1, min(5, 1 + int(total / 20)))
            m["fill_score"] = round(fill_score, 1)
            m["risk_score"] = round(risk_score, 1)
    screened.sort(key=lambda x: (x.get("recommendation_score", 0), x["volume24hr"]), reverse=True)
    return screened


# ----------------- SIDEBAR -----------------
if "locale" not in st.session_state:
    st.session_state["locale"] = "en"
lang = st.sidebar.radio(
    t("app.language"),
    options=["en", "zh"],
    format_func=lambda x: "English" if x == "en" else "中文",
    key="locale_radio",
    horizontal=True,
)
st.session_state["locale"] = lang

st.sidebar.title(t("app.title"))
st.sidebar.markdown(f"### {t('app.control_panel')}")

with st.sidebar.form("start_market_form"):
    st.markdown(f"**{t('app.start_market_making')}**")
    market_input = st.text_input(
        t("app.condition_id_input"),
        placeholder=t("app.condition_id_placeholder"),
    )
    confirm_live = st.checkbox(t("app.confirm_live"))
    submitted = st.form_submit_button(t("app.start_quoting"))
    
    if submitted:
        if not market_input:
            st.warning(t("app.please_enter_id"))
        elif not confirm_live:
            st.warning(t("app.please_confirm_box"))
        else:
            condition_id = resolve_condition_id(market_input)
            if not condition_id:
                st.error(t("app.could_not_resolve"))
            else:
                # Defer actual API call to a second explicit confirmation step
                st.session_state["pending_start_condition_id"] = condition_id

# Sidebar confirmation for starting market making
pending_cid = st.session_state.get("pending_start_condition_id")
if pending_cid:
    st.sidebar.warning(t("app.confirm_start_message").format(cid=pending_cid[:8]))
    c_col1, c_col2 = st.sidebar.columns(2)
    with c_col1:
        if st.button(f"✅ {t('app.confirm_start')}", key="confirm_start_sidebar"):
            try:
                with st.spinner(t("app.initializing")):
                    response = requests.post(f"{API_URL}/markets/{pending_cid}/start")
                if response.status_code == 200:
                    data = response.json()
                    st.success(t("app.started_quoting").format(cid=pending_cid[:8]))
                    st.json(data)
                    st.session_state["pending_start_condition_id"] = None
                    st.rerun()
                else:
                    st.error(t("app.failed").format(text=response.text))
            except Exception as e:
                st.error(t("app.api_connection_error").format(e=e))
                st.session_state["pending_start_condition_id"] = None
    with c_col2:
        if st.button(t("app.cancel"), key="cancel_start_sidebar"):
            st.session_state["pending_start_condition_id"] = None

st.sidebar.markdown("---")
st.sidebar.markdown(f"### {t('app.emergency_controls')}")

kill_condition_id = st.sidebar.text_input(t("app.target_condition_id"), placeholder="0x...", key="kill_input")

col_stop, col_liq = st.sidebar.columns(2)
_label_stop = t("app.stop")
_label_liq = t("app.liquidate_all")

with col_stop:
    if st.button(f"🛑 {_label_stop}", help=t("app.stop_help")):
        if kill_condition_id:
            st.session_state["pending_kill_action"] = "stop"
            st.session_state["pending_kill_condition_id"] = kill_condition_id
        else:
            st.warning(t("app.enter_id"))

with col_liq:
    if st.button(f"☢️ {_label_liq}", help=t("app.liquidate_help")):
        if kill_condition_id:
            st.session_state["pending_kill_action"] = "liquidate"
            st.session_state["pending_kill_condition_id"] = kill_condition_id
        else:
            st.warning(t("app.enter_id"))

# Sidebar confirmation for Stop / Liquidate
pending_kill_action = st.session_state.get("pending_kill_action")
pending_kill_cid = st.session_state.get("pending_kill_condition_id")
if pending_kill_action and pending_kill_cid:
    label = _label_stop if pending_kill_action == "stop" else _label_liq
    st.sidebar.warning(t("app.confirm_action_message").format(label=label, cid=pending_kill_cid[:8]))
    k_col1, k_col2 = st.sidebar.columns(2)
    with k_col1:
        if st.button(f"✅ {t('app.confirm_stop') if pending_kill_action == 'stop' else t('app.confirm_liquidate')}", key="confirm_kill_action"):
            endpoint = "stop" if pending_kill_action == "stop" else "liquidate"
            try:
                res = requests.post(f"{API_URL}/markets/{pending_kill_cid}/{endpoint}")
                if res.status_code == 200:
                    st.success(t("app.label_executed").format(label=label))
                    st.session_state["pending_kill_action"] = None
                    st.session_state["pending_kill_condition_id"] = None
                    st.rerun()
                else:
                    st.error(res.text)
                    st.session_state["pending_kill_action"] = None
                    st.session_state["pending_kill_condition_id"] = None
            except Exception as e:
                st.error(t("app.api_error").format(e=e))
                st.session_state["pending_kill_action"] = None
                st.session_state["pending_kill_condition_id"] = None
    with k_col2:
        if st.button(t("app.cancel"), key="cancel_kill_action"):
            st.session_state["pending_kill_action"] = None
            st.session_state["pending_kill_condition_id"] = None

st.sidebar.markdown("---")
st.sidebar.markdown(f"### {t('app.danger_zone')}")
with st.sidebar.form("wipe_form"):
    st.markdown(f"**{t('app.wipe_all_data')}**")
    wipe_confirm = st.text_input(t("app.type_wipe_confirm"), value="")
    wipe_submitted = st.form_submit_button(f"🔥 {t('app.wipe_all_btn')}")

    if wipe_submitted:
        if wipe_confirm.strip() != "WIPE":
            st.warning(t("app.please_type_wipe"))
        else:
            try:
                res = requests.post(f"{API_URL}/admin/wipe")
                if res.status_code == 200:
                    st.success(t("app.wipe_success"))
                    st.rerun()
                else:
                    st.error(res.text)
            except Exception as e:
                st.error(t("app.api_error").format(e=e))

st.sidebar.markdown("---")
if st.sidebar.button(t("app.refresh_data"), use_container_width=True):
    st.rerun()

# ----------------- MAIN PAGE -----------------
st.title(f"📈 {t('app.dashboard_title')}")

# 1. Inventory & Risk Panel
st.header(f"🛡️ {t('app.inventory_risk')}")
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
    with st.expander(t("app.market_exposures"), expanded=gross_exposure > 0):
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
    st.info(t("app.no_inventory"))

st.markdown("---")

# 2. Market Screener (Gamma)
st.header(f"🧭 {t('app.screener_title')}")
with st.expander(f"📌 {t('app.screener_expander')}", expanded=False):
    st.markdown(
        f"{t('app.screener_score_intro')}\n"
        f"- {t('app.screener_fill')}\n"
        f"- {t('app.screener_profit')}\n"
        f"- {t('app.screener_risk')}\n\n"
        f"{t('app.screener_sort')}\n\n"
        f"{t('app.screener_data_source')}"
    )

mode_col, _ = st.columns([1, 3])
with mode_col:
    screener_mode = st.selectbox(
        t("app.screener_mode"),
        options=["Conservative", "Normal", "Aggressive", "Ultra"],
        index=0,
        help=t("app.screener_mode_help"),
    )

st.caption(t("app.loading_markets_hint"))

col_load, _ = st.columns([1, 3])
with col_load:
    loading = st.session_state.get("screener_loading", False)
    if loading:
        load_page = st.session_state.get("screener_load_page", 0)
        total_pages = 10
        if load_page < total_pages:
            progress = (load_page + 1) / total_pages
            st.caption(t("app.loading_progress").format(current=load_page + 1, total=total_pages))
            progress_bar = st.progress(progress)
            raw_markets = list(st.session_state.get("screener_raw_markets", []))
            offset = load_page * 500
            try:
                resp = requests.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"active": "true", "closed": "false", "limit": 500, "offset": offset},
                    timeout=15,
                )
                if resp.status_code != 200:
                    st.warning(f"Gamma API returned {resp.status_code} at offset {offset}")
                    st.session_state["screener_loading"] = False
                    st.session_state["screener_load_page"] = 0
                    st.session_state["screener_raw_markets"] = []
                    st.rerun()
                else:
                    page = resp.json()
                    seen = {m.get("conditionId") for m in raw_markets}
                    for m in page or []:
                        cid = m.get("conditionId")
                        if cid and cid not in seen:
                            seen.add(cid)
                            raw_markets.append(m)
                    st.session_state["screener_raw_markets"] = raw_markets
                    st.session_state["screener_load_page"] = load_page + 1
                    if len(page or []) < 500:
                        st.session_state["screener_load_page"] = total_pages
                time.sleep(0.25)
            except Exception as e:
                st.error(t("app.gamma_error").format(e=e))
                st.session_state["screener_loading"] = False
                st.session_state["screener_load_page"] = 0
                st.session_state["screener_raw_markets"] = []
                st.rerun()
            else:
                st.rerun()
        else:
            st.caption(t("app.loading_filtering"))
            progress_bar = st.progress(1.0)
            raw_markets = st.session_state.get("screener_raw_markets", [])
            load_mode = st.session_state.get("screener_load_mode", "Ultra")
            try:
                screened = _filter_and_score_screener(raw_markets, load_mode)
                if not screened:
                    st.error(t("app.no_markets_loaded"))
                else:
                    st.session_state["screener_markets"] = screened
            except Exception as e:
                st.error(t("app.gamma_error").format(e=e))
            st.session_state["screener_loading"] = False
            st.session_state["screener_load_page"] = 0
            st.session_state["screener_raw_markets"] = []
            st.session_state["screener_load_mode"] = "Ultra"
            st.rerun()
    else:
        if st.button(t("app.load_screen_markets"), use_container_width=True):
            st.session_state["screener_loading"] = True
            st.session_state["screener_load_page"] = 0
            st.session_state["screener_raw_markets"] = []
            st.session_state["screener_load_mode"] = screener_mode
            st.rerun()

screened_markets = st.session_state.get("screener_markets", [])
if screened_markets:
    # Filter: 只显示 4 星及以上（默认开启）
    filter_4star = st.checkbox(
        t("app.filter_4star"),
        value=True,
        key="screener_filter_4star_cb",
        help=t("app.filter_4star_help"),
    )

    display_markets = [m for m in screened_markets if m.get("stars", 0) >= 4] if filter_4star else screened_markets
    if not display_markets:
        st.info(t("app.no_4star"))
    else:
        filter_label = t("app.filter_label_4star") if filter_4star else t("app.filter_label_all")
        st.caption(
            t("app.caption_display").format(
                n=len(display_markets),
                filter_label=filter_label,
                total=len(screened_markets),
            )
        )

        # Structured table view with interactive selection (based on display_markets)
        current_idx = st.session_state.get("screener_selected_idx", 0)
        current_idx = max(0, min(current_idx, len(display_markets) - 1))

        df_screen = pd.DataFrame(
            [
                {
                    "Stars": "★" * m.get("stars", 0) + "☆" * (5 - m.get("stars", 0)),
                    "Score": m.get("recommendation_score", 0),
                    "Question": m["question"],
                    "Category/Tag": m.get("category", ""),
                    "YES Price": m["yes_price"],
                    "Volume 24h": m["volume24hr"],
                    "Liquidity": m["liquidity"],
                    "End Date": m["end_date"].strftime("%Y-%m-%d"),
                    "Condition ID": m["condition_id"],
                    "Slug": m["slug"],
                }
                for m in display_markets
            ]
        )

        # Single selection: dropdown chooses one market; table is read-only with ▶ indicating selection.
        sel_idx = st.selectbox(
            t("app.selected_market"),
            options=range(len(display_markets)),
            index=current_idx,
            format_func=lambda i: (display_markets[i]["question"][:80] + "…") if len(display_markets[i]["question"]) > 80 else display_markets[i]["question"],
            key="screener_market_select",
            help=t("app.select_one_help"),
        )
        if sel_idx != current_idx:
            st.session_state["screener_selected_idx"] = int(sel_idx)
            st.rerun()

        df_screen["Current"] = ""
        if 0 <= current_idx < len(df_screen):
            df_screen.loc[current_idx, "Current"] = "▶"

        st.dataframe(
            df_screen[
                [
                    "Current",
                    "Stars",
                    "Score",
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
        )
        st.caption(t("app.table_selection_hint"))

        st.markdown(f"#### {t('app.launch_quoting')}")

        # Nicely formatted "card" preview for the currently selected market (from display_markets)
        sel_idx = st.session_state.get("screener_selected_idx", 0)
        sel_idx = max(0, min(sel_idx, len(display_markets) - 1))
        selected_market = display_markets[sel_idx]
        yes_p = selected_market["yes_price"]

        st.markdown(f"##### {t('app.selected_market')}")
        # Prominent card: larger padding, bigger question font, left accent, shadow
        card_html = f"""
        <div style="
            border-radius: 12px;
            padding: 20px 24px;
            margin: 12px 0 16px 0;
            background: linear-gradient(to right, #e8eaf6 0%, #f5f5fb 12px);
            border: 1px solid #9fa8da;
            border-left: 5px solid #3f51b5;
            box-shadow: 0 2px 8px rgba(63,81,181,0.15);
        ">
          <div style="font-weight: 700; font-size: 1.1rem; margin-bottom: 12px; line-height: 1.4;">
            {selected_market['question']}
          </div>
          <div style="font-size: 14px; color: #333;">
            <div style="margin-bottom: 6px;">Condition ID: <code style="background:#fff; padding:2px 6px; border-radius:4px;">{selected_market['condition_id']}</code></div>
            <div style="margin-top: 8px; display: flex; flex-wrap: wrap; gap: 16px;">
              <span>YES Price: <b>{yes_p if yes_p is not None else 'N/A'}</b></span>
              <span>24h Volume: <b>${selected_market['volume24hr']:.0f}</b></span>
              <span>Liquidity: <b>${selected_market['liquidity']:.0f}</b></span>
              <span>End Date: <b>{selected_market['end_date'].strftime('%Y-%m-%d')}</b></span>
            </div>
          </div>
        </div>
        """
        st.markdown(card_html, unsafe_allow_html=True)

        col_start, col_info = st.columns([1, 3])
        with col_start:
            if st.button(
                f"✅ {t('app.start_from_screener')}",
                key="start_from_screener",
                help=t("app.start_from_screener_help"),
            ):
                m_sel = display_markets[sel_idx]
                if not m_sel["condition_id"]:
                    st.error(t("app.missing_condition_id"))
                else:
                    st.session_state["pending_screener_start_cid"] = m_sel["condition_id"]
                    st.session_state["pending_screener_question"] = m_sel["question"]
        with col_info:
            st.write(t("app.displaying_markets").format(n=len(display_markets), total=len(screened_markets)))

        # Compact confirmation panel for screener-based starts
        pending_screener_cid = st.session_state.get("pending_screener_start_cid")
        if pending_screener_cid:
            m_confirm = next(
                (m for m in screened_markets if m["condition_id"] == pending_screener_cid),
                None,
            )
            if not m_confirm:
                st.warning(t("app.market_no_longer"))
                st.session_state["pending_screener_start_cid"] = None
                st.session_state["pending_screener_question"] = ""
            else:
                yes_p = m_confirm["yes_price"]
                st.info(t("app.confirm_screener_start"))
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
                        f"✅ {t('app.confirm_screener_btn')}", key="confirm_screener_start_inline"
                    ):
                        try:
                            with st.spinner(t("app.starting_quoting_screener")):
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
                    if st.button(t("app.cancel"), key="cancel_screener_start_inline"):
                        st.session_state["pending_screener_start_cid"] = None
                        st.session_state["pending_screener_question"] = ""
else:
    st.info(t("app.load_markets_hint"))

st.markdown("---")

# 2.5 System Logs (Tail & Search)
st.header(f"📝 {t('app.system_logs')}")
with st.expander(t("app.view_logs"), expanded=False):
    col_search, col_level, col_refresh = st.columns([3, 1, 1])
    with col_search:
        log_filter = st.text_input(
            "Search Filter (substring match)",
            value="",
            placeholder=t("app.log_search_placeholder"),
        )
    with col_level:
        level_filter = st.selectbox(
            "Level",
            options=["ALL", "INFO", "WARNING", "ERROR"],
            index=0,
        )
    with col_refresh:
        refresh = st.button(f"🔄 {t('app.refresh_logs')}", use_container_width=True)

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

        display_text = "\n".join(filtered_lines) if filtered_lines else t("app.no_logs_matched")
        st.code(display_text, language="log")

# 3. Active Orders Panel
st.header(f"📋 {t('app.active_orders')}")
orders_df = fetch_active_orders()

if not orders_df.empty:
    st.metric(t("app.total_active_orders"), len(orders_df))
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
    st.info(t("app.no_active_orders"))

st.markdown("---")

# 4. System Status
st.header(f"⚙️ {t('app.system_status')}")
st.markdown(t("app.api_health"))

try:
    health = requests.get(f"{API_URL}/health", timeout=2)
    if health.status_code == 200:
        st.success(t("app.backend_online"))
        st.json(health.json())
    else:
        st.warning(t("app.backend_unknown"))
except Exception as e:
    st.error(t("app.backend_offline"))
    
st.caption(t("app.log_tip"))
