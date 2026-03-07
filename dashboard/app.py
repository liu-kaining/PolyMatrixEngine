import asyncio
import math
import os
import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta

import httpx

from dotenv import load_dotenv
load_dotenv()

import streamlit as st

# Dashboard debug logging (visible in docker logs)
_log = logging.getLogger("dashboard")
if not _log.handlers:
    _log.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    _log.addHandler(_h)
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

# Gamma API fetch: concurrency & cap
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_PAGE_LIMIT = 1000
MAX_MARKETS = int(os.getenv("GAMMA_MAX_MARKETS", "50000"))
GAMMA_SEMAPHORE = 5

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
    st.session_state["screener_load_mode"] = "Normal"

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


async def _fetch_gamma_page(client: httpx.AsyncClient, sem: asyncio.Semaphore, offset: int) -> tuple[int, list]:
    """Fetch one page of Gamma markets; on failure return (offset, [])."""
    async with sem:
        try:
            r = await client.get(
                GAMMA_API_URL,
                params={"active": "true", "closed": "false", "limit": GAMMA_PAGE_LIMIT, "offset": offset},
                timeout=30.0,
            )
            r.raise_for_status()
            return (offset, r.json() or [])
        except Exception as e:
            _log.warning("Gamma API offset %s failed: %s", offset, e)
            return (offset, [])


async def _fetch_gamma_markets_async() -> list:
    """Paginate Gamma API with concurrency limit (semaphore 5), dedupe by conditionId, cap at MAX_MARKETS."""
    sem = asyncio.Semaphore(GAMMA_SEMAPHORE)
    all_markets: list = []
    seen: set = set()
    async with httpx.AsyncClient(timeout=30.0) as client:
        offset = 0
        while True:
            if len(all_markets) >= MAX_MARKETS:
                break
            offsets = [offset + i * GAMMA_PAGE_LIMIT for i in range(GAMMA_SEMAPHORE)]
            tasks = [_fetch_gamma_page(client, sem, o) for o in offsets]
            results = await asyncio.gather(*tasks)
            done = False
            for _off, page in results:
                if len(page) < GAMMA_PAGE_LIMIT:
                    done = True
                for m in page or []:
                    cid = m.get("conditionId")
                    if cid and cid not in seen:
                        seen.add(cid)
                        all_markets.append(m)
            if done or len(all_markets) >= MAX_MARKETS:
                break
            offset += GAMMA_SEMAPHORE * GAMMA_PAGE_LIMIT
    return all_markets


@st.cache_data(ttl=300, show_spinner="Scanning global markets...")
def fetch_gamma_markets_cached() -> list:
    """Sync wrapper for Streamlit: run async Gamma fetch, cache 5 min. Returns list of raw market dicts."""
    return asyncio.run(_fetch_gamma_markets_async())


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

@st.cache_data(ttl=2, show_spinner=False)
def fetch_engine_status_snapshot(condition_id=None) -> dict:
    """Best-effort API call for real-time engine observability."""
    try:
        params = {"condition_id": condition_id} if condition_id else None
        res = requests.get(f"{API_URL}/markets/status", params=params, timeout=3)
        if res.status_code == 200:
            return res.json()
        _log.warning("Engine status API failed: %s %s", res.status_code, res.text[:200])
    except Exception as e:
        _log.warning("Engine status API exception: %s", e)
    return {"markets": []}


def format_engine_mode(mode: str) -> str:
    mode = (mode or "").upper()
    if mode == "LOCKED_BY_OPPOSITE":
        return "🔴 LOCKED (Opposite Inventory)"
    if mode == "LIQUIDATING":
        return "🟡 LIQUIDATING"
    if mode == "QUOTING":
        return "🟢 QUOTING"
    if mode == "SUSPENDED":
        return "⚫ SUSPENDED"
    return f"⚪ {mode or 'UNKNOWN'}"


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


# ---------------------------------------------------------------------------
# Screener Scoring Logic (Quantitative Market-Making)
# ---------------------------------------------------------------------------
# 量化逻辑说明：
# 1. 硬性过滤：深度<5k、24h量<5k、YES价越界、危险题材 → 直接剔除，不参与打分
# 2. 换手率得分(40%)：volume_24h/liquidity 越高越好，ratio>=2 拿满，0.5~2 线性，<0.5 得0
# 3. 价格居中得分(30%)：越接近0.50多空分歧越大，做市空间越好；偏差>0.3得0
# 4. 绝对流动性得分(30%)：大池子容纳大资金更安全；10万美金满分
# 5. 点差惩罚(最多-20)：spread>0.05 开始扣分，每多0.01扣5分
# ---------------------------------------------------------------------------

