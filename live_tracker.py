"""
live_tracker.py - Live HTF->LTF Trap Tracker
=============================================
Run: streamlit run live_tracker.py

Morning flow (before 09:15):
  1. Fetch prev-day Nifty spot -> pivot -> S1/S2 CE + R1/R2 PE strikes
  2. Fetch prev-week + current-week 1-min data for each strike
  3. Run HTF scan -> show only OPEN/TRAPPED zones (not CLOSED - those are history)
  4. These are your live watch zones for the session

During market (09:15 - 15:30):
  5. Auto-refresh every 30s
  6. Scan 5-min bars inside each HTF zone
  7. ORANGE POPUP when LTF bears get trapped (waiting for retest)
  8. GREEN POPUP when entry price hit -> trade live
  9. Show live LTP + running P&L for each active position
"""

import os
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import time
from datetime import datetime, timedelta, date
from pathlib import Path

# Load .env for EC2 deployments
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from config import (
    HTF_MINUTES, LTF_MINUTES,
    DEFAULT_SL_BUFFER, DEFAULT_QTY, DEFAULT_LOT_SIZE,
    CLR_GREEN, CLR_RED, CLR_BLUE, CLR_ORANGE, CLR_MUTED,
)
from data import (
    fetch_spot_prev_day, get_instrument_key, fetch_1min,
    resample_tf, pivot_levels,
)
from scanner import scan_htf, scan_ltf

