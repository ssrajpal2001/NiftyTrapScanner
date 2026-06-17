"""
live_tracker.py — Live HTF/LTF Trap Tracker (WebSocket edition)
================================================================
Run:  streamlit run live_tracker.py

Data flow:
  • Morning init (once): REST API → historical 1-min bars → HTF scan → seed ws_feed
  • Live (every 2 s):   ws_feed tick aggregator → LTF scan → dashboard refresh
  • No REST polling during session — WebSocket provides real-time LTP + candles
"""

import os
import time
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
from datetime import datetime, timedelta, date, time as dtime
from pathlib import Path

def _load_env_file(env_path: Path) -> None:
    """Load KEY=VALUE pairs from .env into os.environ (no external dep needed)."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k and k not in os.environ:   # don't override if already set
            os.environ[k] = v.strip()

_load_env_file(Path(__file__).parent / ".env")

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

import ws_feed                                      # WebSocket + candle aggregator
import angel_orders                                 # Paper / live order management

from config import (
    HTF_MINUTES, LTF_MINUTES,
    DEFAULT_SL_BUFFER, DEFAULT_LOT_SIZE,
)
from data import (
    fetch_spot_prev_day, fetch_today_open, fetch_index_ltp,
    get_instrument_key, get_option_expiries,
    fetch_1min, fetch_1min_intraday, resample_tf, pivot_levels, OPTION_CHAIN_KEYS,
    # MCX helpers
    get_mcx_active_futures, get_mcx_option_chain, get_mcx_instrument_key,
    get_mcx_option_expiries, fetch_mcx_prev_day, fetch_mcx_today_open,
    MCX_CRUDE_FUTURES_KEY,
)
from config import MCX_SESSION_START, MCX_SESSION_END
from scanner import scan_htf, scan_ltf, simulate_today_trades, scan_htf_spot, spot_bias

# ─── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Live Trap Tracker",
    page_icon="L",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""<style>
html, body, [data-testid="stAppViewContainer"] {
    background-color: #FFFFFF; color: #1A1A1A;
    font-family: 'Segoe UI', sans-serif;
}
[data-testid="stSidebar"] { background-color: #F5F7FA; border-right: 1px solid #D0D7DE; }
[data-testid="stSidebar"] * { color: #1A1A1A !important; }
h1, h2, h3 { color: #0969DA; }
.metric-card {
    background: #F5F7FA; border: 1px solid #D0D7DE;
    border-radius: 8px; padding: 12px 16px; text-align: center;
}
.metric-label { color: #57606A; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
.metric-value { color: #1A1A1A; font-size: 20px; font-weight: bold; margin-top: 4px; }
.metric-value.green  { color: #1A7F37; }
.metric-value.red    { color: #CF222E; }
.metric-value.blue   { color: #0969DA; }
.metric-value.orange { color: #BF5B00; }
.sec-header {
    background: #EAF0F6; border-left: 4px solid #0969DA;
    padding: 8px 14px; margin: 16px 0 8px 0;
    font-size: 14px; font-weight: 600; color: #0969DA;
    border-radius: 0 4px 4px 0;
}
.alert-orange {
    background: #FFF8E6; border: 1px solid #BF5B00; border-left: 5px solid #BF5B00;
    padding: 12px 16px; margin: 6px 0; border-radius: 4px;
    font-size: 14px; font-weight: 600; color: #BF5B00;
}
.alert-green {
    background: #EAFBEE; border: 1px solid #1A7F37; border-left: 5px solid #1A7F37;
    padding: 12px 16px; margin: 6px 0; border-radius: 4px;
    font-size: 14px; font-weight: 600; color: #1A7F37;
}
.alert-red {
    background: #FFF0F0; border: 1px solid #CF222E; border-left: 5px solid #CF222E;
    padding: 12px 16px; margin: 6px 0; border-radius: 4px;
    font-size: 14px; font-weight: 600; color: #CF222E;
}
.zone-box {
    background: #F0F4F8; border: 1px solid #D0D7DE;
    border-radius: 6px; padding: 10px 14px; margin: 4px 0;
    font-size: 13px; color: #1A1A1A;
}
</style>""", unsafe_allow_html=True)