DANGER_KEYWORDS = [
    "war", "israel", "gaza", "lebanon", "hamas", "missile", "strike",
    "nuclear", "assassination", "dead",
]


def _fails_hard_filters(market_data: dict) -> bool:
    """
    Hard filters: any trigger → exclude (return True).
    - liquidity < 5000
    - volume_24h < 5000
    - yes_price < 0.20 or yes_price > 0.80
    - Title/Question contains danger keywords
    """
    liq = float(market_data.get("liquidity") or market_data.get("liquidityNum") or 0)
    vol = float(market_data.get("volume24hr") or 0)
    yp = market_data.get("yes_price")
    if yp is None:
        return True
    yp = float(yp)

    if liq < 5000 or vol < 5000:
        return True
    if yp < 0.20 or yp > 0.80:
        return True

    title_text = (market_data.get("question") or market_data.get("title") or "").lower()
    for kw in DANGER_KEYWORDS:
        if kw in title_text:
            return True
    return False


def calculate_market_score(market_data: dict) -> float:
    """
    Polymarket 市场打分函数（满分 100 分）— 面向高频做市商的真实交易需求。

    硬性过滤由调用方在传入前完成；本函数假定 market_data 已通过硬性过滤。

    计分维度：
    1. 换手率得分 (40%)：ratio = volume_24h / liquidity；ratio>=2 拿满 40 分，
       0.5~2 线性插值，<0.5 得 0 分。
    2. 价格居中得分 (30%)：distance = |yes_price - 0.50|；
       30 * (1 - distance/0.30)，偏差>0.3 得 0。
    3. 绝对流动性得分 (30%)：liquidity/100000 * 30，封顶 30 分。
    4. 点差惩罚 (最多 -20)：spread = best_ask - best_bid；
       spread>0.05 时每多 0.01 扣 5 分，最多扣 20 分。
       best_bid/best_ask 缺失时不做惩罚。
    """
    vol = max(1.0, float(market_data.get("volume24hr") or 0))
    liq = max(1.0, float(market_data.get("liquidity") or market_data.get("liquidityNum") or 0))
    yp = market_data.get("yes_price")
    if yp is None:
        return 0.0
    yp = float(yp)

    # 1. Turnover Score (40%): volume_24h / liquidity
    ratio = vol / liq if liq > 0 else 0.0
    if ratio >= 2.0:
        turnover_score = 40.0
    elif ratio >= 0.5:
        # Linear: 0.5 -> 0, 2.0 -> 40
        turnover_score = 40.0 * (ratio - 0.5) / (2.0 - 0.5)
    else:
        turnover_score = 0.0

    # 2. Price Centrality Score (30%): closer to 0.50 = higher
    distance = abs(yp - 0.50)
    if distance >= 0.30:
        price_score = 0.0
    else:
        price_score = 30.0 * (1.0 - (distance / 0.30))

    # 3. Absolute Liquidity Score (30%): liquidity / 100000, capped at 30
    liq_score = min(30.0, (liq / 100_000.0) * 30.0)

    # 4. Spread Penalty (up to -20): best_ask - best_bid
    spread_penalty = 0.0
    best_bid = market_data.get("best_bid") or market_data.get("bestBid")
    best_ask = market_data.get("best_ask") or market_data.get("bestAsk")
    if best_bid is not None and best_ask is not None:
        try:
            bid = float(best_bid)
            ask = float(best_ask)
            spread = ask - bid
            if spread > 0.05:
                # 5 points per 0.01 above 0.05, max 20
                excess = spread - 0.05
                spread_penalty = min(20.0, (excess / 0.01) * 5.0)
        except (ValueError, TypeError):
            pass

    score = turnover_score + price_score + liq_score - spread_penalty
    return max(0.0, min(100.0, score))