# ------------------------------------------------------------------------------
st.set_page_config(
    page_title="Live Trap Tracker",
    page_icon="L",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
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
.pnl-card {
    background: #F5F7FA; border: 1px solid #D0D7DE;
    border-radius: 8px; padding: 16px; margin: 6px 0;
}
.zone-box {
    background: #F0F4F8; border: 1px solid #D0D7DE;
    border-radius: 6px; padding: 10px 14px; margin: 4px 0;
    font-size: 13px; color: #1A1A1A;
}
</style>
""", unsafe_allow_html=True)

# ------------------------------------------------------------------------------
LOG_DIR = Path(os.environ.get("TRAP_LOG_DIR", Path(__file__).parent / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

MARKET_OPEN  = datetime.strptime("09:15", "%H:%M").time()
MARKET_CLOSE = datetime.strptime("15:30", "%H:%M").time()


def is_market_open() -> bool:
    if date.today().weekday() >= 5:
        return False
    return MARKET_OPEN <= datetime.now().time() <= MARKET_CLOSE


def log_event(log_path: Path, tag: str, msg: str) -> str:
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{tag:15s}] {msg}"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return line


def browser_notify(title: str, body: str):
    """Fire a browser push notification (user must allow once)."""
    js = f"""
    <script>
    function notify() {{
        if (Notification.permission === "granted") {{
            new Notification("{title}", {{ body: "{body}" }});
        }} else if (Notification.permission !== "denied") {{
            Notification.requestPermission().then(p => {{
                if (p === "granted") new Notification("{title}", {{ body: "{body}" }});
            }});
        }}
    }}
    notify();
    </script>
    """
    components.html(js, height=0)


def round_n(v: float, step: int) -> int:
    return int(round(v / step) * step)

def next_weekday(ref: date, weekday: int) -> date:
    """Next occurrence of weekday (0=Mon … 6=Sun) strictly after ref."""
    days = (weekday - ref.weekday()) % 7
    if days == 0:
        days = 7
    return ref + timedelta(days=days)

def next_tuesday(ref: date) -> date:
    return next_weekday(ref, 1)

def next_friday(ref: date) -> date:
    return next_weekday(ref, 4)

# Index-specific config
INDEX_CONFIG = {
    "Nifty": {
        "label"        : "Nifty 50",
        "strike_step"  : 100,       # default; overridden by sidebar
        "expiry_fn"    : next_tuesday,
        "symbol_prefix": "NIFTY",
        "exchange"     : "NSE_FO",
        "key_prefix"   : "NIFTY",
    },
    "Sensex": {
        "label"        : "Sensex",
        "strike_step"  : 100,
        "expiry_fn"    : next_friday,
        "symbol_prefix": "SENSEX",
        "exchange"     : "BSE_FO",
        "key_prefix"   : "SENSEX",
    },
}

def trading_symbol(strike: int, opt_type: str, expiry: date,
                   prefix: str = "NIFTY") -> str:
    return f"{prefix}{expiry.strftime('%d%b%y').upper()}{strike}{opt_type}"

def fallback_key(strike: int, opt_type: str, expiry: date,
                 exchange: str, key_prefix: str) -> str:
    """Build Upstox instrument key without API lookup."""
    return (f"{exchange}|{key_prefix}"
            f"{expiry.strftime('%y')}{expiry.strftime('%m')}{expiry.strftime('%d')}"
            f"{strike}{opt_type}")

def card(col, label, value, cls=""):
    col.markdown(f"""<div class="metric-card">
<div class="metric-label">{label}</div>
<div class="metric-value {cls}">{value}</div>
</div>""", unsafe_allow_html=True)

def sec(text):
    st.markdown(f'<div class="sec-header">{text}</div>', unsafe_allow_html=True)


# -- token ---------------------------------------------------------------------
def _get_token() -> str:
    # 1. User pasted in UI this session
    if st.session_state.get("live_token"):
        return st.session_state["live_token"]
    # 2. Streamlit secrets.toml
    try:
        t = st.secrets.get("UPSTOX_TOKEN", "") or ""
        if t:
            return t
    except Exception:
        pass
    # 3. Environment variable (EC2 .env / systemd EnvironmentFile)
    t = os.environ.get("UPSTOX_TOKEN", "")
    return t


# -- cached data fetchers ------------------------------------------------------
@st.cache_data(ttl=300)
def _fetch_spot(token, index: str = "Nifty"):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return fetch_spot_prev_day(h, index)

@st.cache_data(ttl=300)
def _fetch_key(strike, opt_type, expiry_str, token, index: str = "Nifty"):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return get_instrument_key(strike, opt_type, expiry_str, h, index)

@st.cache_data(ttl=30)   # 30s TTL for live price updates
def _fetch_data(key, from_date, to_date, token):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return fetch_1min(key, from_date, to_date, h)


def _get_instrument_key(strike, opt_type, expiry, expiry_api, token,
                        index: str = "Nifty") -> str:
    key, err = _fetch_key(strike, opt_type, expiry_api, token, index)
    if err or not key:
        cfg = INDEX_CONFIG[index]
        key = fallback_key(strike, opt_type, expiry,
                           cfg["exchange"], cfg["key_prefix"])
    return key


def _get_ltp(df1: pd.DataFrame) -> float | None:
    """Return last traded price from 1-min dataframe."""
    if df1 is None or df1.empty:
        return None
    today_bars = df1[df1.index.date == date.today()]
    if today_bars.empty:
        return None
    return round(float(today_bars["close"].iloc[-1]), 2)


# ==============================================================================
#  STEP 1 — MORNING INIT: fetch data + find OPEN HTF traps
# ==============================================================================
@st.cache_data(ttl=300)
def morning_scan(strike, opt_type, expiry_api, expiry_date_str, htf_min, token,
                 index: str = "Nifty"):
    """
    Fetch prev-week + current-week data.
    Return ONLY TRAPPED (open) HTF entries — CLOSED ones are historical, not live.
    Also returns a debug dict for troubleshooting.
    """
    expiry = datetime.strptime(expiry_date_str, "%Y-%m-%d").date()

    # Try option chain key first; capture the error for debug
    raw_key, chain_err = _fetch_key(strike, opt_type, expiry_api, token, index)
    cfg = INDEX_CONFIG[index]
    if chain_err or not raw_key:
        key = fallback_key(strike, opt_type, expiry, cfg["exchange"], cfg["key_prefix"])
        key_source = f"fallback (chain err: {chain_err})"
    else:
        key = raw_key
        key_source = "option_chain API"

    # Prev week Monday to today
    today  = date.today()
    monday_this_week = today - timedelta(days=today.weekday())
    monday_prev_week = monday_this_week - timedelta(weeks=1)
    from_date = monday_prev_week.strftime("%Y-%m-%d")
    to_date   = today.strftime("%Y-%m-%d")

    df1, fetch_err = _fetch_data(key, from_date, to_date, token)

    dbg = {
        "key": key, "key_source": key_source,
        "from": from_date, "to": to_date,
        "fetch_err": str(fetch_err) if fetch_err else None,
        "rows_1min": len(df1) if df1 is not None and not df1.empty else 0,
        "rows_htf": 0, "all_entries": 0, "open_traps": 0,
    }

    if fetch_err or df1 is None or df1.empty:
        return [], pd.DataFrame(), key, None, from_date, dbg

    df_htf = resample_tf(df1, htf_min)
    dbg["rows_htf"] = len(df_htf)
    if df_htf.empty:
        return [], pd.DataFrame(), key, df1, from_date, dbg

    df_events, entries = scan_htf(df_htf)
    dbg["all_entries"] = len(entries)

    open_traps = [e for e in entries if e["status"] == "TRAPPED"]
    dbg["open_traps"] = len(open_traps)

    return open_traps, df_events, key, df1, from_date, dbg


# ==============================================================================
#  STEP 2 — LIVE SCAN: check LTF inside open HTF zones
# ==============================================================================
def live_ltf_scan(open_traps, df1, ltf_min, sl_buffer, sym, log_path):
    """
    For each open HTF trap, scan LTF bars.
    Returns (entry_hits, ltf_alerts) — both are lists of dicts.
    """
    if df1 is None or df1.empty or not open_traps:
        return [], []

    df_ltf = resample_tf(df1, ltf_min)
    if df_ltf.empty:
        return [], []

    entry_hits = []   # LTF Ref Bar LOW hit -> BUY now
    ltf_alerts = []   # LTF bears TRAPPED -> waiting for retest

    already_notified = st.session_state.get("notified_setups", set())

    for htf_e in open_traps:
        zh      = htf_e["zone_high"]
        zl      = htf_e["zone_low"]
        tgt     = htf_e["sl"]           # HTF Ref Bar HIGH = your target
        trap_ts = pd.Timestamp(htf_e["trapped_on"])

        df_ltf_after = df_ltf[df_ltf["datetime"] >= trap_ts].copy().reset_index(drop=True)
        if len(df_ltf_after) < 2:
            continue

        htf_ref_label  = pd.Timestamp(htf_e["ref_ts"]).strftime("%d %b %y %H:%M")
        htf_trap_label = trap_ts.strftime("%d %b %y %H:%M")

        _, ltf_entries = scan_ltf(
            df_ltf_after, zh, zl,
            htf_ref_bar  = htf_ref_label,
            htf_trap_bar = htf_trap_label,
            htf_target   = tgt,
        )

        # Pick lowest Zone Low
        closed_ltf  = [e for e in ltf_entries if e["status"] == "CLOSED"]
        trapped_ltf = [e for e in ltf_entries if e["status"] == "TRAPPED"]

        if closed_ltf:
            lowest = min(closed_ltf, key=lambda e: e["zone_low"])
            uid = f"{sym}_{lowest['closed_on']}"
            setup = {
                "uid"            : uid,
                "sym"            : sym,
                "status"         : "ENTRY_HIT",
                "entry_price"    : round(lowest["entry"], 2),
                "entry_ts"       : lowest["closed_on"],
                "sl"             : round(lowest["zone_low"] - sl_buffer, 2),
                "target"         : round(tgt, 2),
                "ltf_zone_high"  : round(lowest["zone_high"], 2),
                "ltf_zone_low"   : round(lowest["zone_low"], 2),
                "htf_zone_high"  : round(zh, 2),
                "htf_zone_low"   : round(zl, 2),
                "htf_ref_bar"    : htf_ref_label,
                "htf_trap_bar"   : htf_trap_label,
            }
            entry_hits.append(setup)
            if uid not in already_notified:
                log_event(log_path, "TRADE_ENTRY",
                          f"{sym} | BUY @ {lowest['entry']:.2f} | "
                          f"SL {setup['sl']:.2f} | Target {tgt:.2f} | "
                          f"HTF Zone {zh:.2f}-{zl:.2f}")
                already_notified.add(uid)
                browser_notify(
                    f"ENTRY: {sym}",
                    f"BUY @ {lowest['entry']:.2f} | Target {tgt:.2f} | SL {setup['sl']:.2f}"
                )

        elif trapped_ltf:
            lowest = min(trapped_ltf, key=lambda e: e["zone_low"])
            uid = f"alert_{sym}_{lowest['trapped_on']}"
            setup = {
                "uid"           : uid,
                "sym"           : sym,
                "status"        : "WAITING_RETEST",
                "entry_price"   : round(lowest["entry"], 2),   # LTF Ref Bar LOW = watch price
                "sl"            : round(lowest["zone_low"] - sl_buffer, 2),
                "target"        : round(tgt, 2),
                "ltf_zone_high" : round(lowest["zone_high"], 2),
                "ltf_zone_low"  : round(lowest["zone_low"], 2),
                "ltf_bear_sl"   : round(lowest["sl"], 2),
                "htf_zone_high" : round(zh, 2),
                "htf_zone_low"  : round(zl, 2),
                "htf_ref_bar"   : htf_ref_label,
            }
            ltf_alerts.append(setup)
            if uid not in already_notified:
                log_event(log_path, "LTF_ALERT",
                          f"{sym} | 5-min bears TRAPPED @ {lowest['sl']:.2f} | "
                          f"WATCH for retest to {lowest['entry']:.2f} | "
                          f"SL {setup['sl']:.2f} | Target {tgt:.2f}")
                already_notified.add(uid)
                browser_notify(
                    f"ALERT: {sym}",
                    f"5-min bears trapped. Watch for retest to {lowest['entry']:.2f}"
                )

    st.session_state["notified_setups"] = already_notified
    return entry_hits, ltf_alerts


# ==============================================================================
#  LIVE P&L TRACKER
# ==============================================================================
def render_live_pnl(entry_hits: list, df1_map: dict, log_path: Path):
    """Show live LTP + P&L for each active trade."""
    if not entry_hits:
        return

    sec("ACTIVE TRADES — Live P&L")
    units = DEFAULT_QTY * DEFAULT_LOT_SIZE

    for setup in entry_hits:
        sym    = setup["sym"]
        df1    = df1_map.get(sym)
        ltp    = _get_ltp(df1)
        entry  = setup["entry_price"]
        sl     = setup["sl"]
        target = setup["target"]

        if ltp is not None:
            pnl          = round((ltp - entry) * units, 2)
            pnl_cls      = "green" if pnl >= 0 else "red"
            pnl_str      = f"+Rs.{pnl:,.0f}" if pnl >= 0 else f"-Rs.{abs(pnl):,.0f}"
            tgt_dist     = round(target - ltp, 2)
            sl_dist      = round(ltp - sl, 2)

            # Log P&L update
            log_event(log_path, "LIVE_PNL",
                      f"{sym} | LTP {ltp:.2f} | Entry {entry:.2f} | "
                      f"P&L {pnl_str} | Dist to Target {tgt_dist:.2f} | "
                      f"Dist to SL {sl_dist:.2f}")
        else:
            ltp     = "--"
            pnl_str = "--"
            pnl_cls = ""
            tgt_dist = "--"
            sl_dist  = "--"

        cols = st.columns(7)
        card(cols[0], "Contract",    sym,              "blue")
        card(cols[1], "Entry",       f"{entry:.2f}",   "")
        card(cols[2], "LTP",         f"{ltp}" if isinstance(ltp, str) else f"{ltp:.2f}", "blue")
        card(cols[3], "Running P&L", pnl_str,          pnl_cls)
        card(cols[4], "Target",      f"{target:.2f}",  "green")
        card(cols[5], "SL",          f"{sl:.2f}",      "red")
        card(cols[6], "To Target",   f"{tgt_dist}" if isinstance(tgt_dist, str) else f"{tgt_dist:.2f}", "green")

        st.markdown(
            f'<div class="alert-green">LONG {sym} @ {entry:.2f} | '
            f'LTP: {ltp} | P&L: {pnl_str} | '
            f'SL: {sl:.2f} | Target: {target:.2f} | '
            f'HTF Zone: {setup["htf_zone_high"]:.2f} - {setup["htf_zone_low"]:.2f}</div>',
            unsafe_allow_html=True,
        )


# ==============================================================================
#  MAIN
# ==============================================================================
def main():
    today_str = date.today().strftime("%Y%m%d")
    log_path  = LOG_DIR / f"live_{today_str}.txt"

    if "notified_setups" not in st.session_state:
        st.session_state["notified_setups"] = set()

    # -- sidebar ---------------------------------------------------------------
    with st.sidebar:
        st.markdown("## Live Trap Tracker")
        st.markdown(f"**{date.today().strftime('%A, %d %b %Y')}**")
        st.markdown("---")

        token_input = st.text_input("Upstox Daily Token", type="password",
                                    placeholder="Paste bearer token")
        if st.button("Set Token", use_container_width=True):
            if token_input.strip():
                st.session_state["live_token"] = token_input.strip()
                st.cache_data.clear()
                st.session_state["log_header_written"] = False
                st.success("Token saved.")
            else:
                st.error("Token cannot be empty.")

        token = _get_token()
        if token:
            st.caption(f"Token: `...{token[-12:]}`")
        else:
            st.warning("No token — paste above.")

        st.markdown("---")
        st.markdown("**Index Selection**")
        index_choice = st.radio(
            "Track", ["Nifty", "Sensex", "Both"],
            index=0, horizontal=True,
        )
        selected_indices = (["Nifty", "Sensex"] if index_choice == "Both"
                            else [index_choice])

        st.markdown("---")
        st.markdown("**Settings**")
        htf_minutes  = st.number_input("HTF (min)",        value=HTF_MINUTES, step=5, min_value=5)
        ltf_minutes  = st.number_input("LTF (min)",        value=LTF_MINUTES, step=1, min_value=1)
        sl_buffer    = st.number_input("SL Buffer (pts)",  value=DEFAULT_SL_BUFFER, step=0.5)
        refresh_secs = st.number_input("Refresh (secs)",   value=30, step=10, min_value=10)
        auto_refresh = st.toggle("Auto-refresh", value=True)

        st.markdown("---")
        if st.button("Force Refresh", use_container_width=True, type="primary"):
            st.cache_data.clear()
            st.rerun()

        st.markdown("---")
        if log_path.exists():
            with open(log_path, "r", encoding="utf-8") as f:
                log_content = f.read()
            st.download_button("Download Log", log_content,
                               file_name=log_path.name, mime="text/plain",
                               key="dl_log")
            st.caption(f"Log: `{log_path.name}`")

    token = _get_token()
    now   = datetime.now()

    st.markdown("# Live Trap Tracker")
    status_col = st.columns(3)
    card(status_col[0], "Time",          now.strftime("%H:%M:%S"),                      "blue")
    card(status_col[1], "Market",        "OPEN" if is_market_open() else "CLOSED",
         "green" if is_market_open() else "red")
    card(status_col[2], "Next Refresh",  f"{refresh_secs}s" if auto_refresh else "Manual", "")

    if not token:
        st.error("Paste your Upstox token in the sidebar to start.")
        st.stop()

    # -- Step 1 + 2: per-index spot + strikes + HTF scan ----------------------
    sec("STEP 1 — Prev-Day Spot + Today's Watch List")

    all_open_traps = {}   # sym -> (open_traps, df1, strike, opt_type)
    df1_map        = {}   # sym -> df1

    log_header_key = f"log_header_{today_str}"

    for idx_name in selected_indices:
        cfg        = INDEX_CONFIG[idx_name]
        step       = cfg["strike_step"]
        expiry     = cfg["expiry_fn"](date.today())
        expiry_api = expiry.strftime("%Y-%m-%d")
        prefix     = cfg["symbol_prefix"]
        lbl_idx    = cfg["label"]

        try:
            spot = _fetch_spot(token, idx_name)
        except Exception as ex:
            st.error(f"{lbl_idx} spot fetch failed: {ex}")
            continue

        H, L, C = spot["high"], spot["low"], spot["close"]
        levels   = pivot_levels(H, L, C)
        s1 = round_n(levels["s1"], step)
        s2 = round_n(levels["s2"], step)
        r1 = round_n(levels["r1"], step)
        r2 = round_n(levels["r2"], step)

        sc = st.columns(6)
        card(sc[0], f"{lbl_idx} Prev Close",    f"{C:,.2f}",           "blue")
        card(sc[1], f"S1 CE x{step}",           f"{s1:,}",             "green")
        card(sc[2], f"S2 CE x{step}",           f"{s2:,}",             "green")
        card(sc[3], f"R1 PE x{step}",           f"{r1:,}",             "red")
        card(sc[4], f"R2 PE x{step}",           f"{r2:,}",             "red")
        card(sc[5], f"{lbl_idx} Expiry",        expiry.strftime("%d %b %y"), "")

        if not st.session_state.get(log_header_key):
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*70}\n")
                f.write(f"SESSION: {now.strftime('%d %b %Y %H:%M:%S')}  INDEX:{idx_name}\n")
                f.write(f"Prev Close:{C}  S1:{s1} S2:{s2} R1:{r1} R2:{r2}\n")
                f.write(f"Expiry:{expiry_api}  HTF:{htf_minutes}min LTF:{ltf_minutes}min SL:{sl_buffer}\n")
                f.write(f"{'='*70}\n")

        contracts = [
            (s1, "CE", f"{lbl_idx} S1 CE {s1:,}"),
            (s2, "CE", f"{lbl_idx} S2 CE {s2:,}"),
            (r1, "PE", f"{lbl_idx} R1 PE {r1:,}"),
            (r2, "PE", f"{lbl_idx} R2 PE {r2:,}"),
        ]

        sec(f"STEP 2 — {lbl_idx} Active HTF Zones (OPEN / Waiting for LTF entry)")

        for strike, opt_type, label in contracts:
            sym = trading_symbol(strike, opt_type, expiry, prefix)

            with st.spinner(f"Loading {label}..."):
                open_traps, df_events, key, df1, from_date, dbg = morning_scan(
                    strike, opt_type, expiry_api, expiry_api,
                    htf_minutes, token, idx_name,
                )

            df1_map[sym] = df1

            if df1 is not None:
                df1_live, _ = _fetch_data(key, from_date,
                                          date.today().strftime("%Y-%m-%d"), token)
                if df1_live is not None and not df1_live.empty:
                    df1_map[sym] = df1_live

            all_open_traps[sym] = (open_traps, df1_map[sym], strike, opt_type)
            ltp = _get_ltp(df1_map[sym])

            with st.expander(
                f"{label}  --  {sym}  |  "
                f"Open HTF zones: {len(open_traps)}  |  "
                f"LTP: {f'{ltp:.2f}' if ltp else '--'}",
                expanded=len(open_traps) > 0,
            ):
                # Always show debug info so issues are visible
                st.caption(
                    f"Key: `{dbg['key']}` ({dbg['key_source']}) | "
                    f"1-min bars: {dbg['rows_1min']} | HTF bars: {dbg['rows_htf']} | "
                    f"All HTF entries: {dbg['all_entries']} | Open: {dbg['open_traps']}"
                    + (f" | ERROR: {dbg['fetch_err']}" if dbg['fetch_err'] else "")
                )
                if open_traps:
                    rows = []
                    for e in open_traps:
                        rows.append({
                            "HTF Trap Bar"   : pd.Timestamp(e["trapped_on"]).strftime("%d %b %y %H:%M"),
                            "HTF Ref Bar"    : pd.Timestamp(e["ref_ts"]).strftime("%d %b %y %H:%M"),
                            "Zone HIGH"      : round(e["zone_high"], 2),
                            "Zone LOW"       : round(e["zone_low"], 2),
                            "1/3 Trigger"    : round(e["zone_trigger"], 2),
                            "Target (BearSL)": round(e["sl"], 2),
                        })
                    df_open = pd.DataFrame(rows)
                    st.dataframe(df_open, width="stretch",
                                 height=min(42 * len(df_open) + 50, 250), hide_index=True)

                    for e in open_traps:
                        st.markdown(
                            f'<div class="zone-box">'
                            f'<b>{sym}</b> | WATCH ZONE | '
                            f'High: <b>{e["zone_high"]:.2f}</b>  '
                            f'Low: <b>{e["zone_low"]:.2f}</b>  '
                            f'Enter LTF scan when price &lt; <b>{e["zone_trigger"]:.2f}</b>  |  '
                            f'Target: <b>{e["sl"]:.2f}</b></div>',
                            unsafe_allow_html=True,
                        )
                else:
                    if not df_events.empty:
                        st.info(f"All {sym} HTF traps are CLOSED (historical). No open zones today.")
                    else:
                        st.info(f"No HTF traps found for {sym} in prev+current week.")

    st.session_state[log_header_key] = True

    # -- Step 3: Live LTF scan -------------------------------------------------
    sec("STEP 3 — Live LTF Alerts + Entry Signals")

    all_entry_hits = []
    all_ltf_alerts = []

    log_event(log_path, "SCAN", f"Refresh at {now.strftime('%H:%M:%S')}")

    for sym, (open_traps, df1, strike, opt_type) in all_open_traps.items():
        if not open_traps or df1 is None:
            continue
        entry_hits, ltf_alerts = live_ltf_scan(
            open_traps, df1, ltf_minutes, sl_buffer, sym, log_path
        )
        all_entry_hits.extend(entry_hits)
        all_ltf_alerts.extend(ltf_alerts)

    # Orange alerts — LTF bears trapped, waiting for retest
    if all_ltf_alerts:
        for s in all_ltf_alerts:
            st.markdown(
                f'<div class="alert-orange">'
                f'ALERT: {s["sym"]} | 5-min bears TRAPPED @ bear SL {s["ltf_bear_sl"]:.2f} | '
                f'WATCH: price returns to {s["entry_price"]:.2f} (LTF Ref Bar LOW) | '
                f'SL: {s["sl"]:.2f} | Target: {s["target"]:.2f} | '
                f'HTF Zone: {s["htf_zone_high"]:.2f} - {s["htf_zone_low"]:.2f}</div>',
                unsafe_allow_html=True,
            )
            st.toast(f"ALERT {s['sym']}: Watch {s['entry_price']:.2f}", icon="!")
    elif not all_entry_hits:
        st.info("No LTF setups triggered yet. Monitoring open HTF zones.")

    # -- Step 4: Live P&L for active trades ------------------------------------
    render_live_pnl(all_entry_hits, df1_map, log_path)

    # -- Live log tail ---------------------------------------------------------
    sec("Today's Event Log")
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Show last 30 lines
        tail = "".join(lines[-30:])
        st.text_area("Event Log", tail, height=250, key="log_tail",
                     label_visibility="collapsed")

    # -- Auto-refresh ----------------------------------------------------------
    if auto_refresh:
        if is_market_open():
            st.caption(f"Next refresh in {refresh_secs}s  |  {now.strftime('%H:%M:%S')}")
            time.sleep(refresh_secs)
            st.cache_data.clear()
            st.rerun()
        else:
            st.caption("Market closed. Auto-refresh paused. Will resume at 09:15.")


if __name__ == "__main__":
    main()
else:
    main()