# ─── Constants ─────────────────────────────────────────────────────────────────
LOG_DIR      = Path(os.environ.get("TRAP_LOG_DIR", Path(__file__).parent / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
MARKET_OPEN  = datetime.strptime("09:15", "%H:%M").time()
MARKET_CLOSE = datetime.strptime("15:30", "%H:%M").time()

SPOT_KEYS = {
    "Nifty":    "NSE_INDEX|Nifty 50",
    "Sensex":   "BSE_INDEX|SENSEX",
    "CrudeOil": MCX_CRUDE_FUTURES_KEY,   # MCX front-month futures (update monthly)
}

INDEX_CONFIG = {
    "Nifty": {
        "label"          : "Nifty 50",
        "strike_step"    : 100,
        "expiry_fn"      : lambda ref: _next_weekday(ref, 1),
        "symbol_prefix"  : "NIFTY",
        "exchange"       : "NSE_FO",
        "key_prefix"     : "NIFTY",
        "numeric_keys"   : False,
        "gap_itm_near"   : 250,
        "gap_itm_far"    : 500,
        "lot_size"       : 75,    # Nifty weekly lot size
        "zone_far_pts"   : 100,   # cascade if zone_high > last_close + 100 pts
        # Trading time gates (morning hold — no window restriction)
        "entry_cutoff"   : "13:45",  # no new entries after this
        "sq_off_time"    : "14:00",  # hard SQ OFF for open trades
        "entry_windows"  : None,     # None = morning hold (any time before cutoff)
    },
    "Sensex": {
        "label"          : "Sensex",
        "strike_step"    : 100,
        "expiry_fn"      : lambda ref: _next_weekday(ref, 3),
        "symbol_prefix"  : "SENSEX",
        "exchange"       : "BSE_FO",
        "key_prefix"     : "SENSEX",
        "numeric_keys"   : True,
        "gap_itm_near"   : 300,
        "gap_itm_far"    : 600,
        "lot_size"       : 20,    # Sensex weekly lot size
        "zone_far_pts"   : 200,   # Sensex moves faster — need wider threshold
        # Trading time gates (morning hold — backtest shows better PnL than windows)
        "entry_cutoff"   : "13:45",  # no new entries after this
        "sq_off_time"    : "14:00",  # hard SQ OFF for open trades
        "entry_windows"  : None,     # None = morning hold (any time before cutoff)
    },
    "CrudeOil": {
        "label"          : "Crude Oil",
        "strike_step"    : 100,
        "expiry_fn"      : lambda ref: _next_weekday(ref, 0),  # MCX options weekly (Mon)
        "symbol_prefix"  : "CRUDEOIL",
        "exchange"       : "MCX_FO",
        "key_prefix"     : "CRUDEOIL",
        "numeric_keys"   : True,    # MCX uses numeric instrument keys
        "is_mcx"         : True,    # flag: use MCX data path (futures as underlying)
        "futures_key"    : MCX_CRUDE_FUTURES_KEY,
        "gap_itm_near"   : 200,     # 200 ITM on gap day (user-specified)
        "gap_itm_far"    : 400,     # 400 ITM on gap day
        "lot_size"       : 100,     # 100 barrels per lot
        "zone_far_pts"   : 50,
        "session_start"  : MCX_SESSION_START,   # "09:00"
        "session_end"    : MCX_SESSION_END,     # "23:29"
        "squareoff_time" : "23:30",
        # Trading time gates — W2 only (100% win rate in backtest; W1 EIA = 8% win rate, skip)
        "entry_cutoff"   : "22:45",  # no new entries after this
        "sq_off_time"    : "23:00",  # trail remainder to 23:00 after T1 hit
        "entry_windows"  : [         # only enter inside these windows
            (dtime(18, 45), dtime(19, 15)),   # W2 — US open (100% win rate)
        ],
    },
}


MCX_OPEN  = datetime.strptime("09:00", "%H:%M").time()
MCX_CLOSE = datetime.strptime("23:30", "%H:%M").time()

def is_market_open(index: str = "Nifty") -> bool:
    if date.today().weekday() >= 5:
        return False
    now = datetime.now().time()
    if INDEX_CONFIG.get(index, {}).get("is_mcx"):
        return MCX_OPEN <= now <= MCX_CLOSE
    return MARKET_OPEN <= now <= MARKET_CLOSE


def _next_weekday(ref: date, weekday: int) -> date:
    days = (weekday - ref.weekday()) % 7
    return ref + timedelta(days=days or 7)


def round_n(v: float, step: int) -> int:
    return int(round(v / step) * step)


def option_daily_atr(df1: pd.DataFrame, htf_min: int, lookback: int = 10) -> float:
    """
    Average daily High-Low range of the option over last `lookback` trading days.
    Uses the resampled HTF bars so we don't need separate daily candles.
    Returns 0.0 if data is insufficient.
    """
    try:
        from data import resample_tf as _rs
        df_htf = _rs(df1, htf_min)
        if df_htf is None or df_htf.empty:
            return 0.0
        df_htf = df_htf.copy()
        # resample_tf returns reset_index() so datetime is a column, not index
        dt_col = "datetime" if "datetime" in df_htf.columns else df_htf.columns[0]
        df_htf["_date"] = pd.to_datetime(df_htf[dt_col]).dt.date
        daily = df_htf.groupby("_date").agg(h=("high", "max"), l=("low", "min"))
        daily["range"] = daily["h"] - daily["l"]
        today = date.today()
        completed = daily[daily.index < today]
        if completed.empty:
            return 0.0
        return float(completed["range"].tail(lookback).mean())
    except Exception:
        return 0.0


def trading_symbol(strike: int, opt_type: str, expiry: date, prefix: str) -> str:
    return f"{prefix}{expiry.strftime('%d%b%y').upper()}{strike}{opt_type}"


def fallback_key(strike: int, opt_type: str, expiry: date,
                 exchange: str, key_prefix: str) -> str:
    return (f"{exchange}|{key_prefix}"
            f"{expiry.strftime('%y')}{expiry.strftime('%m')}{expiry.strftime('%d')}"
            f"{strike}{opt_type}")


def log_event(log_path: Path, tag: str, msg: str) -> str:
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{tag:15s}] {msg}"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return line


def browser_notify(title: str, body: str):
    js = f"""<script>
    (function(){{
      if(Notification.permission==="granted")
        new Notification("{title}",{{body:"{body}"}});
      else if(Notification.permission!=="denied")
        Notification.requestPermission().then(p=>{{
          if(p==="granted") new Notification("{title}",{{body:"{body}"}});
        }});
    }})();
    </script>"""
    components.html(js, height=0)


def card(col, label: str, value: str, cls: str = ""):
    col.markdown(
        f'<div class="metric-card">'
        f'<div class="metric-label">{label}</div>'
        f'<div class="metric-value {cls}">{value}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def sec(text: str):
    st.markdown(f'<div class="sec-header">{text}</div>', unsafe_allow_html=True)


# ─── Token management ──────────────────────────────────────────────────────────
_ENV_PATH = Path(__file__).parent / ".env"


def _decode_jwt_exp(token: str) -> int | None:
    import base64, json as _json
    try:
        seg = token.split(".")[1]
        seg += "=" * (4 - len(seg) % 4)
        return int(_json.loads(base64.urlsafe_b64decode(seg))["exp"])
    except Exception:
        return None


def _token_valid(token: str) -> bool:
    if not token:
        return False
    exp = _decode_jwt_exp(token)
    return True if exp is None else datetime.now().timestamp() < exp


def _token_expiry_str(token: str) -> str:
    exp = _decode_jwt_exp(token)
    return "unknown" if exp is None else datetime.fromtimestamp(exp).strftime("%d %b %H:%M")


def _save_token_to_env(token: str):
    try:
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines() if _ENV_PATH.exists() else []
        new   = [l for l in lines if not l.startswith("UPSTOX_TOKEN=")]
        new.append(f"UPSTOX_TOKEN={token}")
        _ENV_PATH.write_text("\n".join(new) + "\n", encoding="utf-8")
        os.environ["UPSTOX_TOKEN"] = token
    except Exception:
        pass


def _get_token() -> str:
    if st.session_state.get("live_token"):
        return st.session_state["live_token"]
    t = os.environ.get("UPSTOX_TOKEN", "")
    if t and _token_valid(t):
        st.session_state["live_token"] = t
        return t
    try:
        t = st.secrets.get("UPSTOX_TOKEN", "") or ""
        if t and _token_valid(t):
            st.session_state["live_token"] = t
            return t
    except Exception:
        pass
    return ""


# ─── Cached REST helpers ───────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def _fetch_expiries(index: str, token: str) -> list:
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return get_option_expiries(OPTION_CHAIN_KEYS.get(index, OPTION_CHAIN_KEYS["Nifty"]), h)


@st.cache_data(ttl=300)
def _fetch_spot(token: str, index: str = "Nifty"):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return fetch_spot_prev_day(h, index)


@st.cache_data(ttl=60)
def _fetch_today_open(token: str, index: str = "Nifty") -> float | None:
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return fetch_today_open(h, index)


@st.cache_data(ttl=300)
def _fetch_key(strike: int, opt_type: str, expiry_str: str,
               token: str, index: str = "Nifty"):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return get_instrument_key(strike, opt_type, expiry_str, h, index)


@st.cache_data(ttl=3600)
def _fetch_hist(key: str, from_date: str, to_date: str, token: str):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return fetch_1min(key, from_date, to_date, h)


@st.cache_data(ttl=3600)
def _fetch_mcx_spot(token: str):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return fetch_mcx_prev_day(h)


@st.cache_data(ttl=300)
def _fetch_mcx_today_open(token: str):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return fetch_mcx_today_open(h)


@st.cache_data(ttl=1800)
def _fetch_mcx_chain(token: str) -> dict:
    """Returns {strike: {"CE": key, "PE": key}} for active MCX CrudeOil options."""
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return get_mcx_option_chain(MCX_CRUDE_FUTURES_KEY, h)


@st.cache_data(ttl=1800)
def _fetch_mcx_expiries(token: str) -> list:
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return get_mcx_option_expiries(MCX_CRUDE_FUTURES_KEY, h)


def _get_ltp(df1: pd.DataFrame) -> float | None:
    if df1 is None or df1.empty:
        return None
    today_bars = df1[df1.index.date == date.today()]
    if not today_bars.empty:
        return round(float(today_bars["close"].iloc[-1]), 2)
    return round(float(df1["close"].iloc[-1]), 2)


# ─── Bid-ask spread gate ───────────────────────────────────────────────────────
MAX_SPREAD_PCT: float = 3.0   # reject order if (ask-bid)/mid > this %

def check_bid_ask_spread(
    instrument_key: str,
    token: str,
    max_pct: float = MAX_SPREAD_PCT,
) -> tuple[bool, float, float, float]:
    """
    Fetch live market depth for instrument_key via Upstox v2 quotes.
    Returns (ok, spread_pct, best_bid, best_ask).
    ok=True  → spread acceptable, proceed with order.
    ok=False → spread too wide, skip order.
    On API error returns (True, 0, 0, 0) — fail-open so a network glitch doesn't block.
    """
    from config import UPSTOX_BASE
    import requests
    try:
        url  = f"{UPSTOX_BASE}/market-quote/quotes"
        h    = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        resp = requests.get(url, params={"instrument_key": instrument_key},
                            headers=h, timeout=5)
        body = resp.json()
        data = (body.get("data") or {}).get(instrument_key.replace("|", "_")) or {}
        if not data:
            # try alternate key format (Upstox sometimes uses | in response key)
            for v in (body.get("data") or {}).values():
                data = v; break
        depth = data.get("depth") or {}
        bids  = depth.get("buy")  or []
        asks  = depth.get("sell") or []
        best_bid = float(bids[0]["price"]) if bids and bids[0]["price"] else 0.0
        best_ask = float(asks[0]["price"]) if asks and asks[0]["price"] else 0.0
        if best_bid <= 0 or best_ask <= 0:
            return True, 0.0, best_bid, best_ask  # no depth data — fail open
        mid        = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / mid * 100
        ok         = spread_pct <= max_pct
        return ok, round(spread_pct, 2), best_bid, best_ask
    except Exception:
        return True, 0.0, 0.0, 0.0   # fail-open on any error


# ─── Morning scan (one-time init per session) ──────────────────────────────────
@st.cache_data(ttl=3600)
def morning_scan(strike: int, opt_type: str, expiry_api: str,
                 htf_min: int, token: str, index: str = "Nifty"):
    """
    Fetch prev-week + current-week 1-min data.
    Run HTF scan. Return TRAPPED (open) zones + raw data.
    Cached for 1 hour so repeated sidebar changes don't re-fetch.
    """
    h   = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    cfg = INDEX_CONFIG[index]

    raw_key, chain_err = _fetch_key(strike, opt_type, expiry_api, token, index)
    if chain_err or not raw_key:
        if cfg.get("numeric_keys"):
            key = None
        else:
            expiry = datetime.strptime(expiry_api, "%Y-%m-%d").date()
            key    = fallback_key(strike, opt_type, expiry, cfg["exchange"], cfg["key_prefix"])
    else:
        key = raw_key

    today           = date.today()
    monday_this     = today - timedelta(days=today.weekday())
    monday_prev     = monday_this - timedelta(weeks=1)
    from_date       = monday_prev.strftime("%Y-%m-%d")
    to_date         = today.strftime("%Y-%m-%d")

    dbg = {"key": key or "NONE", "from": from_date, "to": to_date,
           "rows_1min": 0, "rows_htf": 0, "open_traps": 0, "fetch_err": None}

    if key is None:
        return [], key, None, from_date, dbg

    df1, err = _fetch_hist(key, from_date, to_date, token)
    dbg["fetch_err"] = str(err) if err else None
    if err or df1 is None or df1.empty:
        return [], key, None, from_date, dbg

    dbg["rows_1min"] = len(df1)
    df_htf = resample_tf(df1, htf_min)
    dbg["rows_htf"] = len(df_htf)

    if df_htf.empty:
        return [], key, df1, from_date, dbg

    _, entries  = scan_htf(df_htf)
    open_traps  = [e for e in entries if e["status"] == "TRAPPED"]
    dbg["open_traps"] = len(open_traps)
    return open_traps, key, df1, from_date, dbg


# ─── Intraday morning scan: TODAY's data only (for gap-day 15-min cascade) ─────
@st.cache_data(ttl=120)
def morning_scan_intraday(strike: int, opt_type: str, expiry_api: str,
                          token: str, index: str = "Nifty"):
    """
    Fetch TODAY's live intraday 1-min bars via Upstox intraday endpoint,
    resample to 15-min, scan for TRAPPED zones formed today only.
    Uses /historical-candle/intraday/{key}/1minute — returns live session bars.
    Cached 2 min so new zones forming during the session are picked up.
    """
    h   = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    cfg = INDEX_CONFIG[index]

    raw_key, chain_err = _fetch_key(strike, opt_type, expiry_api, token, index)
    if chain_err or not raw_key:
        if cfg.get("numeric_keys"):
            key = None
        else:
            expiry = datetime.strptime(expiry_api, "%Y-%m-%d").date()
            key    = fallback_key(strike, opt_type, expiry, cfg["exchange"], cfg["key_prefix"])
    else:
        key = raw_key

    today     = date.today()
    today_str = today.strftime("%Y-%m-%d")

    dbg = {"key": key or "NONE", "endpoint": "intraday",
           "rows_1min": 0, "rows_htf15": 0, "open_traps_today": 0, "fetch_err": None}

    if key is None:
        return [], key, None, today_str, dbg

    # Use intraday endpoint for live session bars — historical endpoint
    # only returns completed prior-day data, not today's live candles.
    df1, err = fetch_1min_intraday(key, h)
    dbg["fetch_err"] = str(err) if err else None
    if err or df1 is None or df1.empty:
        return [], key, None, today_str, dbg

    dbg["rows_1min"] = len(df1)
    df15 = resample_tf(df1, 15)
    dbg["rows_htf15"] = len(df15)

    if df15.empty:
        return [], key, df1, today_str, dbg

    _, entries = scan_htf(df15)
    # Hard filter: only zones that TRAPPED today
    open_traps = [
        e for e in entries
        if e["status"] == "TRAPPED"
        and pd.Timestamp(e["trapped_on"]).date() == today
    ]
    dbg["open_traps_today"] = len(open_traps)
    return open_traps, key, df1, today_str, dbg


# ─── MCX CrudeOil morning scan ─────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def morning_scan_mcx(strike: int, opt_type: str, expiry_api: str, token: str):
    """
    MCX CrudeOil version of morning_scan — same concept as Nifty/Sensex:
    HTF scan runs on the OPTION's own 1-min bars (not futures).
    Uses MCX session (09:00-23:29) for resampling so overnight bars are included.
    Returns (open_traps, opt_key, df1_opt, from_date, dbg).
    """
    h   = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    cfg = INDEX_CONFIG["CrudeOil"]
    ss  = cfg["session_start"]
    se  = cfg["session_end"]
    htf_min = 15   # 15-min HTF for CrudeOil (same as intraday mode for Sensex/Nifty)

    today       = date.today()
    monday_this = today - timedelta(days=today.weekday())
    monday_prev = monday_this - timedelta(weeks=1)
    from_date   = monday_prev.strftime("%Y-%m-%d")
    to_date     = today.strftime("%Y-%m-%d")

    dbg = {"opt_key": "NONE", "fut_key": MCX_CRUDE_FUTURES_KEY,
           "from": from_date, "to": to_date,
           "rows_1min_fut": 0, "rows_1min_opt": 0,
           "rows_htf": 0, "open_traps": 0, "fetch_err": None}

    # ── Option key lookup ──────────────────────────────────────────────────────
    chain   = _fetch_mcx_chain(token)
    opt_key = (chain.get(strike) or {}).get(opt_type)
    if not opt_key:
        dbg["fetch_err"] = f"Strike {strike}{opt_type} not in MCX chain"
        return [], None, None, from_date, dbg
    dbg["opt_key"] = opt_key

    # ── Fetch FUTURES 1-min bars for HTF zone detection ───────────────────────
    # CrudeOil: institutions move price via futures → futures bars show real traps
    df1_fut, err = fetch_1min(MCX_CRUDE_FUTURES_KEY, from_date, to_date, h)
    if err or df1_fut is None or df1_fut.empty:
        dbg["fetch_err"] = f"Futures bars error: {err}"
        return [], opt_key, None, from_date, dbg
    if df1_fut.index.tz is not None:
        df1_fut = df1_fut.copy(); df1_fut.index = df1_fut.index.tz_localize(None)
    dbg["rows_1min_fut"] = len(df1_fut)

    # ── HTF scan on FUTURES bars ───────────────────────────────────────────────
    df_htf = resample_tf(df1_fut, htf_min, session_start=ss, session_end=se)
    dbg["rows_htf"] = len(df_htf)
    if df_htf.empty:
        return [], opt_key, None, from_date, dbg

    _, entries = scan_htf(df_htf)
    open_traps = [e for e in entries if e["status"] == "TRAPPED"]
    dbg["open_traps"] = len(open_traps)

    # ── Fetch OPTION 1-min bars (for WS seeding + LTF scan) ──────────────────
    df1_opt, err2 = fetch_1min(opt_key, from_date, to_date, h)
    if err2 or df1_opt is None or df1_opt.empty:
        dbg["fetch_err"] = f"Option bars error: {err2}"
        return open_traps, opt_key, None, from_date, dbg
    if df1_opt.index.tz is not None:
        df1_opt = df1_opt.copy(); df1_opt.index = df1_opt.index.tz_localize(None)
    dbg["rows_1min_opt"] = len(df1_opt)

    return open_traps, opt_key, df1_opt, from_date, dbg


@st.cache_data(ttl=120)
def morning_scan_mcx_intraday(strike: int, opt_type: str, token: str):
    """
    MCX intraday version: today's live OPTION bars for 15-min zone detection.
    Same concept as Nifty/Sensex — HTF scan runs on the option's own bars.
    Returns (open_traps_today, opt_key, df1_opt_intraday, today_str, dbg).
    """
    h   = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    cfg = INDEX_CONFIG["CrudeOil"]
    ss  = cfg["session_start"]
    se  = cfg["session_end"]
    today     = date.today()
    today_str = today.strftime("%Y-%m-%d")
    dbg = {"endpoint": "intraday_mcx", "rows_1min": 0, "rows_htf15": 0,
           "open_traps_today": 0, "fetch_err": None}

    # Option key
    chain   = _fetch_mcx_chain(token)
    opt_key = (chain.get(strike) or {}).get(opt_type)
    if not opt_key:
        dbg["fetch_err"] = f"Strike {strike}{opt_type} not in MCX chain"
        return [], None, None, today_str, dbg

    # Futures intraday bars for HTF zone detection
    df1_fut, err = fetch_1min_intraday(MCX_CRUDE_FUTURES_KEY, h)
    if err or df1_fut is None or df1_fut.empty:
        dbg["fetch_err"] = str(err)
        return [], opt_key, None, today_str, dbg
    if df1_fut.index.tz is not None:
        df1_fut = df1_fut.copy(); df1_fut.index = df1_fut.index.tz_localize(None)

    dbg["rows_1min"] = len(df1_fut)
    df15 = resample_tf(df1_fut, 15, session_start=ss, session_end=se)
    dbg["rows_htf15"] = len(df15)
    if df15.empty:
        return [], opt_key, None, today_str, dbg

    _, entries = scan_htf(df15)
    open_traps = [
        e for e in entries
        if e["status"] == "TRAPPED"
        and pd.Timestamp(e["trapped_on"]).date() == today
    ]
    dbg["open_traps_today"] = len(open_traps)

    # Option intraday bars (for WS seeding + LTF scan)
    df1_opt, err2 = fetch_1min_intraday(opt_key, h)
    if err2 or df1_opt is None or df1_opt.empty:
        return open_traps, opt_key, None, today_str, dbg
    if df1_opt.index.tz is not None:
        df1_opt = df1_opt.copy(); df1_opt.index = df1_opt.index.tz_localize(None)

    return open_traps, opt_key, df1_opt, today_str, dbg


# ─── Cascade simulation: 15-min → 5-min → 1-min on historical data ────────────
def _run_cascade_simulation(htf_entries: list, df1: pd.DataFrame,
                             sl_buffer: float, lot_size: int, qty: int = 2,
                             sim_date=None,
                             min_entry_time: str = "09:30",
                             max_entry_time: str | None = None,
                             trail_after_t1: bool = True,
                             session_start: str | None = None,
                             session_end: str | None = None) -> list:
    """
    Simulate the 2-tier intraday cascade on historical 1-min data.

    For each 15-min TRAPPED zone on sim_date (default = today):
      Tier 2 : scan 5-min bars inside zone after 15-min trap → find 5-min TRAPPED
      Entry  : 5-min zone_high
      SL     : 5-min zone_low - sl_buffer
      T1     : 15-min HTF target (zone.sl = ref bar HIGH)
    Then simulates forward on 1-min bars.

    Tweaks
    ------
    min_entry_time : ignore 5-min traps that formed before this time (e.g. "09:30")
                     eliminates opening-range noise in first 15 minutes
    max_entry_time : skip entries at or after this time (e.g. "13:00" on expiry day)
    trail_after_t1 : after T1 hit, trail SL to highest low of subsequent 5-min bars
                     above the original SL — locks in profit on the runner lot
    """
    from datetime import date as _date
    target_date = sim_date if sim_date is not None else _date.today()
    units     = qty * lot_size
    t1_units  = units // 2
    rem_units = units - t1_units
    trades    = []

    if df1 is None or df1.empty:
        return trades

    # Normalise df1 to tz-naive DatetimeIndex
    df1_w = df1.copy()
    if df1_w.index.tz is not None:
        df1_w.index = df1_w.index.tz_localize(None)

    def to_scan(df_idx: pd.DataFrame) -> pd.DataFrame:
        d = df_idx.reset_index()
        d.columns = ["datetime"] + list(d.columns[1:])
        return d

    df5_all = resample_tf(df1_w, 5, session_start=session_start, session_end=session_end)

    htf_today = [
        e for e in htf_entries
        if e["status"] in ("TRAPPED", "CLOSED")
        and e.get("trapped_on")
        and pd.Timestamp(e["trapped_on"]).date() == target_date
    ]

    entered_at: set = set()

    for htf_e in htf_today:
        zh  = htf_e["zone_high"]
        zl  = htf_e["zone_low"]
        tgt = round(htf_e["sl"], 2)

        trap_ts = pd.Timestamp(htf_e["trapped_on"])
        if trap_ts.tzinfo is not None:
            trap_ts = trap_ts.tz_localize(None)

        df5_after = df5_all[df5_all["datetime"] >= trap_ts].reset_index(drop=True)
        if len(df5_after) < 2:
            continue

        _, entries_5m = scan_ltf(df5_after, zh, zl)
        trapped_5m = [e for e in entries_5m if e["status"] == "TRAPPED"]

        for e5 in trapped_5m:
            e5_zh = e5["zone_high"]
            e5_zl = e5["zone_low"]
            e5_ts = pd.Timestamp(e5["trapped_on"])
            if e5_ts.tzinfo is not None:
                e5_ts = e5_ts.tz_localize(None)

            # ── Time filters ──────────────────────────────────────────────────
            if min_entry_time:
                mh, mm = map(int, min_entry_time.split(":"))
                if (e5_ts.hour, e5_ts.minute) < (mh, mm):
                    continue   # opening-range noise — skip
            if max_entry_time:
                xh, xm = map(int, max_entry_time.split(":"))
                if (e5_ts.hour, e5_ts.minute) >= (xh, xm):
                    continue   # too late in session — skip

            entry_price = round(e5_zh, 2)
            sl_price    = round(e5_zl - sl_buffer, 2)
            entry_ts    = e5_ts

            if entry_ts in entered_at:
                continue
            entered_at.add(entry_ts)

            # ── Simulate forward on 1-min bars ─────────────────────────────────
            df1_fwd = df1_w[df1_w.index > entry_ts]
            t1_hit       = False
            trail_sl     = sl_price   # updated after T1 when trail_after_t1=True
            result       = "OPEN"
            exit_price   = None
            exit_time    = None
            pnl          = 0

            # For trailing SL: track last completed 5-min bar
            last_5m_ts   = entry_ts

            for bar_ts, bar in df1_fwd.iterrows():
                # Update trailing SL after T1 hit
                if t1_hit and trail_after_t1:
                    # Advance to next 5-min bar boundary when one completes
                    bar_5m_ts = bar_ts.floor("5min")
                    if bar_5m_ts > last_5m_ts:
                        last_5m_ts = bar_5m_ts
                        # Find the completed 5-min bar just before this boundary
                        prior_5m = df5_all[df5_all["datetime"] < bar_ts]
                        if not prior_5m.empty:
                            candidate_sl = round(prior_5m.iloc[-1]["low"] - sl_buffer, 2)
                            # Only trail UP (never lower the SL)
                            if candidate_sl > trail_sl:
                                trail_sl = candidate_sl

                active_sl = trail_sl if t1_hit else sl_price

                if bar["low"] <= active_sl:
                    if not t1_hit:
                        result = "SL_HIT"; exit_price = active_sl; exit_time = bar_ts
                        pnl = round((active_sl - entry_price) * units, 2)
                    else:
                        result = "T1+SL"; exit_price = active_sl; exit_time = bar_ts
                        pnl = round((tgt - entry_price) * t1_units
                                    + (active_sl - entry_price) * rem_units, 2)
                    break
                if not t1_hit and bar["high"] >= tgt:
                    t1_hit = True
            else:
                if not df1_fwd.empty:
                    last = df1_fwd.iloc[-1]
                    exit_price = last["close"]; exit_time = df1_fwd.index[-1]
                    if t1_hit:
                        result = "T1+RUNNING"
                        pnl = round((tgt - entry_price) * t1_units
                                    + (exit_price - entry_price) * rem_units, 2)
                    else:
                        result = "SQ_OFF"
                        pnl = round((exit_price - entry_price) * units, 2)

            trades.append({
                "htf_zone"   : f"{zl:.0f}→{zh:.0f}",
                "entry_price": entry_price,
                "entry_time" : entry_ts,
                "sl"         : sl_price,
                "t1"         : tgt,
                "result"     : result,
                "exit_price" : exit_price,
                "exit_time"  : exit_time,
                "pnl"        : pnl,
                "units"      : units,
            })

    return sorted(trades, key=lambda t: t["entry_time"])


# ─── LTF scan (called every 2 s from fragment) ─────────────────────────────────
def live_ltf_scan(open_traps: list, df1: pd.DataFrame, ltf_min: int,
                  sl_buffer: float, sym: str, log_path: Path,
                  opt_type: str = "", spot_ltp: float = 0,
                  expiry=None, lot_size: int = 20, qty: int = 2,
                  strike: int | None = None, exchange: str = "BSE",
                  index_name: str = "Sensex",
                  upstox_key: str = "", upstox_token: str = "",
                  entry_cutoff: str | None = "13:45",
                  entry_windows: list | None = None,
                  strike_step: int = 100):
    if df1 is None or df1.empty or not open_traps:
        return [], []

    df_ltf = resample_tf(df1, ltf_min)
    if df_ltf.empty:
        return [], []

    entry_hits  = []
    ltf_alerts  = []
    notified    = st.session_state.get("notified_setups", set())

    for htf_e in open_traps:
        zh       = htf_e["zone_high"]
        zl       = htf_e["zone_low"]
        tgt      = htf_e["sl"]
        trap_ts  = pd.Timestamp(htf_e["trapped_on"])
        # Normalize tz: historical HTF scan produces tz-aware timestamps;
        # ws_feed candles are tz-stripped — make them comparable.
        if trap_ts.tzinfo is not None:
            trap_ts = trap_ts.tz_convert(None)
        df_after = df_ltf[df_ltf["datetime"] >= trap_ts].copy().reset_index(drop=True)
        if len(df_after) < 2:
            continue

        ref_ts_raw   = pd.Timestamp(htf_e["ref_ts"])
        if ref_ts_raw.tzinfo is not None:
            ref_ts_raw = ref_ts_raw.tz_convert(None)
        htf_ref_lbl  = ref_ts_raw.strftime("%d %b %y %H:%M")
        htf_trap_lbl = trap_ts.strftime("%d %b %y %H:%M")

        _, ltf_entries = scan_ltf(
            df_after, zh, zl,
            htf_ref_bar=htf_ref_lbl, htf_trap_bar=htf_trap_lbl, htf_target=tgt,
        )

        _today_date = date.today()
        # Only today's LTF entries are valid — a closed_on from a previous day
        # means the signal already fired yesterday; don't re-trigger it today.
        closed_ltf  = [e for e in ltf_entries
                       if e["status"] == "CLOSED"
                       and e.get("closed_on")
                       and pd.Timestamp(e["closed_on"]).date() == _today_date]
        trapped_ltf = [e for e in ltf_entries if e["status"] == "TRAPPED"]

        if closed_ltf:
            lowest = min(closed_ltf, key=lambda e: e["zone_low"])

            # ── Trading window / cutoff gate ───────────────────────────────────
            _entry_ts = pd.Timestamp(lowest["closed_on"]).replace(tzinfo=None)
            _entry_t  = _entry_ts.time()

            if entry_cutoff:
                _co_h, _co_m = map(int, entry_cutoff.split(":"))
                if (_entry_t.hour, _entry_t.minute) > (_co_h, _co_m):
                    continue  # past entry cutoff — no new positions

            if entry_windows:
                _in_window = any(ws <= _entry_t <= we for ws, we in entry_windows)
                if not _in_window:
                    continue  # not in any active entry window — wait

            # ── 1-ITM strike adjustment ────────────────────────────────────────
            # 1-ITM = 1 strike step ITM from current spot ATM (not from pivot strike)
            _use_1itm   = st.session_state.get("use_1itm", False)
            live_strike = strike
            if _use_1itm and spot_ltp and spot_ltp > 0 and strike_step:
                _atm = round_n(spot_ltp, strike_step)
                if opt_type == "CE":
                    live_strike = _atm - strike_step   # 1 step ITM below spot
                elif opt_type == "PE":
                    live_strike = _atm + strike_step   # 1 step ITM above spot

            # Derive 1-ITM Upstox key and subscribe WS so P&L tracks the actual order strike
            _live_key = upstox_key   # default: same as scan strike
            _live_sym = sym          # default: same symbol
            if _use_1itm and live_strike != strike and expiry:
                _cfg         = INDEX_CONFIG.get(index_name, {})
                _upstox_exch = upstox_key.split("|")[0] if "|" in upstox_key else ""
                _key_pfx     = _cfg.get("key_prefix", "SENSEX")
                _sym_pfx     = _cfg.get("symbol_prefix", "SENSEX")
                if _upstox_exch:
                    _live_key = fallback_key(live_strike, opt_type, expiry, _upstox_exch, _key_pfx)
                    _live_sym = trading_symbol(live_strike, opt_type, expiry, _sym_pfx)
                    if not ws_feed.get_ltp(_live_key):
                        ws_feed.add_keys([_live_key], {})

            uid    = f"{sym}_{lowest['closed_on']}"
            setup  = {
                "uid"         : uid,
                "sym"         : sym,
                "status"      : "ENTRY_HIT",
                "entry_price" : round(lowest["entry"], 2),
                "entry_ts"    : lowest["closed_on"],
                "sl"          : round(lowest["zone_low"] - sl_buffer, 2),
                "target"      : round(tgt, 2),
                "ltf_zone_high": round(lowest["zone_high"], 2),
                "ltf_zone_low" : round(lowest["zone_low"],  2),
                "htf_zone_high": round(zh, 2),
                "htf_zone_low" : round(zl, 2),
                "live_strike"  : live_strike,
                "live_key"     : _live_key,
                "live_sym"     : _live_sym,
            }
            entry_hits.append(setup)
            if uid not in notified:
                _itm_tag = f" [1-ITM {live_strike}]" if _use_1itm and live_strike != strike else ""
                log_event(log_path, "TRADE_ENTRY",
                          f"{_live_sym} BUY@{lowest['entry']:.2f} SL{setup['sl']:.2f} T{tgt:.2f}{_itm_tag}")
                notified.add(uid)
                browser_notify(f"ENTRY: {_live_sym}",
                               f"BUY @ {lowest['entry']:.2f} | SL {setup['sl']:.2f}{_itm_tag}")
                # ── Trade log (paper or live) ─────────────────────────────────
                if opt_type and spot_ltp and expiry:
                    _live_set = st.session_state.get("live_scripts", set())
                    _is_live  = index_name in _live_set
                    # Bid-ask spread gate — check actual order strike
                    _spread_ok   = True
                    _spread_info = ""
                    if _is_live and _live_key and upstox_token:
                        _max_sp = st.session_state.get("max_spread_pct", MAX_SPREAD_PCT)
                        _spread_ok, _spct, _bid, _ask = check_bid_ask_spread(
                            _live_key, upstox_token, max_pct=_max_sp
                        )
                        if not _spread_ok:
                            _spread_info = (f"Bid:{_bid} Ask:{_ask} "
                                            f"Spread:{_spct:.1f}% > {_max_sp}%")
                            log_event(log_path, "SKIPPED_WIDE_SPREAD",
                                      f"{_live_sym} {_spread_info}")
                            angel_orders._log(
                                f"SPREAD REJECTED [{_live_sym}] -- {_spread_info} -- order NOT placed"
                            )
                            _rej = st.session_state.get("_spread_rejected", [])
                            _rej_entry = {"sym": _live_sym, "info": _spread_info,
                                          "time": datetime.now().strftime("%H:%M:%S")}
                            if not any(r["sym"] == _live_sym for r in _rej):
                                _rej.append(_rej_entry)
                            st.session_state["_spread_rejected"] = _rej
                    if _spread_ok:
                        angel_orders.log_entry(
                            side           = opt_type,
                            spot_ltp       = spot_ltp,
                            expiry         = expiry,
                            qty            = qty * lot_size,
                            sl_price       = setup["sl"],
                            target_price   = setup["target"],
                            tracked_sym    = upstox_key,   # scan strike Upstox key for SL/T1 monitoring
                            signal_src     = f"5-min retest zone {setup['ltf_zone_low']:.0f}->{setup['ltf_zone_high']:.0f}",
                            lots           = qty,
                            lot_size       = lot_size,
                            strike         = live_strike,
                            exchange       = exchange,
                            index_name     = index_name,
                            product_type   = st.session_state.get("angel_product_type", "CARRYFORWARD"),
                            paper_override = not _is_live,
                            live_key       = _live_key,    # exec strike key (1-ITM or same as scan)
                        )

        elif trapped_ltf:
            lowest = min(trapped_ltf, key=lambda e: e["zone_low"])
            uid    = f"alert_{sym}_{lowest['trapped_on']}"
            setup  = {
                "uid"         : uid,
                "sym"         : sym,
                "status"      : "WAITING_RETEST",
                "entry_price" : round(lowest["entry"], 2),
                "sl"          : round(lowest["zone_low"] - sl_buffer, 2),
                "target"      : round(tgt, 2),
                "ltf_bear_sl" : round(lowest["sl"], 2),
                "ltf_zone_high": round(lowest["zone_high"], 2),
                "ltf_zone_low" : round(lowest["zone_low"],  2),
                "htf_zone_high": round(zh, 2),
                "htf_zone_low" : round(zl, 2),
            }
            ltf_alerts.append(setup)
            if uid not in notified:
                log_event(log_path, "LTF_ALERT",
                          f"{sym} Bears TRAPPED@{lowest['sl']:.2f} Watch{lowest['entry']:.2f}")
                notified.add(uid)
                browser_notify(f"ALERT: {sym}",
                               f"5-min bears trapped. Watch retest to {lowest['entry']:.2f}")

    st.session_state["notified_setups"] = notified
    return entry_hits, ltf_alerts


# ─── Intraday scan: 15-min HTF → 5-min MTF → 1-min entry (gap day fallback) ───
def live_intraday_scan(open_traps_15m: list, df1: pd.DataFrame,
                       sl_buffer: float, sym: str, log_path: Path,
                       opt_type: str = "", spot_bias_val: str = "NEUTRAL",
                       strike: int | None = None, exchange: str = "BSE",
                       index_name: str = "Sensex",
                       upstox_key: str = "", upstox_token: str = ""):
    """
    2-tier cascade for gap days when no fresh 75-min trap exists.
      Tier 1 : 15-min zone  (open_traps_15m)
      Tier 2 : 5-min TRAPPED inside 15-min zone → immediate ENTRY
    Entry price = 5-min zone_high (bear's SL level).
    SL          = 5-min zone_low − sl_buffer.
    Target      = 15-min HTF target (sl field of 15-min trap).
    spot_bias_val : "BULLISH" / "BEARISH" / "NEUTRAL" — filters by spot direction.
    """
    if df1 is None or df1.empty or not open_traps_15m:
        return [], []

    notified = st.session_state.get("notified_setups", set())

    # Prepare scan-compatible df (DatetimeIndex → datetime column, tz-stripped)
    def to_scan_df(df_in: pd.DataFrame) -> pd.DataFrame:
        d = df_in.copy().reset_index()
        col = "dt" if "dt" in d.columns else d.columns[0]
        d = d.rename(columns={col: "datetime"})
        if d["datetime"].dt.tz is not None:
            d["datetime"] = d["datetime"].dt.tz_localize(None)
        return d

    df5 = resample_tf(df1, 5)
    if df5.empty:
        return [], []

    df1_scan = to_scan_df(df1)

    entry_hits = []
    mtf_alerts = []

    for htf_e in open_traps_15m:
        zh  = htf_e["zone_high"]
        zl  = htf_e["zone_low"]
        tgt = htf_e["sl"]

        trap_ts = pd.Timestamp(htf_e["trapped_on"])
        if trap_ts.tzinfo is not None:
            trap_ts = trap_ts.tz_localize(None)

        # ── Tier 2: 5-min scan inside 15-min zone ─────────────────────────────
        df5_after = df5[df5["datetime"] >= trap_ts].copy().reset_index(drop=True)
        if len(df5_after) < 2:
            continue

        _, entries_5m = scan_ltf(df5_after, zh, zl,
                                  htf_ref_bar=str(htf_e.get("ref_ts", "")),
                                  htf_trap_bar=str(trap_ts),
                                  htf_target=tgt)
        trapped_5m = [e for e in entries_5m if e["status"] in ("TRAPPED", "CLOSED")]

        if not trapped_5m:
            continue

        # ── Tier 2 entry: 5-min TRAPPED → immediate entry (no 1-min required) ──
        for e5 in trapped_5m:
            e5_zh = e5["zone_high"]
            e5_zl = e5["zone_low"]
            e5_ts = pd.Timestamp(e5["trapped_on"])
            if e5_ts.tzinfo is not None:
                e5_ts = e5_ts.tz_localize(None)

            uid = f"intra_{sym}_{e5['trapped_on']}"
            setup = {
                "uid"         : uid,
                "sym"         : sym,
                "status"      : "ENTRY_HIT",
                "entry_price" : round(e5_zh, 2),
                "entry_ts"    : e5["trapped_on"],
                "sl"          : round(e5_zl - sl_buffer, 2),
                "target"      : round(tgt, 2),
                "mode"        : "INTRADAY_15_5",
                "ltf_zone_high": round(e5_zh, 2),
                "ltf_zone_low" : round(e5_zl, 2),
                "htf_zone_high": zh,
                "htf_zone_low" : zl,
            }
            entry_hits.append(setup)
            if uid not in notified:
                log_event(log_path, "INTRA_ENTRY",
                          f"{sym} BUY@{e5_zh:.2f} SL{setup['sl']:.2f} T{tgt:.2f}")
                notified.add(uid)
                browser_notify(f"INTRADAY ENTRY: {sym}",
                               f"BUY @ {e5_zh:.2f} | SL {setup['sl']:.2f}")
                # ── Trade log (paper or live) ─────────────────────────────────
                if opt_type and spot_ltp and expiry:
                    _live_set = st.session_state.get("live_scripts", set())
                    _is_live  = index_name in _live_set
                    # Bid-ask spread gate (only for live orders)
                    _spread_ok   = True
                    _spread_info = ""
                    if _is_live and upstox_key and upstox_token:
                        _max_sp = st.session_state.get("max_spread_pct", MAX_SPREAD_PCT)
                        _spread_ok, _spct, _bid, _ask = check_bid_ask_spread(
                            upstox_key, upstox_token, max_pct=_max_sp
                        )
                        if not _spread_ok:
                            _spread_info = (f"Bid:{_bid} Ask:{_ask} "
                                            f"Spread:{_spct:.1f}% > {_max_sp}%")
                            log_event(log_path, "SKIPPED_WIDE_SPREAD",
                                      f"{sym} {_spread_info}")
                            angel_orders._log(
                                f"SPREAD REJECTED [{sym}] — {_spread_info} — order NOT placed"
                            )
                            _rej = st.session_state.get("_spread_rejected", [])
                            _rej_entry = {"sym": sym, "info": _spread_info,
                                          "time": datetime.now().strftime("%H:%M:%S")}
                            if not any(r["sym"] == sym for r in _rej):
                                _rej.append(_rej_entry)
                            st.session_state["_spread_rejected"] = _rej
                    if _spread_ok:
                        angel_orders.log_entry(
                            side           = opt_type,
                            spot_ltp       = spot_ltp,
                            expiry         = expiry,
                            qty            = qty * lot_size,
                            sl_price       = setup["sl"],
                            target_price   = setup["target"],
                            tracked_sym    = upstox_key,   # Upstox key for SL/T1 monitoring
                            signal_src     = f"15m→5m cascade zone {e5_zl:.0f}→{e5_zh:.0f}",
                            lots           = qty,
                            lot_size       = lot_size,
                            strike         = strike,
                            exchange       = exchange,
                            index_name     = index_name,
                            product_type   = st.session_state.get("angel_product_type", "CARRYFORWARD"),
                            paper_override = not _is_live,
                        )

        # ── MTF_TRAPPED: placeholder for remaining code refs ──────────────────
        if False:
            uid = f"mtf_{sym}_placeholder"
            if not any(a["uid"] == uid for a in mtf_alerts):
                mtf_alerts.append({
                    "uid"         : uid,
                    "sym"         : sym,
                    "status"      : "MTF_TRAPPED",
                    "entry_price" : 0,
                    "sl"          : 0,
                    "target"      : 0,
                    "e5_zone_high": 0,
                    "e5_zone_low" : 0,
                    "htf_zone_high": zh,
                    "htf_zone_low" : zl,
                })

    st.session_state["notified_setups"] = notified

    # Return only the BEST single entry — highest entry_price = most recent 5-min trap
    # (avoids showing stale early-session zones that fired long before current price)
    if entry_hits:
        entry_hits = [max(entry_hits, key=lambda e: e["entry_price"])]

    # Spot direction filter: suppress entries against spot trend
    if spot_bias_val != "NEUTRAL" and opt_type:
        allowed = "CE" if spot_bias_val == "BULLISH" else "PE"
        if opt_type != allowed:
            entry_hits = []

    return entry_hits, mtf_alerts


# ─── Phase 3 paired trade panel ────────────────────────────────────────────────
def _render_pair_phase3(pair_label: str, ce_sym: str, pe_sym: str,
                        ltf_results: dict, df1_map: dict, log_path: Path,
                        sl_direction_filter: bool = False,
                        idx_name: str = "Nifty"):
    ce_entries, ce_alerts = ltf_results.get(ce_sym, ([], []))
    pe_entries, pe_alerts = ltf_results.get(pe_sym, ([], []))
    ce_hit = bool(ce_entries)
    pe_hit = bool(pe_entries)

    st.markdown(
        f'<div class="zone-box" style="border-left:4px solid #0969DA; margin-bottom:6px;">'
        f'<b>{pair_label}</b> &nbsp;|&nbsp; CE: <b>{ce_sym}</b> &nbsp;↔&nbsp; PE: <b>{pe_sym}</b>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if not ce_hit and not pe_hit:
        col1, col2 = st.columns(2)
        with col1:
            if ce_alerts:
                w = ce_alerts[0]["entry_price"]
                st.markdown(
                    f'<div class="alert-orange">CE WATCH: {ce_sym} — '
                    f'Bears trapped. Retest entry @ <b>{w:.2f}</b></div>',
                    unsafe_allow_html=True,
                )
            else:
                st.caption(f"CE {ce_sym}: No entry yet")
        with col2:
            if pe_alerts:
                w = pe_alerts[0]["entry_price"]
                st.markdown(
                    f'<div class="alert-orange">PE WATCH: {pe_sym} — '
                    f'Bears trapped. Retest entry @ <b>{w:.2f}</b></div>',
                    unsafe_allow_html=True,
                )
            else:
                st.caption(f"PE {pe_sym}: No entry yet")
        return

    if ce_hit and pe_hit:
        ce_ts = pd.Timestamp(ce_entries[0]["entry_ts"])
        pe_ts = pd.Timestamp(pe_entries[0]["entry_ts"])
        if pe_ts >= ce_ts:
            active_side, active, sl_side = "PE", pe_entries[0], "CE"
        else:
            active_side, active, sl_side = "CE", ce_entries[0], "PE"
        sl_was_hit = True
    elif ce_hit:
        active_side, active, sl_side, sl_was_hit = "CE", ce_entries[0], "PE", False
    else:
        active_side, active, sl_side, sl_was_hit = "PE", pe_entries[0], "CE", False

    counterpart_alerts  = pe_alerts if active_side == "CE" else ce_alerts
    counterpart_fired   = pe_entries if active_side == "CE" else ce_entries
    sl_watch_sym        = pe_sym if active_side == "CE" else ce_sym

    sl_watch_price = None
    if counterpart_alerts:
        cp_price = counterpart_alerts[0]["entry_price"]
        if sl_direction_filter and counterpart_fired:
            if cp_price > counterpart_fired[-1]["entry_price"]:
                sl_watch_price = cp_price
        else:
            sl_watch_price = cp_price

    sym        = active["sym"]
    df1        = df1_map.get(sym)
    # Prefer ws_feed LTP directly — more current than df tail
    ws_key     = next((m["key"] for m in st.session_state.get("_contracts", [])
                       if m["sym"] == sym), None)
    ltp = ws_feed.get_ltp(ws_key) if ws_key else None
    if ltp is None:
        ltp = _get_ltp(df1)

    entry      = active["entry_price"]
    target     = active["target"]
    ltf_zl     = active.get("ltf_zone_low", 0)
    hard_floor = round(ltf_zl * 0.97, 2)
    lot_size   = INDEX_CONFIG.get(idx_name, {}).get("lot_size", DEFAULT_LOT_SIZE)
    units      = 2 * lot_size       # e.g. Sensex: 2×20=40, Nifty: 2×75=150
    t1_units   = units // 2
    rem_units  = units - t1_units

    if ltp is not None:
        pnl_full = round((ltp - entry) * units, 2)
        pnl_str  = f"+Rs.{pnl_full:,.0f}" if pnl_full >= 0 else f"-Rs.{abs(pnl_full):,.0f}"
        pnl_cls  = "green" if pnl_full >= 0 else "red"
    else:
        pnl_str, pnl_cls = "Awaiting feed", "orange"

    target_hit     = ltp is not None and ltp >= target
    hard_floor_hit = ltp is not None and ltp < hard_floor
    side_cls       = "green" if active_side == "CE" else "red"
    notified       = st.session_state.get("notified_setups", set())

    cols = st.columns(9)
    card(cols[0], "Side",              active_side,                       side_cls)
    card(cols[1], "Contract",          sym,                               "blue")
    card(cols[2], f"Entry ×2 lots",   f"{entry:.2f}",                   "")
    card(cols[3], "LTP",               f"{ltp:.2f}" if ltp else "--",    "blue")
    card(cols[4], f"P&L ({units}u)",   pnl_str,                          pnl_cls)
    card(cols[5], "T1 Target",         f"{target:.2f} → {t1_units}u",   "green")
    card(cols[6], "Hard Floor SL",     f"{hard_floor:.2f}",              "red")
    card(cols[7], "After T1",          f"{rem_units}u CP-SL/Floor",      "orange")
    card(cols[8], "CP-SL Watch",
         f"{sl_watch_sym} → exit {rem_units}u" if sl_watch_price else f"Watch {sl_watch_sym}",
         "orange")

    if hard_floor_hit:
        msg = (f"HARD FLOOR SL: {sym} | LTP {ltp:.2f} < Floor {hard_floor:.2f} | "
               f"EXIT ALL {rem_units} REMAINING UNITS NOW")
        st.markdown(f'<div class="alert-red">{msg}</div>', unsafe_allow_html=True)
        uid_hf = f"hf_{sym}_{hard_floor}"
        if uid_hf not in notified:
            log_event(log_path, "HARD_FLOOR_SL", msg)
            browser_notify(f"HARD FLOOR SL: {sym}", msg)
            notified.add(uid_hf)
            st.session_state["notified_setups"] = notified

    elif target_hit:
        t1_pnl = round((target - entry) * t1_units, 2)
        msg    = (f"T1 HIT: {sym} | Book {t1_units}u @ {target:.2f} | "
                  f"T1 P&L Rs.{t1_pnl:,.0f} | Hold {rem_units}u CP-SL/Floor")
        st.markdown(f'<div class="alert-green">{msg}</div>', unsafe_allow_html=True)
        uid_t1 = f"t1_{sym}_{target}"
        if uid_t1 not in notified:
            log_event(log_path, "T1_HIT", msg)
            browser_notify(f"T1 TARGET: {sym}", msg)
            notified.add(uid_t1)
            st.session_state["notified_setups"] = notified

        # ── Remaining lots tracker (after T1 booked) ──────────────────────────
        st.markdown("**Remaining position — after T1 exit:**")
        rem_cols = st.columns(5)
        rem_pnl     = round((ltp - entry) * rem_units, 2) if ltp else 0
        rem_pnl_str = f"+Rs.{rem_pnl:,.0f}" if rem_pnl >= 0 else f"-Rs.{abs(rem_pnl):,.0f}"
        rem_col     = "green" if rem_pnl >= 0 else "red"
        card(rem_cols[0], f"Holding",     f"{rem_units}u",                              "blue")
        card(rem_cols[1], "LTP",          f"{ltp:.2f}" if ltp else "--",               "blue")
        card(rem_cols[2], f"Running P&L", rem_pnl_str,                                 rem_col)
        card(rem_cols[3], "Hard Floor",   f"{hard_floor:.2f} → EXIT {rem_units}u",    "red")
        card(rem_cols[4], "CP-SL Watch",
             f"If {sl_watch_sym} > {sl_watch_price:.2f} → EXIT {rem_units}u"
             if sl_watch_price else f"Watch {sl_watch_sym}",
             "orange")

        # Alert if hard floor breached after T1
        if hard_floor_hit:
            msg2 = (f"HARD FLOOR after T1: {sym} | LTP {ltp:.2f} < Floor {hard_floor:.2f} | "
                    f"EXIT remaining {rem_units}u NOW")
            st.markdown(f'<div class="alert-red">{msg2}</div>', unsafe_allow_html=True)
            uid_hf2 = f"hf2_{sym}_{hard_floor}"
            if uid_hf2 not in notified:
                log_event(log_path, "HARD_FLOOR_REM", msg2)
                browser_notify(f"FLOOR SL (rem): {sym}", msg2)
                notified.add(uid_hf2)
                st.session_state["notified_setups"] = notified
        elif sl_watch_price and ltp and ltp <= sl_watch_price:
            msg3 = (f"CP-SL FIRED: {sl_watch_sym} reached {sl_watch_price:.2f} | "
                    f"EXIT {rem_units}u of {sym} NOW")
            st.markdown(f'<div class="alert-red">{msg3}</div>', unsafe_allow_html=True)
            uid_cp = f"cp_{sym}_{sl_watch_price}"
            if uid_cp not in notified:
                log_event(log_path, "CP_SL_FIRED", msg3)
                browser_notify(f"CP-SL: {sym}", msg3)
                notified.add(uid_cp)
                st.session_state["notified_setups"] = notified
        else:
            st.success(f"Running {rem_units}u | LTP {ltp:.2f} | "
                       f"P&L {rem_pnl_str} | Floor @ {hard_floor:.2f} | "
                       f"CP-SL: watch {sl_watch_sym}"
                       + (f" > {sl_watch_price:.2f}" if sl_watch_price else ""))

    else:
        ltp_disp = f"{ltp:.2f}" if ltp else "--"
        st.markdown(
            f'<div class="alert-green">ACTIVE {active_side} (Scenario C): {sym} | '
            f'Entry {entry:.2f} | LTP {ltp_disp} | P&L {pnl_str} | '
            f'T1 @ {target:.2f} ({t1_units}u) | Floor SL: {hard_floor:.2f} | '
            f'CP-SL: {sl_watch_sym}</div>',
            unsafe_allow_html=True,
        )

    if sl_was_hit:
        st.warning(f"{sl_side} fired earlier → prior {active_side} SL triggered. "
                   f"Now tracking new {active_side}.")

    now_ts      = datetime.now()
    entry_ts_dt = pd.Timestamp(active.get("entry_ts", now_ts)).to_pydatetime()
    mins_open   = round((now_ts - entry_ts_dt).total_seconds() / 60, 1)
    reward      = target - entry if target > entry else 1
    progress    = round((ltp - entry) / reward * 100, 1) if ltp else 0

    with st.expander("Shadow Scenarios F/G (display only)", expanded=False):
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            st.metric("Time Open", f"{mins_open} min", delta="/ 60 min max (F)")
            if mins_open >= 60 and (ltp is None or ltp < target):
                st.error("TIME EXIT would fire — F/H would close NOW")
        with sc2:
            st.metric("Progress to T1", f"{progress}%", delta="need 30% @ T+30")
            if mins_open >= 30 and progress < 30 and (ltp is None or ltp < target):
                st.error("PROGRESS EXIT would fire — G/H would close NOW")
        with sc3:
            st.caption("Actual trade stays open until CP-SL or Hard Floor. "
                       "These panels are shadow only.")


# ─── Dashboard HTML table (4 contracts) ────────────────────────────────────────
def _dashboard_table(contracts: list, ltf_results: dict) -> str:
    """
    Columns: CONTRACT | LTP | CHG% | HTF ZONE DETAIL | LTF STATUS
    HTF column shows zone levels + target for the best (most recent) open trap.
    LTF column shows exactly what to watch for, not just a count.
    """
    PAIR_HEADER = {0: "PAIR 1 — S1 CE ↔ R1 PE", 2: "PAIR 2 — S2 CE ↔ R2 PE"}
    rows = []

    for i, meta in enumerate(contracts):
        if i in PAIR_HEADER:
            rows.append(
                f'<tr style="background:#EAF0F6;">'
                f'<td colspan="5" style="padding:5px 14px; font-size:11px; '
                f'font-weight:700; color:#0969DA; letter-spacing:1px;">'
                f'{PAIR_HEADER[i]}</td></tr>'
            )

        sym           = meta["sym"]
        key           = meta["key"]
        lbl           = meta["label"]
        open_traps    = meta.get("open_traps", [])
        intraday_mode = meta.get("intraday_mode", False)
        open_traps_15m= meta.get("open_traps_15m", [])
        # Which zone list to display in HTF column
        display_traps = open_traps_15m if intraday_mode else open_traps

        ltp  = ws_feed.get_ltp(key)
        prev = ws_feed.get_prev_close(key) or meta.get("prev_close", 0)

        # ── LTP cell ───────────────────────────────────────────────────────────
        ltp_str = f"{ltp:,.2f}" if ltp else "—"
        if ltp and prev:
            chg     = ltp - prev
            chg_pct = chg / prev * 100
            arrow   = "▲" if chg >= 0 else "▼"
            l_col   = "#1A7F37" if chg >= 0 else "#CF222E"
            chg_str = f"{'+' if chg >= 0 else ''}{chg_pct:.1f}%"
        else:
            arrow, l_col, chg_str = "", "#57606A", "—"

        # ── HTF zone detail cell ───────────────────────────────────────────────
        htf_n   = len(display_traps)
        tf_label = "15m" if intraday_mode else "75m"
        if intraday_mode:
            mode_badge = '<span style="font-size:10px; background:#BF5B00; color:#fff; ' \
                         'padding:1px 5px; border-radius:3px;">⚡ 15m</span> '
        else:
            mode_badge = ""

        if htf_n == 0:
            htf_html = (
                f'{mode_badge}<span style="color:#57606A;">No open {tf_label} zones</span>'
            )
            h_col = "#57606A"
        else:
            best = display_traps[-1]
            zh   = best["zone_high"]
            zl   = best["zone_low"]
            zt   = best["zone_trigger"]
            tgt  = best["sl"]
            trap_date = pd.Timestamp(best["trapped_on"])
            if trap_date.tzinfo is not None:
                trap_date = trap_date.tz_localize(None)
            trap_lbl = trap_date.strftime("%d %b %I:%M %p")
            extra = f' <span style="color:#57606A; font-size:10px;">(+{htf_n-1} more)</span>' \
                    if htf_n > 1 else ""
            h_col    = "#BF5B00" if intraday_mode else "#0969DA"
            htf_html = (
                f'{mode_badge}<b style="color:{h_col};">'
                f'{htf_n} {tf_label} zone{"s" if htf_n>1 else ""}</b>{extra}<br/>'
                f'<span style="font-size:11px; color:#57606A;">Trapped: {trap_lbl}</span><br/>'
                f'<span style="font-size:11px;">Zone&nbsp;'
                f'<b>{zl:,.0f}</b>&nbsp;→&nbsp;<b>{zh:,.0f}</b></span><br/>'
                f'<span style="font-size:11px; color:#BF5B00;">Entry if LTP&nbsp;&lt;&nbsp;'
                f'<b>{zt:,.0f}</b></span><br/>'
                f'<span style="font-size:11px; color:#1A7F37;">Target:&nbsp;<b>{tgt:,.0f}</b></span>'
            )

        # ── LTF / intraday status cell ─────────────────────────────────────────
        hits, alerts = ltf_results.get(sym, ([], []))

        if hits:
            e       = hits[0]
            ep      = e["entry_price"]
            sl      = e["sl"]
            tgt_e   = e["target"]
            ltf_col = "#1A7F37"
            mode_note = "1-min trap" if intraday_mode else "5-min retest"
            ltf_html = (
                f'<b style="color:#1A7F37;">&#x1F7E2; BUY SIGNAL</b>'
                f'<span style="font-size:10px; color:#57606A;"> ({mode_note})</span><br/>'
                f'<span style="font-size:12px;">Entry:&nbsp;<b>{ep:,.2f}</b></span><br/>'
                f'<span style="font-size:11px; color:#CF222E;">SL:&nbsp;{sl:,.2f}</span>&nbsp;'
                f'<span style="font-size:11px; color:#1A7F37;">T1:&nbsp;{tgt_e:,.2f}</span>'
            )
        elif alerts:
            a = alerts[0]
            if a.get("status") == "MTF_TRAPPED":
                # Intraday: 5-min trapped, waiting for 1-min trap
                e5_zh = a.get("e5_zone_high", a["entry_price"])
                e5_zl = a.get("e5_zone_low",  a["sl"])
                tgt_a = a["target"]
                ltf_col  = "#8250DF"
                ltf_html = (
                    f'<b style="color:#8250DF;">&#x1F7E3; 5-min TRAPPED</b><br/>'
                    f'<span style="font-size:11px;">5m zone {e5_zl:,.0f}→{e5_zh:,.0f}</span><br/>'
                    f'<span style="font-size:12px;">Watch <b>1-min trap</b> inside</span><br/>'
                    f'<span style="font-size:11px; color:#1A7F37;">T1: {tgt_a:,.2f}</span>'
                )
            else:
                watch   = a["entry_price"]
                bear_sl = a.get("ltf_bear_sl", watch)
                ltf_col = "#BF5B00"
                ltf_html = (
                    f'<b style="color:#BF5B00;">&#x1F7E0; BEARS TRAPPED</b><br/>'
                    f'<span style="font-size:11px;">Trapped @ {bear_sl:,.2f}</span><br/>'
                    f'<span style="font-size:12px;">Watch retest:&nbsp;<b>&gt;&nbsp;{watch:,.2f}</b></span>'
                )
        elif htf_n > 0:
            zt_best  = display_traps[-1]["zone_trigger"]
            scan_lbl = "15m trap" if intraday_mode else "5-min trap"
            ltf_col  = "#57606A"
            ltf_html = (
                f'<span style="color:#57606A; font-size:12px;">Monitoring</span><br/>'
                f'<span style="font-size:11px;">{scan_lbl} needed<br/>'
                f'inside zone LTP&nbsp;&lt;&nbsp;<b>{zt_best:,.0f}</b></span>'
            )
        else:
            ltf_col  = "#57606A"
            ltf_html = '<span style="color:#C8D0D9;">No zones — not tracking</span>'

        bg = "#FFFFFF" if i % 2 == 0 else "#F9FAFB"
        rows.append(
            f'<tr style="border-bottom:1px solid #D0D7DE; background:{bg}; vertical-align:top;">'
            # Contract
            f'<td style="padding:10px 14px; font-weight:600; font-size:13px; vertical-align:middle;">'
            f'{lbl}<br/>'
            f'<span style="font-size:10px; color:#57606A; font-weight:400;">{sym}</span>'
            f'</td>'
            # LTP
            f'<td style="padding:10px 14px; text-align:right; font-size:22px; '
            f'font-weight:700; color:{l_col}; vertical-align:middle;">'
            f'{ltp_str}&nbsp;{arrow}</td>'
            # CHG%
            f'<td style="padding:10px 14px; text-align:right; font-size:13px; '
            f'color:{l_col}; vertical-align:middle;">{chg_str}</td>'
            # HTF ZONE DETAIL
            f'<td style="padding:10px 14px; font-size:12px; line-height:1.6; '
            f'vertical-align:top;">{htf_html}</td>'
            # LTF STATUS
            f'<td style="padding:10px 14px; font-size:12px; line-height:1.6; '
            f'vertical-align:top; color:{ltf_col};">{ltf_html}</td>'
            f'</tr>'
        )

    return (
        '<table style="width:100%;border-collapse:collapse;font-family:\'Segoe UI\',sans-serif;">'
        '<thead><tr style="background:#EAF0F6; border-bottom:2px solid #0969DA;">'
        + "".join(
            f'<th style="text-align:{a}; padding:10px 14px; color:#0969DA; '
            f'font-size:11px; letter-spacing:1px; font-weight:700;">{h}</th>'
            for h, a in [
                ("CONTRACT",    "left"),
                ("LTP",         "right"),
                ("CHG%",        "right"),
                ("HTF ZONE",    "left"),
                ("LTF STATUS",  "left"),
            ]
        )
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


# ─── Live fragment (reruns every 2 s) ──────────────────────────────────────────
@st.fragment(run_every=30)
def _live_panel():
    contracts = st.session_state.get("_contracts", [])
    idx_pairs = st.session_state.get("_idx_pairs", [])
    settings  = st.session_state.get("_settings", {})
    log_path  = st.session_state.get("_log_path", LOG_DIR / "live.txt")
    token     = _get_token()

    if not contracts:
        st.info("Initializing... wait a moment or click Force Refresh.")
        return

    ltf_min   = settings.get("ltf_min", LTF_MINUTES)
    sl_buffer = settings.get("sl_buffer", DEFAULT_SL_BUFFER)
    sl_dir    = settings.get("sl_dir", False)
    track_p1  = settings.get("track_pair1", True)
    track_p2  = settings.get("track_pair2", True)
    sel_idx   = settings.get("selected_indices", ["Nifty"])

    now = datetime.now()

    # ── Status bar ─────────────────────────────────────────────────────────────
    ws_on   = ws_feed.is_connected()
    ws_dot  = "🟢" if ws_on else "🔴"
    ws_lbl  = "WS LIVE" if ws_on else "CONNECTING"
    mkt_lbl = "OPEN ●" if is_market_open() else "CLOSED ○"
    mkt_col = "green" if is_market_open() else "red"

    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    c1.markdown(f"**{now.strftime('%H:%M:%S')}**")
    c2.markdown(f"{ws_dot} **{ws_lbl}**")
    c3.markdown(f'<span style="color:{"#1A7F37" if is_market_open() else "#CF222E"};">'
                f"**{mkt_lbl}**</span>", unsafe_allow_html=True)
    _ws_keys_stored = st.session_state.get("_ws_keys", [])
    _ltp_count = sum(1 for k in _ws_keys_stored if ws_feed.get_ltp(k) is not None)
    if not ws_on:
        c4.caption("Reconnecting...")
    elif _ltp_count == 0 and _ws_keys_stored:
        c4.caption(f"⚠️ Connected but 0/{len(_ws_keys_stored)} ticks received")

    # ── Index spot LTP row ─────────────────────────────────────────────────────
    spot_cols = st.columns(len(sel_idx) * 3)
    ci = 0
    for idx_name in sel_idx:
        sk      = SPOT_KEYS.get(idx_name, "")
        ltp     = ws_feed.get_ltp(sk)
        prev_c  = settings.get(f"spot_close_{idx_name}", 0)
        ltp_str = f"{ltp:,.2f}" if ltp else "—"
        if ltp and prev_c:
            chg     = ltp - prev_c
            chg_pct = chg / prev_c * 100
            delta   = f"{'+'if chg>=0 else ''}{chg:,.0f} ({chg_pct:+.2f}%)"
        else:
            delta = None
        spot_cols[ci].metric(f"{idx_name} Spot", ltp_str, delta=delta)
        ci += 3

    st.divider()

    # ── Refresh spot bias every 5 minutes from live WS spot candles ───────────
    _bias_refresh_every = 300  # seconds
    _now_ts = time.time()
    for idx_name in sel_idx:
        _bias_key    = f"_spot_bias_ts_{idx_name}"
        _last_update = st.session_state.get(_bias_key, 0)
        if _now_ts - _last_update >= _bias_refresh_every:
            _sk = SPOT_KEYS.get(idx_name, "")
            _spot_df1_live = ws_feed.get_candles_1m(_sk) if _sk else pd.DataFrame()
            if not _spot_df1_live.empty:
                _idx_cfg_bias = INDEX_CONFIG.get(idx_name, {})
                _ss_bias = _idx_cfg_bias.get("session_start") if _idx_cfg_bias.get("is_mcx") else None
                _se_bias = _idx_cfg_bias.get("session_end")   if _idx_cfg_bias.get("is_mcx") else None

                # Bias = most recent OPEN trap on the spot/futures chart.
                # BEARISH TRAP → BULLISH bias (CE). BULLISH TRAP → BEARISH bias (PE).
                _new_bias = "NEUTRAL"
                _spot_15_live = resample_tf(_spot_df1_live, 15,
                                            session_start=_ss_bias,
                                            session_end=_se_bias)
                if not _spot_15_live.empty:
                    _sp_ev, _ = scan_htf_spot(_spot_15_live)
                    _new_bias  = spot_bias(_sp_ev)

                # Update bias in all contracts for this index
                for _m in contracts:
                    if idx_name.lower() in _m["sym"].lower():
                        _m["spot_bias"] = _new_bias
                _all_c = st.session_state.get("_contracts", [])
                for _m in _all_c:
                    if idx_name.lower() in _m.get("sym", "").lower():
                        _m["spot_bias"] = _new_bias
                st.session_state["_contracts"] = _all_c
                st.session_state[_bias_key] = _now_ts

    # ── Scan on latest ws_feed candles ────────────────────────────────────────
    ltf_results   = {}
    df1_map       = {}
    any_intraday  = any(m.get("intraday_mode") for m in contracts)

    for meta in contracts:
        key = meta["key"]
        sym = meta["sym"]
        df1 = ws_feed.get_candles_1m(key)
        df1_map[sym] = df1

        if df1.empty:
            ltf_results[sym] = ([], [])
        elif meta.get("intraday_mode"):
            # Rescan 15-min zones live from current WS bars — catches new zones forming during day
            _df1_today = df1[df1.index.date == date.today()] if not df1.empty else df1
            if not _df1_today.empty:
                _df15 = resample_tf(_df1_today, 15,
                                    session_start=meta.get("session_start"),
                                    session_end=meta.get("session_end"))
                if not _df15.empty:
                    _, _live_15m = scan_htf(_df15)
                    _live_open_15m = [e for e in _live_15m
                                      if e["status"] in ("TRAPPED", "CLOSED")
                                      and e.get("trapped_on")
                                      and pd.Timestamp(e["trapped_on"]).date() == date.today()]
                else:
                    _live_open_15m = meta.get("open_traps_15m", [])
            else:
                _live_open_15m = meta.get("open_traps_15m", [])
            hits, alerts = live_intraday_scan(
                _live_open_15m, df1, sl_buffer, sym, log_path,
                opt_type=meta.get("opt_type", ""),
                spot_bias_val=meta.get("spot_bias", "NEUTRAL"),
                strike=meta.get("strike"),
                exchange=meta.get("exchange", "BSE"),
                index_name=meta.get("idx_name", "Sensex"),
                upstox_key=key,
                upstox_token=token,
            )
            ltf_results[sym] = (hits, alerts)
        elif meta["open_traps"]:
            _spot_ltp_live = ws_feed.get_ltp(SPOT_KEYS.get(meta.get("idx_name", "Sensex"), ""))
            hits, alerts = live_ltf_scan(
                meta["open_traps"], df1, ltf_min, sl_buffer, sym, log_path,
                opt_type   = meta.get("opt_type", ""),
                spot_ltp   = _spot_ltp_live or 0,
                expiry     = meta.get("expiry"),
                lot_size   = meta.get("lot_size", 20),
                qty        = 2,
                strike     = meta.get("strike"),
                exchange   = meta.get("exchange", "BSE"),
                index_name = meta.get("idx_name", "Sensex"),
                upstox_key = key,
                upstox_token = token,
                entry_cutoff  = meta.get("entry_cutoff"),
                entry_windows = meta.get("entry_windows"),
                strike_step   = meta.get("strike_step", 100),
            )
            ltf_results[sym] = (hits, alerts)
        else:
            ltf_results[sym] = ([], [])

    # SL / T1 exits are now handled tick-by-tick in the WS callback (_on_tick)
    # Only trail SL update needs bar data — still done here on fragment cadence

        # Update trailing SL using live 5-min bars of each open trade's tracked sym
        for t in angel_orders.get_open_trades():
            if t["trailing_mode"]:
                _df1_trail = df1_map.get(t["tracked_sym"])
                if _df1_trail is not None and not _df1_trail.empty:
                    from data import resample_tf as _rs
                    _df5_trail = _rs(_df1_trail, 5)
                    angel_orders.update_trail_sl(_df5_trail)

    # ── 4-row dashboard ────────────────────────────────────────────────────────
    if any_intraday:
        _intra_reason = "Gap day — no fresh 75-min trap" if any(
            st.session_state.get(f"gap_fired_{idx}", False) for idx in ("Sensex", "Nifty")
        ) else "No fresh 75-min zone today"
        st.info(
            f"⚡ **INTRADAY MODE** — {_intra_reason}. "
            "Running 15-min HTF → 5-min MTF → 1-min entry cascade."
        )
    sec("Live Dashboard — All Contracts")
    st.markdown(_dashboard_table(contracts, ltf_results), unsafe_allow_html=True)

    # Toast once per unique alert uid — not every 2s
    _notified = st.session_state.get("notified_setups", set())
    for meta in contracts:
        sym    = meta["sym"]
        _, als = ltf_results.get(sym, ([], []))
        for a in als:
            uid = a.get("uid", "")
            toast_uid = f"toast_{uid}"
            if toast_uid not in _notified:
                st.toast(f"ALERT {sym}: WATCH {a['entry_price']:.2f}", icon="🟠")
                _notified.add(toast_uid)
    st.session_state["notified_setups"] = _notified

    st.divider()

    # ── Phase 3 paired trade panels ────────────────────────────────────────────
    active_pairs = [
        p for p in idx_pairs
        if (track_p1 and "Pair 1" in p["label"])
        or (track_p2 and "Pair 2" in p["label"])
    ]

    if active_pairs:
        sec("Active Trades — Scenario C (Phase 3)")
        for pair in active_pairs:
            # Derive index name from pair label (e.g. "Sensex Pair 1 — S1/R1")
            pair_idx = next(
                (i for i in settings.get("selected_indices", ["Nifty"])
                 if i.lower() in pair["label"].lower()),
                "Nifty"
            )
            _render_pair_phase3(
                pair["label"], pair["ce_sym"], pair["pe_sym"],
                ltf_results, df1_map, log_path, sl_dir, pair_idx,
            )
    else:
        st.info("No active pairs selected.")

    # ── Paper Trades Panel ─────────────────────────────────────────────────────
    _mode_label = "🟡 PAPER MODE (no real orders)" if angel_orders.PAPER_MODE else "🔴 LIVE MODE — REAL ORDERS"
    sec(f"📋 Angel One Trades  |  {_mode_label}")
    _rej_list = st.session_state.get("_spread_rejected", [])
    if _rej_list:
        for _r in _rej_list:
            st.warning(
                f"⚠️ **TRADE SKIPPED — Wide Spread** | {_r['time']} | `{_r['sym']}` | "
                f"{_r['info']} — order was NOT placed to protect from bad fill",
                icon="🚫",
            )
    st.caption(
        f"Mode: {'PAPER ✓' if angel_orders.PAPER_MODE else '🔴 LIVE — Angel One SmartAPI'}  |  "
        f"Open: {len(angel_orders.get_open_trades())}  Closed: {len(angel_orders.get_closed_trades())}  "
        f"Session P&L: ₹{angel_orders.get_session_pnl():,.0f}"
    )
    open_pt   = angel_orders.get_open_trades()
    closed_pt = angel_orders.get_closed_trades()
    pnl_total = angel_orders.get_session_pnl()

    pc1, pc2, pc3 = st.columns(3)
    pc1.metric("Open positions",  len(open_pt))
    pc2.metric("Closed today",    len(closed_pt))
    sign = "+" if pnl_total >= 0 else ""
    pc3.metric("Session P&L",     f"{sign}₹{pnl_total:,.0f}",
               delta=f"{sign}{pnl_total:,.0f}")

    # Auto-sync with broker: close any trade the broker no longer holds
    if not angel_orders.PAPER_MODE:
        _auto_closed = angel_orders.sync_with_broker()
        if _auto_closed:
            st.warning(f"Auto-closed (broker netqty=0): {', '.join(_auto_closed)}")
            open_pt = angel_orders.get_open_trades()

    if open_pt:
        st.markdown("**Open Positions:**")
        for t in open_pt:
            cur_ltp  = ws_feed.get_ltp(next(
                (m["key"] for m in contracts if m["sym"] == t["tracked_sym"]), ""))
            ep       = t["entry_px"] or 0
            cur_ltp  = cur_ltp or ep
            live_pnl = round((cur_ltp - ep) * t["qty_remaining"], 2) if ep else 0
            pnl_sign = "+" if live_pnl >= 0 else ""
            pnl_col  = "🟢" if live_pnl >= 0 else "🔴"

            if t["trailing_mode"]:
                sl_display = f"Trail SL: **{t['trail_sl']:.1f}** (trailing, {len(t['trail_zones'])} zones)"
                status_tag = "TRAILING — 50% booked"
            else:
                sl_display = f"SL: **{t['sl']:.1f}**  |  T1: **{t['target']:.1f}**"
                status_tag = "ACTIVE — full qty"

            lots_rem     = t["qty_remaining"] // t.get("lot_size", 20)
            scan_sym     = t.get("tracked_sym", "—")
            exec_sym     = t.get("live_key", scan_sym)
            spot_entry   = t.get("spot_at_entry", 0)
            entry_time   = t.get("entry_time", "")
            entry_hm     = pd.Timestamp(entry_time).strftime("%H:%M") if entry_time else "—"
            mode_tag     = "PAPER" if t.get("paper_mode") else "LIVE"
            tracking_ltp = ws_feed.get_ltp(scan_sym) or cur_ltp

            _tid = t.get("id", "")
            _col_info, _col_btn = st.columns([5, 1])
            with _col_info:
                st.info(
                    f"**{t['side']} {t['strike']}** — {status_tag} ({mode_tag})\n\n"
                    f"Entry @ **{entry_hm}**  |  Spot at entry: **{spot_entry:.0f}**  |  "
                    f"Lots: **{lots_rem}** × {t.get('lot_size',20)} = **{t['qty_remaining']} units**\n\n"
                    f"Entry LTP: **{ep:.1f}**  |  Current LTP: **{cur_ltp:.1f}**  |  "
                    f"{sl_display}\n\n"
                    f"Scan/SL key: `{scan_sym}`  |  Tracking LTP: **{tracking_ltp:.1f}**"
                    + (f"  |  Exec key: `{exec_sym}`" if exec_sym != scan_sym else "") + "\n\n"
                    f"Signal: **{t.get('signal_src','—')}**  |  "
                    f"{pnl_col} Live P&L: **{pnl_sign}₹{live_pnl:,.0f}**"
                )
            with _col_btn:
                if st.button("❌ Mark\nClosed", key=f"mclose_{_tid}", help="Mark this position as manually closed at broker"):
                    angel_orders.manual_close_trade(_tid, exit_px=cur_ltp)
                    st.rerun()

    if closed_pt:
        rows = []
        for t in closed_pt:
            rows.append({
                "Side"    : t["side"],
                "Strike"  : t["strike"],
                "Entry"   : t["entry_px"],
                "Exit"    : t["exit_px"],
                "Reason"  : t["exit_reason"],
                "P&L"     : f"{'+'if (t['pnl'] or 0)>=0 else ''}₹{t['pnl']:,.0f}",
            })
        st.dataframe(rows, use_container_width=True)

    if not open_pt and not closed_pt:
        st.caption("No paper trades yet today. Waiting for entry signals.")

    # ── Log tail ───────────────────────────────────────────────────────────────
    # ── Today's Phase 3 Trades (persistent — survives refresh) ────────────────
    import json as _json
    _today_str_frag = date.today().strftime("%Y%m%d")
    _p3_path_frag   = log_path.parent / f"p3_trades_{_today_str_frag}.json"
    if _p3_path_frag.exists():
        try:
            _p3_all_frag = list(_json.loads(_p3_path_frag.read_text(encoding="utf-8")).values())
        except Exception:
            _p3_all_frag = []

        if _p3_all_frag:
            sec("📅 Today's Phase 3 Trades — All Signals")

            # Split into open (no exit_time) and closed — only today's entries
            _today_date_frag = date.today()
            def _is_today(t):
                et = t.get("entry_time")
                if not et:
                    return False
                try:
                    return pd.Timestamp(et).date() == _today_date_frag
                except Exception:
                    return False
            _p3_open   = [t for t in _p3_all_frag if not t.get("exit_time") and _is_today(t)]
            _p3_closed = [t for t in _p3_all_frag if t.get("exit_time") and _is_today(t)]

            # Open positions first
            if _p3_open:
                st.markdown("**🟡 Open Positions:**")
                for t in _p3_open:
                    cur_ltp = ws_feed.get_ltp(next(
                        (m["key"] for m in contracts if m["sym"] == t.get("sym")), "")) or 0
                    ep      = t.get("entry_price") or 0
                    live_pnl = round((cur_ltp - ep) * (2 * 20), 2) if ep and cur_ltp else 0
                    pnl_col  = "🟢" if live_pnl >= 0 else "🔴"
                    pnl_sign = "+" if live_pnl >= 0 else ""
                    et = pd.Timestamp(t["entry_time"]).strftime("%H:%M") if t.get("entry_time") else "—"
                    st.info(
                        f"**{t.get('opt_type','')} {t.get('sym','')}**  |  "
                        f"Entry: **{ep:.1f}** @ {et}  |  "
                        f"SL: **{t.get('sl',0):.1f}**  T1: **{t.get('t1',0):.1f}**  |  "
                        f"Current LTP: **{cur_ltp:.1f}**  |  "
                        f"{pnl_col} Live P&L: **{pnl_sign}₹{live_pnl:,.0f}**"
                    )

            # Closed trades table
            if _p3_closed:
                st.markdown("**📋 Closed Trades Today:**")
                _rows = []
                for t in sorted(_p3_closed, key=lambda x: x.get("entry_time", "")):
                    et   = pd.Timestamp(t["entry_time"]).strftime("%H:%M") if t.get("entry_time") else "—"
                    xt   = pd.Timestamp(t["exit_time"]).strftime("%H:%M") if t.get("exit_time") else "—"
                    pnl  = t.get("pnl", 0) or 0
                    _rows.append({
                        "Contract" : t.get("sym", ""),
                        "Side"     : t.get("opt_type", ""),
                        "Entry Time": et,
                        "Entry LTP" : t.get("entry_price", "—"),
                        "SL"        : t.get("sl", "—"),
                        "T1"        : t.get("t1", "—"),
                        "Exit Time" : xt,
                        "Exit LTP"  : t.get("exit_price", "—"),
                        "Result"    : t.get("result", "—"),
                        "P&L (₹)"  : f"{'+'if pnl>=0 else ''}₹{pnl:,.0f}",
                    })
                st.dataframe(_rows, use_container_width=True)

                total_pnl = sum(t.get("pnl", 0) or 0 for t in _p3_closed)
                pnl_sign  = "+" if total_pnl >= 0 else ""
                pnl_col   = "#1A7F37" if total_pnl >= 0 else "#DA3633"
                st.markdown(
                    f'<div style="font-size:15px;font-weight:bold;color:{pnl_col};'
                    f'padding:6px 0;">Session P&L: {pnl_sign}₹{total_pnl:,.0f}</div>',
                    unsafe_allow_html=True,
                )

    if log_path.exists():
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with st.expander(f"Event Log — last {min(30, len(lines))} lines", expanded=False):
            st.code("".join(lines[-30:]), language=None)


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    today_str = date.today().strftime("%Y%m%d")
    log_path  = LOG_DIR / f"live_{today_str}.txt"

    if "notified_setups" not in st.session_state:
        st.session_state["notified_setups"] = set()

    # ── Sidebar ────────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("## Live Trap Tracker")
        st.markdown(f"**{date.today().strftime('%A, %d %b %Y')}**")
        st.markdown("---")

        # Token
        cur_tok = os.environ.get("UPSTOX_TOKEN", "")
        if cur_tok and _token_valid(cur_tok):
            st.success(f"Token valid until {_token_expiry_str(cur_tok)}")
            if st.button("Change Token", use_container_width=True):
                st.session_state["_show_token_input"] = True
        else:
            if cur_tok:
                st.error(f"Token expired at {_token_expiry_str(cur_tok)}")
            st.session_state["_show_token_input"] = True

        if st.session_state.get("_show_token_input"):
            tok_input = st.text_input("Upstox Daily Token", type="password",
                                      placeholder="Paste bearer token", key="tok_input")
            if st.button("Set Token", use_container_width=True):
                t = tok_input.strip()
                if not t:
                    st.error("Token cannot be empty.")
                elif not _token_valid(t):
                    st.error("Token is already expired — get a fresh token.")
                else:
                    _save_token_to_env(t)
                    st.session_state["live_token"] = t
                    st.session_state["_show_token_input"] = False
                    st.session_state.pop("_initialized", None)
                    st.session_state.pop("_initialized_indices", None)
                    st.cache_data.clear()
                    st.rerun()

        token = _get_token()
        if token:
            st.caption(f"Token: `...{token[-12:]}`")
        else:
            st.warning("No token — paste above.")

        # ── Angel One Credentials ──────────────────────────────────────────────
        st.markdown("---")
        _angel_mode = "🔴 LIVE" if not angel_orders.PAPER_MODE else "🟡 PAPER"
        with st.expander(f"Angel One Credentials  ({_angel_mode})", expanded=not bool(os.environ.get("ANGEL_API_KEY"))):
            _cur_api_key  = os.environ.get("ANGEL_API_KEY", "")
            _cur_client   = os.environ.get("ANGEL_CLIENT_ID", "")
            _cur_password = os.environ.get("ANGEL_PASSWORD", "")
            _cur_totp     = os.environ.get("ANGEL_TOTP_SECRET", "")

            _in_api_key  = st.text_input("API Key",     value=_cur_api_key,  key="ang_api_key")
            _in_client   = st.text_input("Client ID",   value=_cur_client,   key="ang_client")
            _in_password = st.text_input("PIN / Password", value=_cur_password, type="password", key="ang_pw")
            _in_totp     = st.text_input("TOTP Secret", value=_cur_totp,     type="password", key="ang_totp")

            if st.button("Save Angel Credentials", use_container_width=True):
                _env_path = Path(__file__).parent / ".env"
                _env_lines = _env_path.read_text(encoding="utf-8").splitlines() if _env_path.exists() else []
                _updates = {
                    "ANGEL_API_KEY"    : _in_api_key.strip(),
                    "ANGEL_CLIENT_ID"  : _in_client.strip(),
                    "ANGEL_PASSWORD"   : _in_password.strip(),
                    "ANGEL_TOTP_SECRET": _in_totp.strip(),
                }
                # Replace existing lines or append missing keys
                _new_lines = []
                _written = set()
                for _ln in _env_lines:
                    _k = _ln.split("=")[0].strip()
                    if _k in _updates:
                        _new_lines.append(f"{_k}={_updates[_k]}")
                        _written.add(_k)
                    else:
                        _new_lines.append(_ln)
                for _k, _v in _updates.items():
                    if _k not in _written:
                        _new_lines.append(f"{_k}={_v}")
                _env_path.write_text("\n".join(_new_lines) + "\n", encoding="utf-8")
                # Apply to current process immediately
                for _k, _v in _updates.items():
                    os.environ[_k] = _v
                    setattr(angel_orders, _k.replace("ANGEL_", "ANGEL_").replace("SECRET", "SEC")
                            .replace("ANGEL_TOTP_SEC", "ANGEL_TOTP_SEC"), _v)
                angel_orders.ANGEL_API_KEY   = _updates["ANGEL_API_KEY"]
                angel_orders.ANGEL_CLIENT_ID  = _updates["ANGEL_CLIENT_ID"]
                angel_orders.ANGEL_PASSWORD   = _updates["ANGEL_PASSWORD"]
                angel_orders.ANGEL_TOTP_SEC   = _updates["ANGEL_TOTP_SECRET"]
                st.session_state.pop("_angel_logged_in", None)  # force re-login
                st.session_state.pop("_initialized", None)
                st.session_state.pop("_initialized_indices", None)
                st.success("Credentials saved — Force Refresh to re-login")
                st.rerun()

            if _cur_api_key:
                st.caption(f"API Key: `...{_cur_api_key[-4:]}`  |  Client: `{_cur_client}`")

        # ── Order product type (NRML vs MIS) ──────────────────────────────────
        _product_type = st.radio(
            "Order Type",
            ["CARRYFORWARD (NRML)", "INTRADAY (MIS)"],
            index=0,
            horizontal=True,
            help="CARRYFORWARD = Normal F&O order (NRML). INTRADAY = auto sq-off at session end (MIS).",
        )
        st.session_state["angel_product_type"] = (
            "CARRYFORWARD" if _product_type.startswith("CARRYFORWARD") else "INTRADAY"
        )

        # ── Trading window status ──────────────────────────────────────────────
        st.markdown("---")
        st.markdown("**Trading Gates**")
        _use_1itm = st.checkbox(
            "Use 1-ITM strike for orders",
            value=st.session_state.get("use_1itm", False),
            key="use_1itm",
            help="CE: order at strike−step (deeper ITM). PE: strike+step. Higher delta → better option P&L. "
                 "HTF scan still runs on the selected strike; only the live order uses 1-ITM.",
        )
        _now_t = datetime.now().time()
        for _cfg_idx, _cfg_v in INDEX_CONFIG.items():
            _ec  = _cfg_v.get("entry_cutoff")
            _ew  = _cfg_v.get("entry_windows")
            _sqt = _cfg_v.get("sq_off_time", "")
            if _ew:
                _in_w = any(ws <= _now_t <= we for ws, we in _ew)
                _next_w = next((ws for ws, we in _ew if ws > _now_t), None)
                _w_status = "🟢 IN WINDOW" if _in_w else f"⏸ {'next: ' + _next_w.strftime('%H:%M') if _next_w else 'done today'}"
                st.caption(f"{_cfg_idx}: {_w_status}  |  SQ {_sqt}")
            elif _ec:
                _co_h, _co_m = map(int, _ec.split(":"))
                _gate_open = (_now_t.hour, _now_t.minute) <= (_co_h, _co_m)
                _g_status  = f"🟢 MORNING HOLD (till {_ec})" if _gate_open else f"⛔ CLOSED ({_ec} passed)"
                st.caption(f"{_cfg_idx}: {_g_status}  |  SQ {_sqt}")

        st.markdown("---")
        index_choice     = st.radio("Index", ["Nifty", "Sensex", "Both", "CrudeOil"],
                                    index=0, horizontal=True)
        if index_choice == "Both":
            selected_indices = ["Nifty", "Sensex"]
        elif index_choice == "CrudeOil":
            selected_indices = ["CrudeOil"]
        else:
            selected_indices = [index_choice]

        # ── Per-script LIVE / PAPER toggle ────────────────────────────────────
        st.markdown("**Order Mode per Script**")
        _all_scripts = ["Nifty", "Sensex", "CrudeOil"]
        _live_scripts: set[str] = set()
        for _sc in _all_scripts:
            _sc_live = st.checkbox(
                f"🔴 LIVE — {_sc}",
                value=False,
                key=f"live_mode_{_sc}",
                help=f"Tick = LIVE orders on Angel One for {_sc}. Untick = paper/simulation only.",
            )
            if _sc_live:
                _live_scripts.add(_sc)
        st.session_state["live_scripts"] = _live_scripts

        st.markdown("---")
        track_pair1 = st.checkbox("Pair 1 — S1 CE / R1 PE", value=True)
        track_pair2 = st.checkbox("Pair 2 — S2 CE / R2 PE", value=True)

        st.markdown("---")
        htf_min    = st.number_input("HTF (min)",       value=HTF_MINUTES, step=5,  min_value=5)
        ltf_min    = st.number_input("LTF (min)",       value=LTF_MINUTES, step=1,  min_value=1)
        sl_buffer         = st.number_input("SL Buffer (pts)", value=DEFAULT_SL_BUFFER, step=0.5)
        zone_far_mult     = st.number_input(
            "Zone Far ATR multiplier",
            value=1.5, step=0.1, min_value=0.3, max_value=3.0,
            help="Cascade fires if current LTP − zone entry > X × option ATR. "
                 "Backtest: 1.5x optimal. Lower = cascade more aggressively."
        )
        gap_nifty  = st.number_input("Nifty gap %",  value=1.0, step=0.1,
                                     min_value=0.5, max_value=5.0, key="gap_nifty")
        gap_sensex = st.number_input("Sensex gap %", value=1.0, step=0.1,
                                     min_value=0.5, max_value=5.0, key="gap_sensex")
        gap_thresh = {"Nifty": gap_nifty, "Sensex": gap_sensex}
        _max_spread_pct = st.number_input(
            "Max Bid-Ask Spread %",
            value=float(MAX_SPREAD_PCT), step=0.5, min_value=0.5, max_value=20.0,
            help="If (ask−bid)/mid > this %, order is SKIPPED and reason is logged. "
                 "Set higher for illiquid scripts (e.g. 8–10%) or lower for liquid ones.",
        )
        st.session_state["max_spread_pct"] = _max_spread_pct
        sl_dir          = st.toggle("Phase 3 SL Direction Filter", value=True)
        spot_dir_filter = st.checkbox("📍 Spot Direction Filter (15-5 min)",
                                      value=True,
                                      help="Only take CE trades when spot is BULLISH (bears trapped), "
                                           "only PE when BEARISH (bulls trapped). "
                                           "Reads today's Nifty/Sensex 15-min spot trap.")
        bias_override = st.selectbox(
            "Spot Bias Override",
            ["AUTO (computed)", "BULLISH → CE only", "BEARISH → PE only", "NEUTRAL (both)"],
            index=0,
            help="Override the auto-computed spot bias. Use when auto is stuck or wrong.",
        )

        # ── Next-day key levels (auto-computed from today's spot close) ────────
        st.markdown("---")
        _next_levels = st.session_state.get("_next_day_levels")
        if _next_levels:
            st.markdown("**Next Session Levels**")
            st.markdown(
                f"<div style='font-size:11px;line-height:1.7;'>"
                f"<b>Close:</b> {_next_levels.get('close','—')}<br>"
                f"<b>Pivot:</b> {_next_levels.get('pivot','—')}<br>"
                f"<b>R1:</b> {_next_levels.get('r1','—')} &nbsp; <b>R2:</b> {_next_levels.get('r2','—')}<br>"
                f"<b>S1:</b> {_next_levels.get('s1','—')} &nbsp; <b>S2:</b> {_next_levels.get('s2','—')}<br>"
                f"<b>CE strikes:</b> {_next_levels.get('ce','—')}<br>"
                f"<b>PE strikes:</b> {_next_levels.get('pe','—')}<br>"
                f"<b>Gap UP if &gt;</b> {_next_levels.get('gap_up','—')}<br>"
                f"<b>Gap DN if &lt;</b> {_next_levels.get('gap_dn','—')}"
                f"</div>",
                unsafe_allow_html=True
            )
            st.markdown("---")

        if st.button("Force Refresh", use_container_width=True, type="primary"):
            st.session_state.pop("_initialized", None)
            st.session_state.pop("_initialized_indices", None)
            for _k in list(st.session_state.keys()):
                if _k.startswith("_spot_df1_"):
                    del st.session_state[_k]
            ws_feed.stop()
            st.cache_data.clear()
            st.rerun()

        if st.button("Reconnect WS", use_container_width=True):
            # Just restart — auto-reconnect loop handles the rest, no page reload
            _token_now = st.session_state.get("upstox_token", "")
            _keys_now  = st.session_state.get("_ws_keys", [])
            ws_feed.stop()
            if _token_now and _keys_now:
                ws_feed.start(_token_now, _keys_now)
            st.toast("WS restarting...", icon="🔄")

        if log_path.exists():
            st.markdown("---")
            st.download_button("Download Log",
                               log_path.read_text(encoding="utf-8"),
                               file_name=log_path.name, mime="text/plain")

    token = _get_token()

    # ── Header ─────────────────────────────────────────────────────────────────
    st.markdown("# Live Trap Tracker")

    # Strategy banner — always visible so you know what mode is active
    _strat_parts = ["**Strategy: 15-min HTF + 5-min Cascade  |  Strikes: Prev-Day Pivot (S1/S2 CE, R1/R2 PE)**"]
    _spot_icon   = "🟢 ON" if spot_dir_filter else "⚫ OFF"
    _sl_icon     = "🟢 ON" if sl_dir          else "⚫ OFF"
    _strat_parts.append(f"Spot Direction Filter: {_spot_icon}")
    _strat_parts.append(f"Phase-3 SL Direction Filter: {_sl_icon}")
    st.info("  |  ".join(_strat_parts))

    if not token:
        st.error("Paste your Upstox token in the sidebar to start.")
        st.stop()

    # ── Detect settings change that requires full re-init ───────────────────────
    # Index change alone does NOT reset — new indices are appended incrementally.
    # Token / timeframe change requires full reset.
    _core_key = f"{htf_min}_{ltf_min}_{token[-8:] if token else ''}"
    if st.session_state.get("_core_key") != _core_key:
        st.session_state.pop("_initialized", None)
        st.session_state.pop("_initialized_indices", None)
        st.session_state["_core_key"] = _core_key

    # ── Incremental init: only process indices not yet initialised ─────────────
    _inited_set  = st.session_state.get("_initialized_indices", set())
    _new_indices = [i for i in selected_indices if i not in _inited_set]

    if _new_indices:

        # ── Angel One login (live mode, once per session) ──────────────────────
        if not angel_orders.PAPER_MODE and not st.session_state.get("_angel_logged_in"):
            with st.spinner("Logging in to Angel One..."):
                ok = angel_orders.login()
            if ok:
                st.success("Angel One login SUCCESS — live orders enabled")
                st.session_state["_angel_logged_in"] = True
            else:
                st.error("Angel One login FAILED — check .env credentials (ANGEL_PASSWORD, ANGEL_API_KEY, etc.)")
                st.stop()

        contracts  = []    # accumulates NEW contracts this run only
        idx_pairs  = []
        hist_map   = {}    # key → df1 for new ws keys only
        ws_keys    = []    # new instrument keys to subscribe

        for idx_name in _new_indices:
            cfg      = INDEX_CONFIG[idx_name]
            step     = cfg["strike_step"]
            prefix   = cfg["symbol_prefix"]
            lbl_idx  = cfg["label"]
            # Reset cross-pair slot at start of each index (fresh each Force Refresh)
            st.session_state[f"_sim_slot_{idx_name}"] = {"CE": None, "PE": None}

            is_mcx = cfg.get("is_mcx", False)

            # ── Fetch prev-day OHLC (Equity: index, MCX: futures) ─────────────
            with st.spinner(f"Loading {lbl_idx} spot data..."):
                try:
                    spot = _fetch_mcx_spot(token) if is_mcx else _fetch_spot(token, idx_name)
                except Exception as ex:
                    st.error(f"{lbl_idx} spot fetch failed: {ex}")
                    continue

            H, L, C = spot["high"], spot["low"], spot["close"]

            # Gap filter
            today_open = None
            gap_pct    = 0.0
            gap_fired  = False
            try:
                today_open = _fetch_mcx_today_open(token) if is_mcx else _fetch_today_open(token, idx_name)
            except Exception:
                pass
            if today_open and C > 0:
                gap_pct   = abs(today_open - C) / C * 100
                gap_fired = gap_pct >= gap_thresh.get(idx_name, 1.2)
            st.session_state[f"gap_fired_{idx_name}"] = gap_fired

            levels = pivot_levels(H, L, C)
            s1 = round_n(levels["s1"], step)
            s2 = round_n(levels["s2"], step)
            r1 = round_n(levels["r1"], step)
            r2 = round_n(levels["r2"], step)

            using_atm = False
            itm_near  = cfg.get("gap_itm_near", 300)
            itm_far   = cfg.get("gap_itm_far",  600)
            if gap_fired and today_open:
                atm       = round_n(today_open, step)
                ce1       = atm - itm_near   # ITM for CE (below futures)
                ce2       = atm - itm_far
                pe1       = atm + itm_near   # ITM for PE (above futures)
                pe2       = atm + itm_far
                using_atm = True
                direction = "GAP UP" if today_open > C else "GAP DOWN"
                st.warning(
                    f"⚠️ **{direction} {gap_pct:.1f}%** detected  |  "
                    f"Prev Close: {C:,.2f}  Today Open: {today_open:,.2f}  |  "
                    f"ITM-{itm_near} CE {ce1:,} / ITM-{itm_far} CE {ce2:,}  "
                    f"ITM-{itm_near} PE {pe1:,} / ITM-{itm_far} PE {pe2:,}"
                )
            else:
                ce1, ce2, pe1, pe2 = s1, s2, r1, r2

            # Expiry
            if is_mcx:
                expiries_list = _fetch_mcx_expiries(token)
            else:
                expiries_list = _fetch_expiries(idx_name, token)
            today_iso = date.today().strftime("%Y-%m-%d")
            if expiries_list:
                future     = [e for e in expiries_list if e >= today_iso]
                expiry_api = future[0] if future else expiries_list[-1]
                expiry     = datetime.strptime(expiry_api, "%Y-%m-%d").date()
            else:
                expiry     = cfg["expiry_fn"](date.today())
                expiry_api = expiry.strftime("%Y-%m-%d")

            # Display spot row
            _und_label = "Futures" if is_mcx else "Prev Close"
            _ce_lbl1   = f"ITM-{itm_near} CE" if using_atm else "S1 CE"
            _ce_lbl2   = f"ITM-{itm_far} CE"  if using_atm else "S2 CE"
            _pe_lbl1   = f"ITM-{itm_near} PE" if using_atm else "R1 PE"
            _pe_lbl2   = f"ITM-{itm_far} PE"  if using_atm else "R2 PE"
            sc = st.columns(7)
            card(sc[0], f"{lbl_idx} {_und_label}", f"{C:,.2f}", "blue")
            card(sc[1], "Today Open",               f"{today_open:,.2f}" if today_open else "—", "")
            card(sc[2], _ce_lbl1, f"{ce1:,}", "green")
            card(sc[3], _ce_lbl2, f"{ce2:,}", "green")
            card(sc[4], _pe_lbl1, f"{pe1:,}", "red")
            card(sc[5], _pe_lbl2, f"{pe2:,}", "red")
            card(sc[6], f"{lbl_idx} Expiry", expiry.strftime("%d %b %y"), "")

            # Add underlying key to WS subscription
            ws_keys.append(SPOT_KEYS[idx_name])

            # HTF scan for each contract
            contract_defs = [
                (ce1, "CE", f"{lbl_idx} {_ce_lbl1} {ce1:,}"),
                (ce2, "CE", f"{lbl_idx} {_ce_lbl2} {ce2:,}"),
                (pe1, "PE", f"{lbl_idx} {_pe_lbl1} {pe1:,}"),
                (pe2, "PE", f"{lbl_idx} {_pe_lbl2} {pe2:,}"),
            ]

            ce_syms = []
            pe_syms = []

            for strike, opt_type, label in contract_defs:
                sym = trading_symbol(strike, opt_type, expiry, prefix)
                with st.spinner(f"HTF scan: {label}..."):
                    if is_mcx:
                        open_traps, key, df1, from_date, dbg = morning_scan_mcx(
                            strike, opt_type, expiry_api, token,
                        )
                    else:
                        open_traps, key, df1, from_date, dbg = morning_scan(
                            strike, opt_type, expiry_api, htf_min, token, idx_name,
                        )

                if key:
                    ws_keys.append(key)
                    if df1 is not None and not df1.empty:
                        hist_map[key] = df1
                    # Wire prev_close from yesterday's close (for % change display)
                    prev_c = 0.0
                    if df1 is not None and not df1.empty:
                        yesterday_bars = df1[df1.index.date < date.today()]
                        if not yesterday_bars.empty:
                            prev_c = round(float(yesterday_bars["close"].iloc[-1]), 2)

                    # ── Intraday mode detection ────────────────────────────────
                    # Cascade fires when:
                    #   (a) no fresh 75-min zone trapped TODAY, OR
                    #   (b) zone exists but zone_high is too far from option's last close
                    # Any TRAPPED zone within lookback window counts — not just today's.
                    # A zone trapped last week that hasn't closed yet is still tradeable.
                    _cur_ltp_check = ws_feed.get_ltp(key) or prev_c
                    # Only consider zones where zone price is within 60% of current LTP
                    # — filters out stale deep-history zones (e.g. Jun 12 zones at 30-100
                    #   when current option LTP is 540, which are never reachable today)
                    _ltp_floor = _cur_ltp_check * 0.4 if _cur_ltp_check else 0
                    today_traps   = [e for e in open_traps
                                     if e.get("trapped_on")
                                     and e.get("zone_trigger", e.get("zone_high", 0)) >= _ltp_floor]
                    has_fresh_75m = bool(today_traps)

                    zone_too_far   = False
                    _daily_atr     = option_daily_atr(df1, htf_min) if df1 is not None else 0.0
                    _zone_far_threshold = _daily_atr * zone_far_mult if _daily_atr > 0 else None
                    if has_fresh_75m and _zone_far_threshold:
                        _cur_ltp = _cur_ltp_check
                        zone_too_far = all(
                            _cur_ltp - e.get("zone_trigger", e.get("zone_high", 0)) > _zone_far_threshold
                            for e in today_traps
                            if e.get("zone_trigger", e.get("zone_high", 0)) > 0
                        )

                    _cascade_reason = ""
                    if not has_fresh_75m:
                        _cascade_reason = "Gap day, no 75-min zone" if gap_fired else "No 75-min zone today"
                    elif zone_too_far:
                        _atr_str = f"{_daily_atr:.1f}" if _daily_atr else "?"
                        _cur_ltp = ws_feed.get_ltp(key) or prev_c
                        _nearest_z = min(today_traps,
                                         key=lambda e: abs(e.get("zone_trigger", e.get("zone_high",0)) - _cur_ltp),
                                         default={})
                        _gap = _cur_ltp - _nearest_z.get("zone_trigger", _nearest_z.get("zone_high", 0))
                        _cascade_reason = (f"Zone too far — LTP {_cur_ltp:.0f} vs zone {_nearest_z.get('zone_trigger',0):.0f} "
                                           f"gap {_gap:.0f} > {zone_far_mult:.1f}x ATR({_atr_str})")

                    intraday_mode = not has_fresh_75m or zone_too_far
                    open_traps_15m = []

                    intraday_df1 = None
                    if intraday_mode:
                        with st.spinner(f"⚡ INTRADAY CASCADE: 15-min scan for {label}..."):
                            if is_mcx:
                                open_traps_15m, _, intraday_df1, _, _dbg15 = morning_scan_mcx_intraday(
                                    strike, opt_type, token,
                                )
                            else:
                                open_traps_15m, _, intraday_df1, _, _dbg15 = morning_scan_intraday(
                                    strike, opt_type, expiry_api, token, idx_name,
                                )
                        st.caption(
                            f"↳ [CASCADE — {_cascade_reason}] "
                            f"15-min zones: {len(open_traps_15m)} | "
                            f"15-min bars: {_dbg15.get('rows_htf15',0)} | "
                            f"err: {_dbg15.get('fetch_err') or 'none'}"
                        )

                    # ── Spot direction bias (for filter) ──────────────────────
                    _spot_bias = "NEUTRAL"   # default
                    # Manual override takes priority over computed bias
                    if bias_override == "BULLISH → CE only":
                        _spot_bias = "BULLISH"
                    elif bias_override == "BEARISH → PE only":
                        _spot_bias = "BEARISH"
                    elif bias_override == "NEUTRAL (both)":
                        _spot_bias = "NEUTRAL"
                    elif spot_dir_filter and opt_type:
                        _spot_df1    = st.session_state.get(f"_spot_df1_{idx_name}")
                        if _spot_df1 is None:
                            _h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
                            # Try intraday endpoint first (returns today's live bars),
                            # fall back to regular historical endpoint.
                            _spot_raw_key = SPOT_KEYS.get(idx_name,
                                            "NSE_INDEX|Nifty 50" if idx_name == "Nifty"
                                            else "BSE_INDEX|SENSEX")
                            _spot_df1, _serr = fetch_1min_intraday(_spot_raw_key, _h)
                            if _serr or _spot_df1 is None or _spot_df1.empty:
                                from data import fetch_spot_bars
                                _today_str = date.today().strftime("%Y-%m-%d")
                                _spot_df1 = fetch_spot_bars(
                                    _today_str, _today_str, _h, idx_name, minutes=1
                                )
                            st.session_state[f"_spot_df1_{idx_name}"] = _spot_df1
                        if not _spot_df1.empty:
                            _spot_15 = resample_tf(_spot_df1, 15,
                                                   session_start=cfg.get("session_start") if is_mcx else None,
                                                   session_end=cfg.get("session_end")     if is_mcx else None)
                            if not _spot_15.empty:
                                _spot_ev, _ = scan_htf_spot(_spot_15)
                                _spot_bias   = spot_bias(_spot_ev)
                        _bias_icon = ("🟢 BULLISH → CE only" if _spot_bias == "BULLISH"
                                      else "🔴 BEARISH → PE only" if _spot_bias == "BEARISH"
                                      else "⚪ NEUTRAL")
                        st.caption(f"📍 Spot bias ({idx_name}): {_bias_icon}")

                        # Compute next-session levels from today's spot close
                        if not _spot_df1.empty and "_next_day_levels" not in st.session_state:
                            _sc = round(float(_spot_df1["close"].iloc[-1]), 0)
                            _sh = round(float(_spot_df1["high"].max()), 0)
                            _sl = round(float(_spot_df1["low"].min()), 0)
                            _npv = pivot_levels(_sh, _sl, _sc)
                            def _r(x): return int(round(x/100)*100)
                            _gap_pct = gap_thresh.get(idx_name, 1.5) / 100
                            st.session_state["_next_day_levels"] = {
                                "close"  : int(_sc),
                                "pivot"  : int(_npv["pivot"]),
                                "r1"     : int(_npv["r1"]),
                                "r2"     : int(_npv["r2"]),
                                "s1"     : int(_npv["s1"]),
                                "s2"     : int(_npv["s2"]),
                                "ce"     : f"{_r(_npv['s1'])} / {_r(_npv['s2'])}",
                                "pe"     : f"{_r(_npv['r1'])} / {_r(_npv['r2'])}",
                                "gap_up" : int(_sc * (1 + _gap_pct)),
                                "gap_dn" : int(_sc * (1 - _gap_pct)),
                            }

                    contracts.append({
                        "sym"          : sym,
                        "key"          : key,
                        "label"        : label,
                        "opt_type"     : opt_type,
                        "open_traps"   : open_traps,
                        "open_traps_15m": open_traps_15m,
                        "intraday_mode": intraday_mode,
                        "prev_close"   : prev_c,
                        "spot_bias"    : _spot_bias,
                        "idx_name"     : idx_name,
                        "session_start": cfg.get("session_start") if is_mcx else None,
                        "session_end"  : cfg.get("session_end")   if is_mcx else None,
                        "strike"       : strike,
                        "exchange"     : "MCX" if is_mcx else ("BFO" if idx_name == "Sensex" else "NFO"),
                        "expiry"       : expiry,
                        "lot_size"     : cfg.get("lot_size", 20),
                        "entry_cutoff" : cfg.get("entry_cutoff"),
                        "entry_windows": cfg.get("entry_windows"),
                        "strike_step"  : cfg.get("strike_step", 100),
                    })

                    # ── Today's P&L simulation ─────────────────────────────────
                    _lot_size   = INDEX_CONFIG.get(idx_name, {}).get("lot_size", DEFAULT_LOT_SIZE)
                    _sim_df1    = intraday_df1 if (intraday_mode and intraday_df1 is not None and not intraday_df1.empty) else df1
                    _htf_min_s  = 15 if intraday_mode else htf_min

                    # Cross-pair slot tracker: 1 CE open at a time, 1 PE open at a time
                    # Shared across all contracts in this index via session_state
                    _slot_key = f"_sim_slot_{idx_name}"
                    if _slot_key not in st.session_state:
                        st.session_state[_slot_key] = {"CE": None, "PE": None}
                    _slot = st.session_state[_slot_key]

                    _sess_start = cfg.get("session_start") if is_mcx else None
                    _sess_end   = cfg.get("session_end")   if is_mcx else None
                    _min_et     = cfg.get("session_start", "09:30") if is_mcx else "09:30"
                    if _sim_df1 is not None and not _sim_df1.empty:
                        _df_htf_s = resample_tf(_sim_df1, _htf_min_s,
                                                session_start=_sess_start, session_end=_sess_end)
                        if not _df_htf_s.empty:
                            _, _all_sim = scan_htf(_df_htf_s)
                            if intraday_mode:
                                _expires_today = (expiry == date.today())
                                _max_et = "13:00" if (_expires_today and not is_mcx) else None
                                _sim_trades = _run_cascade_simulation(
                                    _all_sim, _sim_df1,
                                    sl_buffer=sl_buffer, lot_size=_lot_size, qty=2,
                                    min_entry_time=_min_et,
                                    max_entry_time=_max_et,
                                    trail_after_t1=True,
                                    session_start=_sess_start,
                                    session_end=_sess_end,
                                )
                            else:
                                _sim_trades = simulate_today_trades(
                                    _all_sim, _sim_df1,
                                    sl_buffer=sl_buffer, lot_size=_lot_size, qty=2,
                                )

                            # ── Cross-pair rule: 1 CE open / 1 PE open at a time ──────
                            _filtered_trades = []
                            for _t in _sim_trades:
                                _side   = opt_type
                                _et     = pd.Timestamp(_t["entry_time"])
                                _xt_raw = _t.get("exit_time")
                                # None or empty string = still open / running
                                _xt = pd.Timestamp(_xt_raw) if (_xt_raw and str(_xt_raw).strip()) else None

                                _open_slot = _slot.get(_side)
                                if _open_slot is not None:
                                    _ox_raw = _open_slot.get("exit_time")
                                    # If exit_time is None/empty → slot is still occupied
                                    _ox = pd.Timestamp(_ox_raw) if (_ox_raw and str(_ox_raw).strip()) else None
                                    slot_occupied = (_ox is None) or (_ox > _et)
                                    if slot_occupied:
                                        continue  # same-side trade still open — skip

                                # Slot free — take trade, occupy slot
                                _filtered_trades.append(_t)
                                _slot[_side] = {
                                    "sym"       : sym,
                                    "entry_time": str(_et),
                                    "exit_time" : str(_xt) if _xt else None,
                                }
                            _sim_trades = _filtered_trades
                            st.session_state[_slot_key] = _slot

                            # ── Persist to JSON BEFORE spot filter ─────────────────
                            import json as _json
                            _p3_path = log_path.parent / f"p3_trades_{today_str}.json"
                            _p3_all  = {}
                            if _p3_path.exists():
                                try:
                                    _p3_all = _json.loads(_p3_path.read_text(encoding="utf-8"))
                                except Exception:
                                    _p3_all = {}
                            for _t in _sim_trades:
                                _tid = f"{sym}_{_t.get('entry_time','')}"
                                _p3_all[_tid] = {
                                    **_t,
                                    "sym"        : sym,
                                    "opt_type"   : opt_type,
                                    "idx"        : idx_name,
                                    "bias_filter": _spot_bias,
                                }
                            try:
                                _p3_path.write_text(
                                    _json.dumps(_p3_all, default=str, indent=2),
                                    encoding="utf-8"
                                )
                            except Exception:
                                pass

                            # ── Spot direction filter (UI display only) ─────────────
                            if spot_dir_filter and _spot_bias != "NEUTRAL":
                                allowed = "CE" if _spot_bias == "BULLISH" else "PE"
                                if opt_type != allowed:
                                    _sim_trades = []

                            total_pnl  = sum(t["pnl"] for t in _sim_trades)
                            total_col  = "#1A7F37" if total_pnl >= 0 else "#DA3633"
                            pnl_header = f'+₹{total_pnl:,.0f}' if total_pnl >= 0 else f'-₹{abs(total_pnl):,.0f}'
                            n_t1  = sum(1 for t in _sim_trades if "T1" in t["result"])
                            n_sl  = sum(1 for t in _sim_trades if "SL" in t["result"])
                            exp_label = (
                                f"📊 {label} — Today: {len(_sim_trades)} signal(s) | "
                                f"T1✓ {n_t1}  SL✗ {n_sl}  |  "
                                f"Net P&L: {pnl_header}"
                            ) if _sim_trades else f"📊 {label} — Today: No signals"
                            with st.expander(exp_label, expanded=True):
                                if not _sim_trades:
                                    _cur_ltp_disp = ws_feed.get_ltp(key) or prev_c
                                    st.caption(f"No closed LTF entry today — watching zones below. Current LTP: **{_cur_ltp_disp:.1f}**")
                                    # Show all open HTF zones being tracked
                                    _zones_to_show = open_traps_15m if intraday_mode else open_traps
                                    _zone_label    = "15-min" if intraday_mode else "75-min"
                                    if _zones_to_show:
                                        _zone_rows = []
                                        for _z in _zones_to_show:
                                            _trap_dt = pd.Timestamp(_z["trapped_on"]).strftime("%d %b %H:%M") if _z.get("trapped_on") else "—"
                                            _ref_dt  = pd.Timestamp(_z["ref_ts"]).strftime("%d %b %H:%M") if _z.get("ref_ts") else "—"
                                            _ztrig   = _z.get("zone_trigger", _z.get("zone_high", 0))
                                            _dist    = round(_cur_ltp_disp - _ztrig, 1) if _ztrig else "?"
                                            _zone_rows.append({
                                                "TF"         : _zone_label,
                                                "Ref bar"    : _ref_dt,
                                                "Trapped on" : _trap_dt,
                                                "Zone H"     : round(_z.get("zone_high", 0), 1),
                                                "Zone L"     : round(_z.get("zone_low",  0), 1),
                                                "Entry"      : round(_ztrig, 1),
                                                "SL"         : round(_z.get("sl", 0), 1),
                                                "LTP dist"   : _dist,
                                            })
                                        st.dataframe(_zone_rows, use_container_width=True)
                                    else:
                                        st.caption("No open zones found.")
                                else:
                                    st.markdown(
                                        f'<div style="font-size:13px; font-weight:bold; '
                                        f'color:{total_col}; margin-bottom:8px;">'
                                        f'Total P&L (2 lots each): {pnl_header}</div>',
                                        unsafe_allow_html=True,
                                    )
                                    for t in _sim_trades:
                                        res = t["result"]
                                        col = "#1A7F37" if "T1" in res else ("#DA3633" if "SL" in res else "#8B949E")
                                        et  = pd.Timestamp(t["entry_time"]).strftime("%H:%M") if t.get("entry_time") else "—"
                                        xt  = pd.Timestamp(t["exit_time"]).strftime("%H:%M") if t.get("exit_time") else "—"
                                        pnl_str = f"+₹{t['pnl']:,.0f}" if t["pnl"] >= 0 else f"-₹{abs(t['pnl']):,.0f}"
                                        htf_z = t.get("htf_zone", "")
                                        htf_part = f'<span style="color:#57606A;font-size:10px;">HTF {htf_z}</span> &nbsp;' if htf_z else ""
                                        st.markdown(
                                            f'<div style="padding:5px 0; border-bottom:1px solid #30363D; font-size:12px;">'
                                            f'{htf_part}'
                                            f'<b>{et}</b> Entry@<b>{t["entry_price"]}</b> &nbsp;'
                                            f'SL {t["sl"]} &nbsp;T1 {t["t1"]} &nbsp;'
                                            f'Exit {xt}@{t["exit_price"] or "—"} &nbsp;'
                                            f'<b style="color:{col};">{res}</b> &nbsp;'
                                            f'<b style="color:{col};">{pnl_str}</b>'
                                            f'</div>',
                                            unsafe_allow_html=True,
                                        )

                    if opt_type == "CE":
                        ce_syms.append(sym)
                    else:
                        pe_syms.append(sym)

                # Debug caption
                _dbg_key  = dbg.get("key") or dbg.get("opt_key") or "NONE"
                _dbg_1min = dbg.get("rows_1min") or dbg.get("rows_1min_opt") or dbg.get("rows_1min_fut") or 0
                _dbg_htf  = dbg.get("rows_htf") or dbg.get("rows_htf15") or 0
                _dbg_oz   = dbg.get("open_traps") or dbg.get("open_traps_today") or 0
                st.caption(
                    f"{label} | Key: `{_dbg_key}` | "
                    f"1-min: {_dbg_1min} bars | "
                    f"HTF: {_dbg_htf} bars | "
                    f"Open zones: {_dbg_oz}"
                    + (f" | ERR: {dbg.get('fetch_err')}" if dbg.get('fetch_err') else "")
                )

            # Build pairs (S1 CE ↔ R1 PE, S2 CE ↔ R2 PE)
            if len(ce_syms) >= 1 and len(pe_syms) >= 1:
                idx_pairs.append({"label": f"{lbl_idx} Pair 1 — S1/R1",
                                   "ce_sym": ce_syms[0], "pe_sym": pe_syms[0]})
            if len(ce_syms) >= 2 and len(pe_syms) >= 2:
                idx_pairs.append({"label": f"{lbl_idx} Pair 2 — S2/R2",
                                   "ce_sym": ce_syms[1], "pe_sym": pe_syms[1]})

            # Write session log header
            log_header_key = f"log_hdr_{today_str}_{idx_name}"
            if not st.session_state.get(log_header_key):
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"\n{'='*70}\n")
                    f.write(f"SESSION {datetime.now().strftime('%d %b %Y %H:%M:%S')}  "
                            f"INDEX:{idx_name}  Prev Close:{C}  "
                            f"CE:{ce1}/{ce2} PE:{pe1}/{pe2} ({'ATM' if using_atm else 'PIVOT'})  "
                            f"Expiry:{expiry_api}  HTF:{htf_min}m LTF:{ltf_min}m\n")
                    f.write(f"{'='*70}\n")
                st.session_state[log_header_key] = True

        # WebSocket — start fresh or add new keys to running feed
        if ws_keys and token:
            existing_ws_keys = st.session_state.get("_ws_keys", [])
            if existing_ws_keys and ws_feed.is_connected():
                ws_feed.add_keys(ws_keys, hist_map)
            else:
                ws_feed.start(token, ws_keys, hist_map)

            # Register tick-level SL check — fires on every WS tick, not on UI refresh
            def _on_tick(key: str, ltp: float) -> None:
                open_t = angel_orders.get_open_trades()
                if not open_t:
                    return
                prices = {t["tracked_sym"]: ws_feed.get_ltp(t["tracked_sym"]) or ltp
                          for t in open_t}
                # prefer live_key LTP for 1-ITM trades
                for t in open_t:
                    lk = t.get("live_key", "")
                    if lk:
                        v = ws_feed.get_ltp(lk)
                        if v:
                            prices[t["tracked_sym"]] = v
                angel_orders.check_exits(prices)
            ws_feed.set_tick_callback(_on_tick)

            st.session_state["_ws_keys"] = existing_ws_keys + ws_keys

        # Append new contracts to existing ones — never wipe old scripts
        _existing_contracts = st.session_state.get("_contracts", [])
        _existing_pairs     = st.session_state.get("_idx_pairs", [])
        st.session_state["_contracts"] = _existing_contracts + contracts
        st.session_state["_idx_pairs"] = _existing_pairs + idx_pairs
        st.session_state["_log_path"]  = log_path
        _prev_settings = st.session_state.get("_settings", {})
        _prev_settings.update({
            "ltf_min"        : ltf_min,
            "sl_buffer"      : sl_buffer,
            "sl_dir"         : sl_dir,
            "track_pair1"    : track_pair1,
            "track_pair2"    : track_pair2,
            "selected_indices": list(_inited_set | set(_new_indices)),
        })
        st.session_state["_settings"] = _prev_settings
        st.session_state["_initialized_indices"] = _inited_set | set(_new_indices)
        st.session_state["_initialized"] = True

    else:
        # Update mutable settings without re-scanning
        if "_settings" in st.session_state:
            s = st.session_state["_settings"]
            s["ltf_min"]     = ltf_min
            s["sl_buffer"]   = sl_buffer
            s["sl_dir"]      = sl_dir
            s["track_pair1"] = track_pair1
            s["track_pair2"] = track_pair2

    # ── Live panel (fragment — runs every 2 s) ──────────────────────────────────
    _live_panel()


if __name__ == "__main__":
    main()