def _filter_and_score_screener(raw_markets: list, screener_mode: str) -> list:
    """Filter raw Gamma markets (binary, blacklist, DTE/vol/liq) and compute opportunity score. Returns list of screened dicts."""
    now = datetime.now(timezone.utc)
    # Adjusted thresholds to favor high-frequency turnover
    if screener_mode == "Conservative":
        min_dte = timedelta(days=3)
        min_vol, min_liq = 10_000.0, 2_000.0
        price_low, price_high = 0.20, 0.80
    elif screener_mode == "Normal":
        min_dte = timedelta(days=1)
        min_vol, min_liq = 2_000.0, 400.0
        price_low, price_high = 0.15, 0.85
    elif screener_mode == "Aggressive":
        min_dte = timedelta(hours=6)
        min_vol, min_liq = 500.0, 50.0
        price_low, price_high = 0.05, 0.95
    else:
        min_dte = timedelta(hours=1)  # Ultra: basically everything active
        min_vol, min_liq = 0.0, 0.0
        price_low, price_high = 0.0, 1.0

    sports_blacklist = {
        "sports", "sport", "nfl", "nba", "mlb", "nhl", "soccer", "football", "tennis",
        "hockey", "baseball", "basketball", "premier-league", "premier league",
        "champions-league", "champions league", "division", "win the cup", "stanley cup",
        "super bowl", "world series", "playoffs", "play-offs", "ucl", "uefa"
    }
    question_blacklist = {
        "win the match", "wins the match", "to win the match", "halftime", "half-time",
        "in-play", "in play", "live betting", "live market", "live odds",
        "up or down", "strikes by", "one day after launch", "one week after",
        "points", "score", "goals", "touchdown", "points by", "home team", "away team",
    }
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
        # Hard filter: danger topic keywords (war, etc.) in title/question
        if any(kw in question_lower for kw in DANGER_KEYWORDS):
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
        # Hard filter: liquidity < 5000 or volume_24h < 5000 → exclude
        if liq < 5000 or vol_24h < 5000:
            continue
        yes_price = None
        prices_raw = m.get("outcomePrices")
        if prices_raw:
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw if isinstance(prices_raw, list) else []
            if prices and outcomes:
                yes_idx = next((i for i, o in enumerate(outcomes) if str(o).strip().lower() == "yes"), None)
                if yes_idx is not None and yes_idx < len(prices):
                    try:
                        yes_price = float(prices[yes_idx])
                    except Exception:
                        pass
                elif prices:
                    try:
                        yes_price = float(prices[0])
                    except Exception:
                        pass
        # No valid YES price → can't evaluate, skip
        if yes_price is None:
            continue
        # Hard filter: yes_price outside [0.20, 0.80] → exclude (oscillation zone only)
        if yes_price < 0.20 or yes_price > 0.80:
            continue
        if screener_mode != "Ultra":
            if end_dt - now < min_dte or vol_24h <= min_vol or liq <= min_liq:
                continue
            if not (price_low <= yes_price <= price_high):
                continue
        r_min_s = None
        r_max_sp = None
        try:
            raw_rms = m.get("rewardsMinSize")
            if raw_rms is not None:
                r_min_s = float(raw_rms)
        except (ValueError, TypeError):
            pass
        try:
            raw_rmsp = m.get("rewardsMaxSpread")
            if raw_rmsp is not None:
                r_max_sp = float(raw_rmsp) / 100.0
        except (ValueError, TypeError):
            pass

        # Reward rate: Gamma may put it at top-level "rewardsDailyRate" or inside "clobRewards"[0]
        r_rate_raw = m.get("rewardsDailyRate")
        if r_rate_raw is None:
            cr = m.get("clobRewards") or []
            if isinstance(cr, list) and len(cr) > 0 and isinstance(cr[0], dict):
                r_rate_raw = cr[0].get("rewardsDailyRate")
        try:
            r_rate_val = float(r_rate_raw) if r_rate_raw is not None else None
        except (ValueError, TypeError):
            r_rate_val = None

        # best_bid/best_ask for spread penalty (Gamma may expose these; CLOB has full book)
        best_bid = m.get("bestBid") or m.get("best_bid")
        best_ask = m.get("bestAsk") or m.get("best_ask")
        # Polymarket "竞争度" (competition): 0–1, lower = less competition = better for reward farming
        try:
            competitive_val = float(m.get("competitive")) if m.get("competitive") is not None else None
        except (ValueError, TypeError):
            competitive_val = None
        screened.append({
            "question": question_text,
            "yes_price": yes_price,
            "volume24hr": vol_24h,
            "liquidity": liq,
            "end_date": end_dt,
            "condition_id": m.get("conditionId"),
            "slug": m.get("slug"),
            "category": display_category,
            "rewards_min_size": r_min_s,
            "rewards_max_spread": r_max_sp,
            "reward_rate_per_day": r_rate_val,
            "competitive": competitive_val,
            "best_bid": best_bid,
            "best_ask": best_ask,
        })

    if screened:
        for m in screened:
            score = calculate_market_score(m)
            m["recommendation_score"] = round(score, 1)
            # Star bands: 1* 0-19, 2* 20-39, 3* 40-59, 4* 60-79, 5* 80-100
            m["stars"] = max(1, min(5, 1 + int(score / 20)))
    screened.sort(key=lambda x: (x.get("recommendation_score", 0), x["volume24hr"]), reverse=True)

    # Category diversity cap removed for high-frequency trading focus
    # We want the absolute most active markets regardless of category
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

st.subheader("Real-time Engine Status")
engine_status = fetch_engine_status_snapshot()
status_rows = engine_status.get("markets", []) if isinstance(engine_status, dict) else []

if status_rows:
    for i, row in enumerate(status_rows):
        cid = row.get("condition_id", "")
        st.markdown(f"**Market:** `{cid}`")

        fv_yes = row.get("fv_yes")
        fv_no = row.get("fv_no")
        fv_sum = row.get("fv_sum")

        c1, c2, c3 = st.columns(3)
        if fv_yes is None or fv_no is None:
            c1.metric("FV_yes", "—")
            c2.metric("FV_no", "—")
            c3.metric("FV_yes + FV_no", "—")
        else:
            c1.metric("FV_yes", f"{float(fv_yes):.4f}")
            c2.metric("FV_no", f"{float(fv_no):.4f}")
            c3.metric("FV_yes + FV_no", f"{float(fv_sum):.4f}" if fv_sum is not None else "—")

        s1, s2 = st.columns(2)
        s1.markdown(f"**YES Engine:** {format_engine_mode(row.get('yes_mode'))}")
        s2.markdown(f"**NO Engine:** {format_engine_mode(row.get('no_mode'))}")

        st.caption(
            f"YES exposure={float(row.get('yes_exposure', 0.0)):.4f} | "
            f"NO exposure={float(row.get('no_exposure', 0.0)):.4f}"
        )

        # Rewards Eligibility
        r_min_size = row.get("rewards_min_size")
        r_max_spread = row.get("rewards_max_spread")
        has_rewards = (r_min_size is not None and float(r_min_size) > 0) or (
            r_max_spread is not None and float(r_max_spread) > 0
        )
        if has_rewards:
            yes_rt = row.get("yes_runtime") or {}
            no_rt = row.get("no_runtime") or {}
            yes_elig = yes_rt.get("rewards_eligible", False)
            no_elig = no_rt.get("rewards_eligible", False)
            farming = yes_elig or no_elig
            status_icon = "🟢 FARMING" if farming else "⚪ INELIGIBLE"
            min_s = f"{float(r_min_size):.0f}" if r_min_size else "—"
            max_sp = f"{float(r_max_spread) * 100:.1f}¢" if r_max_spread else "—"
            st.markdown(
                f"**Rewards Eligibility:** {status_icon} &nbsp;|&nbsp; "
                f"Min Size: **{min_s}** &nbsp;|&nbsp; Max Spread: **{max_sp}**"
            )
        else:
            st.caption("Rewards: No liquidity rewards program for this market.")

        if i < len(status_rows) - 1:
            st.markdown("")
else:
    st.caption("No engine status yet. Start a market and wait for first ticks.")

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
        index=1,  # Default: Normal (min_dte=3d, safer than Ultra)
        help=t("app.screener_mode_help"),
    )

st.caption(t("app.loading_markets_hint"))

col_load, _ = st.columns([1, 3])
with col_load:
    if st.button(t("app.load_screen_markets"), use_container_width=True):
        try:
            raw_markets = fetch_gamma_markets_cached()
            with st.spinner(t("app.loading_filtering")):
                screened = _filter_and_score_screener(raw_markets, screener_mode)
            if not screened:
                st.error(t("app.no_markets_loaded"))
            else:
                st.session_state["screener_markets"] = screened
                st.session_state["screener_load_mode"] = screener_mode
        except Exception as e:
            st.error(t("app.gamma_error").format(e=e))
        st.rerun()

screened_markets = st.session_state.get("screener_markets", [])
if screened_markets:
    # Filter: 只显示 4 星及以上（默认开启）、仅奖励市场、仅竞争度低
    col_f1, col_f2, col_f3 = st.columns([1, 1, 1])
    with col_f1:
        filter_4star = st.checkbox(
            t("app.filter_4star"),
            value=True,
            key="screener_filter_4star_cb",
            help=t("app.filter_4star_help"),
        )
    with col_f2:
        filter_rewards_only = st.checkbox(
            t("app.filter_rewards_only"),
            value=False,
            key="screener_filter_rewards_cb",
            help=t("app.filter_rewards_only_help"),
        )
    with col_f3:
        filter_low_competition = st.checkbox(
            t("app.filter_low_competition"),
            value=False,
            key="screener_filter_low_comp_cb",
            help=t("app.filter_low_competition_help"),
        )

    # Apply filters
    display_markets = screened_markets
    if filter_4star:
        display_markets = [m for m in display_markets if m.get("stars", 0) >= 4]
    if filter_rewards_only:
        display_markets = [m for m in display_markets if (m.get("rewards_min_size") or 0) > 0]
    if filter_low_competition:
        display_markets = [m for m in display_markets if m.get("competitive") is not None and m.get("competitive") < 0.6]
    
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

        # Table with click-to-select: each row has a Select button; click it to select that market.
        current_idx = st.session_state.get("screener_selected_idx", 0)
        current_idx = max(0, min(current_idx, len(display_markets) - 1))

        # Table header (Competition = Polymarket 竞争度, lower % = easier to earn rewards)
        col_w = [0.3, 0.5, 0.4, 2.5, 0.7, 0.5, 0.5, 0.5, 0.6, 0.5, 0.5, 0.5, 0.5]
        hc = st.columns(col_w)
        for col, label in zip(hc, ["", "Stars", "Score", "Question", "Category", "YES", "Vol 24h", "Liq", t("app.col_rewards_per_day"), t("app.col_min_size"), t("app.col_spread"), t("app.col_competition"), t("app.select")]):
            col.markdown(f"**{label}**" if label else "")

        # Rows: click Select to choose that market
        for i, m in enumerate(display_markets):
            cols = st.columns(col_w)
            stars = "★" * m.get("stars", 0) + "☆" * (5 - m.get("stars", 0))
            with cols[0]:
                st.write("▶" if i == current_idx else "")
            with cols[1]:
                st.write(stars)
            with cols[2]:
                st.write(f"{m.get('recommendation_score', 0):.1f}")
            with cols[3]:
                st.write(m["question"])
            with cols[4]:
                st.write(m.get("category", ""))
            with cols[5]:
                st.write(f"{m['yes_price']:.3f}" if m.get("yes_price") is not None else "—")
            with cols[6]:
                st.write(f"{m.get('volume24hr', 0):.0f}")
            with cols[7]:
                st.write(f"{m.get('liquidity', 0):.0f}")
            
            # Rewards columns
            r_rate = m.get("reward_rate_per_day")
            r_min = m.get("rewards_min_size")
            r_sp = m.get("rewards_max_spread")
            comp = m.get("competitive")
            with cols[8]:
                st.write(f"{r_rate:.2f}" if r_rate else "—")
            with cols[9]:
                st.write(f"{r_min:.0f}" if r_min else "—")
            with cols[10]:
                st.write(f"{r_sp * 100:.1f}¢" if r_sp else "—")
            with cols[11]:
                st.write(f"{comp * 100:.0f}%" if comp is not None else "—")
            with cols[12]:
                if st.button("✓", key=f"screener_sel_{i}", help=t("app.click_to_select")):
                    st.session_state["screener_selected_idx"] = i
                    st.rerun()

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
                            url = f"{API_URL}/markets/{pending_screener_cid}/start"
                            _log.info("[Screener] POST %s", url)
                            with st.spinner(t("app.starting_quoting_screener")):
                                res = requests.post(url, timeout=30)
                            if res.status_code == 200:
                                _log.info("[Screener] Start OK: %s", res.json())
                                st.success(
                                    f"Started quoting for {pending_screener_cid[:8]}..."
                                )
                                st.json(res.json())
                                st.session_state["pending_screener_start_cid"] = None
                                st.session_state["pending_screener_question"] = ""
                                st.rerun()
                            else:
                                _log.warning("[Screener] Start failed: %s %s", res.status_code, res.text[:200])
                                st.error(res.text)
                                st.session_state["pending_screener_start_cid"] = None
                                st.session_state["pending_screener_question"] = ""
                        except Exception as e:
                            _log.exception("[Screener] Start request failed: %s", e)
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
