"""
trap_ui.py  --  Nifty Trap Scanner
Tab 1 : Option 75-min Trap Scanner  (main trading tool)
Tab 2 : Daily Nifty Trap Scanner    (52-week validation)

Run: streamlit run trap_ui.py
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import requests
from datetime import datetime, timedelta, date

# ── page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Nifty Trap Scanner",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── WHITE THEME ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
html, body, [data-testid="stAppViewContainer"] {
    background-color: #FFFFFF;
    color: #1A1A2E;
    font-family: 'Segoe UI', sans-serif;
}
[data-testid="stSidebar"] {
    background-color: #F5F7FA;
    border-right: 1px solid #DDE1E7;
}
h1, h2, h3 { color: #1565C0; }
.metric-card {
    background: #F5F7FA;
    border: 1px solid #DDE1E7;
    border-radius: 8px;
    padding: 14px 18px;
    text-align: center;
}
.metric-label { color: #6B7280; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
.metric-value { color: #1A1A2E; font-size: 22px; font-weight: bold; margin-top: 4px; }
.metric-value.green  { color: #1B5E20; }
.metric-value.red    { color: #B71C1C; }
.metric-value.blue   { color: #1565C0; }
.metric-value.orange { color: #E65100; }
.sec-header {
    background: #EFF3FB;
    border-left: 4px solid #1565C0;
    padding: 8px 14px;
    margin: 18px 0 10px 0;
    font-size: 14px;
    font-weight: 600;
    color: #1565C0;
    border-radius: 0 4px 4px 0;
}
div[data-testid="stDataFrame"] { border: 1px solid #DDE1E7; border-radius: 6px; }
</style>
""", unsafe_allow_html=True)

# ── token — sidebar input wins, then secrets.toml, then session state ──────────
# Token is resolved at render time so sidebar input takes effect immediately.
_FALLBACK_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ"
    ".eyJzdWIiOiI0SkNIRDciLCJqdGkiOiI2YTJlNWEyOTFiZTRjMDQyZTg1YTg3MTMiLC"
    "Jpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaWF0IjoxNzgxND"
    "IyNjMzLCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNlcnZpY2UiLCJleHAiOjE3ODE0NzQ0MD"
    "B9.QEijmZUpQ8RRUjpJf3dKyRaXZQ_UJfW_gnXVK0p6TS8"
)

def _get_token() -> str:
    # 1. sidebar live input (stored in session state after user submits)
    if st.session_state.get("live_token"):
        return st.session_state["live_token"]
    # 2. secrets.toml
    try:
        t = st.secrets.get("UPSTOX_TOKEN", "") or ""
        if t:
            return t
    except Exception:
        pass
    # 3. hardcoded fallback (for R&D — update daily)
    return _FALLBACK_TOKEN

HEADERS: dict = {}   # populated after sidebar renders


# ══════════════════════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def round_n(v: float, step: int) -> int:
    return int(round(v / step) * step)

def next_tuesday(ref: date) -> date:
    days = (1 - ref.weekday()) % 7
    if days == 0:
        days = 7
    return ref + timedelta(days=days)

def expiry_label(d: date) -> str:
    return d.strftime("%d %b %Y")

def option_key_upstox(strike: int, opt_type: str, expiry: date) -> str:
    """Upstox numeric-style constructed key (fallback only)."""
    yy = expiry.strftime("%y")
    m  = str(expiry.month)
    dd = expiry.strftime("%d")
    return f"NSE_FO|NIFTY{yy}{m}{dd}{strike}{opt_type}"

def trading_symbol(strike: int, opt_type: str, expiry: date) -> str:
    """Human-readable NSE trading symbol e.g. NIFTY26JUN2522700CE"""
    return f"NIFTY{expiry.strftime('%d%b%y').upper()}{strike}{opt_type}"

def url_encode(key: str) -> str:
    return key.replace("|", "%7C").replace(" ", "%20")

def card(col, label: str, value: str, cls: str = ""):
    col.markdown(f"""<div class="metric-card">
<div class="metric-label">{label}</div>
<div class="metric-value {cls}">{value}</div>
</div>""", unsafe_allow_html=True)

def sec(text: str):
    st.markdown(f'<div class="sec-header">{text}</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 1 — OPTION 75-MIN TRAP SCANNER
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300)
def fetch_spot_prev_day() -> dict:
    """
    Returns the last COMPLETED trading day's OHLC.
    Strategy: fetch 10 days of daily data, then pick the most recent candle
    whose date is strictly BEFORE today. This handles:
      - Today is a trading day (market open or not yet open)
      - Today is a weekend / holiday (API has no candle for today)
    """
    today_str = datetime.today().strftime("%Y-%m-%d")
    from_str  = (datetime.today() - timedelta(days=14)).strftime("%Y-%m-%d")
    url = (f"https://api.upstox.com/v2/historical-candle/"
           f"NSE_INDEX%7CNifty%2050/day/{today_str}/{from_str}")
    r = requests.get(url, headers=HEADERS, timeout=15)
    body = r.json()
    if body.get("status") != "success":
        st.error(f"Spot API error: {body}")
        st.stop()
    # candles come descending from API; reversed → ascending
    candles = list(reversed(body["data"]["candles"]))
    # pick the last candle whose date < today
    today_date = datetime.today().date()
    prev_row = None
    for c in reversed(candles):
        candle_date = datetime.strptime(c[0][:10], "%Y-%m-%d").date()
        if candle_date < today_date:
            prev_row = c
            break
    if prev_row is None:
        prev_row = candles[-1]   # fallback: use last available
    return {
        "date" : prev_row[0][:10],
        "open" : round(prev_row[1], 2),
        "high" : round(prev_row[2], 2),
        "low"  : round(prev_row[3], 2),
        "close": round(prev_row[4], 2),
    }


@st.cache_data(ttl=300)
def get_instrument_key(strike: int, opt_type: str, expiry_str: str) -> tuple:
    url = (f"https://api.upstox.com/v2/option/chain"
           f"?instrument_key=NSE_INDEX%7CNifty%2050&expiry_date={expiry_str}")
    r = requests.get(url, headers=HEADERS, timeout=15)
    body = r.json()
    if body.get("status") != "success":
        return None, body
    for item in body.get("data", []):
        for side in ["call_options", "put_options"]:
            opt = item.get(side, {})
            mkt = opt.get("market_data", {})
            key = opt.get("instrument_key") or mkt.get("instrument_key")
            s   = item.get("strike_price") or opt.get("strike_price")
            t   = "CE" if side == "call_options" else "PE"
            if key and s and int(s) == strike and t == opt_type:
                return key, None
    return None, {"msg": f"Strike {strike}{opt_type} not found in option chain"}


@st.cache_data(ttl=120)
def fetch_1min(instrument_key: str, from_date: str, to_date: str):
    encoded = url_encode(instrument_key)
    url = (f"https://api.upstox.com/v2/historical-candle/"
           f"{encoded}/1minute/{to_date}/{from_date}")
    r = requests.get(url, headers=HEADERS, timeout=20)
    body = r.json()
    if body.get("status") != "success":
        return None, body
    candles = list(reversed(body["data"]["candles"]))
    if not candles:
        return pd.DataFrame(), None
    df = pd.DataFrame(candles, columns=["ts","open","high","low","close","vol","oi"])
    df["dt"] = pd.to_datetime(df["ts"])
    df = df.set_index("dt")
    df[["open","high","low","close"]] = df[["open","high","low","close"]].round(2)
    return df[["open","high","low","close","vol"]], None


def resample_tf(df_1min: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if df_1min.empty:
        return pd.DataFrame()
    bars = []
    for day, day_df in df_1min.groupby(df_1min.index.date):
        d = day_df.between_time("09:15", "15:29")
        if d.empty:
            continue
        tz     = d.index.tz
        origin = pd.Timestamp(f"{day} 09:15:00", tz=tz)
        r75 = d.resample(f"{minutes}min", origin=origin).agg(
            open  =("open",  "first"),
            high  =("high",  "max"),
            low   =("low",   "min"),
            close =("close", "last"),
            volume=("vol",   "sum"),
        ).dropna(subset=["open"])
        bars.append(r75)
    if not bars:
        return pd.DataFrame()
    out = pd.concat(bars)
    out.index.name = "datetime"
    return out.reset_index()


def scan_75min(df75: pd.DataFrame):
    """
    BEARISH TRAP with ZONE logic on any-timeframe bars.

    ENTRY:  curr LOW < prev LOW
              → Bear entered at prev LOW (entry_level), SL at prev HIGH

    TRAP:   future bar HIGH > SL
              → Bears stopped out
              → ZONE defined:
                  Zone HIGH    = entry_level        (Ref Bar LOW = where bears entered)
                  Zone LOW     = Trap Bar LOW       (candle that hit SL)
                  Zone Trigger = Zone LOW + (Zone HIGH - Zone LOW) / 3  ← 1/3 from bottom

    CLOSED: after TRAPPED, any future bar LOW <= Zone Trigger
              → Price returned into zone and reached 1/3 level = 0-loss exit for institution
    """
    entries = []
    events  = []

    def make(ref_ts, entry, sl):
        return {"ref_ts": ref_ts, "entry": entry, "sl": sl,
                "status": "ACTIVE", "trapped_on": None,
                "zone_high": None, "zone_low": None, "zone_trigger": None,
                "closed_on": None, "event_idx": None}

    for i in range(1, len(df75)):
        prev = df75.iloc[i - 1]
        curr = df75.iloc[i]
        ts   = curr["datetime"]

        for e in entries:
            if e["status"] == "CLOSED":
                continue

            if e["status"] == "ACTIVE" and curr["high"] > e["sl"]:
                e["status"]      = "TRAPPED"
                e["trapped_on"]  = ts
                # ── Zone calculation ──────────────────────────────────────────
                zone_high    = e["entry"]                           # Ref Bar LOW
                zone_low     = curr["low"]                          # Trap Bar LOW
                zone_trigger = zone_low + (zone_high - zone_low) / 3
                e["zone_high"]    = zone_high
                e["zone_low"]     = zone_low
                e["zone_trigger"] = zone_trigger
                e["event_idx"]    = len(events)
                events.append({
                    "Trap Bar"     : ts,
                    "Ref Bar"      : e["ref_ts"],
                    "Entry Level"  : e["entry"],
                    "SL Level"     : e["sl"],
                    "Zone High"    : round(zone_high,    2),
                    "Zone Low"     : round(zone_low,     2),
                    "Zone Trigger" : round(zone_trigger, 2),
                    "Status"       : "OPEN",
                    "Close Bar"    : pd.NaT,
                })

            if e["status"] == "TRAPPED" and curr["low"] <= e["zone_trigger"]:
                e["status"]    = "CLOSED"
                e["closed_on"] = ts
                events[e["event_idx"]]["Status"]    = "CLOSED"
                events[e["event_idx"]]["Close Bar"] = ts

        if curr["low"] < prev["low"]:
            entries.append(make(prev["datetime"], prev["low"], prev["high"]))

    return pd.DataFrame(events) if events else pd.DataFrame(), entries


def build_75min_chart(df75: pd.DataFrame, trap_row: dict, context_bars: int = 10) -> go.Figure:
    trap_ts  = pd.Timestamp(trap_row["Trap Bar"])
    close_ts = pd.Timestamp(trap_row["Close Bar"]) if pd.notna(trap_row.get("Close Bar")) else None
    entry    = float(trap_row["Entry Level"])
    sl       = float(trap_row["SL Level"])
    status   = trap_row["Status"]

    your_entry = sl
    your_sl    = entry
    trade_dir  = "BULLISH (BUY)"

    idx = df75[df75["datetime"] <= trap_ts].index
    if len(idx) == 0:
        return go.Figure()

    start_i = max(0, idx[-1] - context_bars)
    end_i   = len(df75) - 1
    if close_ts is not None:
        ci = df75[df75["datetime"] <= close_ts].index
        if len(ci):
            end_i = min(len(df75) - 1, ci[-1] + 5)

    w = df75.iloc[start_i : end_i + 1].copy()
    x_vals = w["datetime"].dt.strftime("%d-%b-%y %H:%M")

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=x_vals,
        open=w["open"], high=w["high"], low=w["low"], close=w["close"],
        increasing_line_color="#1B5E20", increasing_fillcolor="#1B5E20",
        decreasing_line_color="#B71C1C", decreasing_fillcolor="#B71C1C",
        name="75-min bar", showlegend=False,
    ))

    x0, x1 = x_vals.iloc[0], x_vals.iloc[-1]

    # Zone values from trap_row (if present)
    zone_high    = trap_row.get("Zone High")
    zone_low     = trap_row.get("Zone Low")
    zone_trigger = trap_row.get("Zone Trigger")

    # SL level — orange dashed (where trap fired)
    fig.add_shape(type="line", x0=x0, x1=x1, y0=sl, y1=sl,
                  line=dict(color="#E65100", width=1.5, dash="dash"))
    fig.add_annotation(x=x1, y=sl, text=f"  SL Hit {sl:.2f}",
                       showarrow=False, xanchor="left", font=dict(color="#E65100", size=11))

    # Bear entry (your SL) — red dashed
    fig.add_shape(type="line", x0=x0, x1=x1, y0=your_sl, y1=your_sl,
                  line=dict(color="#B71C1C", width=1.5, dash="dash"))
    fig.add_annotation(x=x0, y=your_sl, text=f"Bear Entry {your_sl:.2f}  ",
                       showarrow=False, xanchor="right", font=dict(color="#B71C1C", size=11))

    # Zone — shaded band + boundary lines
    if zone_high is not None and zone_low is not None:
        zone_high = float(zone_high)
        zone_low  = float(zone_low)
        # shaded zone area
        fig.add_shape(type="rect", x0=x0, x1=x1,
                      y0=zone_low, y1=zone_high,
                      fillcolor="rgba(21,101,192,0.08)",
                      line=dict(width=0))
        # zone high line — blue solid
        fig.add_shape(type="line", x0=x0, x1=x1, y0=zone_high, y1=zone_high,
                      line=dict(color="#1565C0", width=1.5, dash="solid"))
        fig.add_annotation(x=x1, y=zone_high, text=f"  Zone High {zone_high:.2f}",
                           showarrow=False, xanchor="left", font=dict(color="#1565C0", size=11))
        # zone low line — blue dashed
        fig.add_shape(type="line", x0=x0, x1=x1, y0=zone_low, y1=zone_low,
                      line=dict(color="#1565C0", width=1.2, dash="dot"))
        fig.add_annotation(x=x1, y=zone_low, text=f"  Zone Low {zone_low:.2f}",
                           showarrow=False, xanchor="left", font=dict(color="#1565C0", size=10))

    if zone_trigger is not None:
        zone_trigger = float(zone_trigger)
        # 1/3 trigger — green dashed
        fig.add_shape(type="line", x0=x0, x1=x1, y0=zone_trigger, y1=zone_trigger,
                      line=dict(color="#1B5E20", width=1.5, dash="dash"))
        fig.add_annotation(x=x0, y=zone_trigger, text=f"1/3 Trigger {zone_trigger:.2f}  ",
                           showarrow=False, xanchor="right", font=dict(color="#1B5E20", size=11))

    # Trap vertical line
    trap_x = trap_ts.strftime("%d-%b-%y %H:%M")
    if trap_x in x_vals.values:
        fig.add_vline(x=trap_x, line_width=1.5, line_dash="dot", line_color="#E65100",
                      annotation_text="Trap", annotation_position="top",
                      annotation_font_color="#E65100")

    # Close vertical line
    if close_ts is not None:
        close_x = close_ts.strftime("%d-%b-%y %H:%M")
        if close_x in x_vals.values:
            fig.add_vline(x=close_x, line_width=1.5, line_dash="dot", line_color="#6B7280",
                          annotation_text="Closed", annotation_position="top",
                          annotation_font_color="#6B7280")

    status_badge = "🟠 OPEN" if status == "OPEN" else "⚪ CLOSED"
    fig.update_layout(
        title=dict(
            text=f"BEARS TRAPPED → {trade_dir}  |  {status_badge}  |  "
                 f"Trap: {trap_ts.strftime('%d %b %Y %H:%M')}",
            font=dict(color="#1A1A2E", size=14),
        ),
        paper_bgcolor="#FFFFFF",
        plot_bgcolor="#FAFBFC",
        font=dict(color="#1A1A2E"),
        xaxis=dict(gridcolor="#E8ECF0", rangeslider=dict(visible=False), type="category"),
        yaxis=dict(gridcolor="#E8ECF0"),
        margin=dict(l=60, r=140, t=60, b=40),
        height=480,
    )
    return fig


def _scan_one_contract(label, strike, opt_type, cls, expiry, expiry_api,
                       from_date, to_date, tf_minutes, chart_context):
    """Fetch, resample, scan and render results for one option contract."""
    sym = trading_symbol(strike, opt_type, expiry)

    with st.spinner(f"Looking up instrument key for {sym}..."):
        key, err = get_instrument_key(strike, opt_type, expiry_api)

    if err or not key:
        key = option_key_upstox(strike, opt_type, expiry)
        key_status = f"⚠️ Constructed (chain lookup failed)"
    else:
        key_status = f"✓ From option chain"

    c1, c2, c3 = st.columns(3)
    card(c1, "Trading Symbol", sym,                          cls)
    card(c2, "Instrument Key", key,                          "blue")
    card(c3, "Key Status",     key_status,                   "")
    st.markdown("<br>", unsafe_allow_html=True)

    with st.spinner(f"Fetching 1-min data for {sym}..."):
        df1min, err = fetch_1min(key, from_date, to_date)

    if err:
        st.error(f"1-min fetch failed: {err}")
        return
    if df1min is None or df1min.empty:
        st.warning(f"No 1-min data for `{sym}` — contract may not be active yet.")
        return

    # resample to chosen timeframe
    df_tf = resample_tf(df1min, tf_minutes)
    if df_tf.empty:
        st.warning(f"Resample to {tf_minutes}-min produced no bars.")
        return

    st.caption(f"{len(df1min):,} 1-min bars  →  {len(df_tf)} bars of {tf_minutes}-min")

    df_events, all_entries = scan_75min(df_tf)   # scan_75min works on any timeframe
    total      = len(df_events)
    closed     = int((df_events["Status"] == "CLOSED").sum()) if total else 0
    open_      = total - closed
    open_traps = [e for e in all_entries if e["status"] == "TRAPPED"]

    m1, m2, m3 = st.columns(3)
    card(m1, "Bears Trapped",   str(total),  "blue")
    card(m2, "Closed (0-loss)", str(closed), "green")
    card(m3, "Still Open",      str(open_),  "orange")
    st.markdown("<br>", unsafe_allow_html=True)

    if not df_events.empty:
        disp = df_events.copy()
        for col in ["Trap Bar", "Ref Bar", "Close Bar"]:
            disp[col] = disp[col].apply(
                lambda x: x.strftime("%d %b %y %H:%M") if pd.notna(x) else "-")
        for col in ["Entry Level", "SL Level", "Zone High", "Zone Low", "Zone Trigger"]:
            disp[col] = disp[col].map("{:.2f}".format)

        def sc(val):
            if val == "OPEN":   return "color:#E65100;font-weight:bold;"
            if val == "CLOSED": return "color:#6B7280;"
            return ""

        st.dataframe(
            disp[["Trap Bar","Ref Bar","Entry Level","SL Level",
                  "Zone High","Zone Low","Zone Trigger","Status","Close Bar"]]
            .style.map(sc, subset=["Status"]),
            use_container_width=True, height=300, hide_index=True,
        )

        # chart drilldown
        df_r = df_events.reset_index(drop=True)
        drop_labels = [
            f"{row['Trap Bar'].strftime('%d %b %y %H:%M')}  |  BEARS TRAPPED  "
            f"|  Entry {row['Entry Level']:.2f}  SL {row['SL Level']:.2f}  |  {row['Status']}"
            for _, row in df_r.iterrows()
        ]
        sel_label = st.selectbox("Select trap to chart", drop_labels,
                                 key=f"sel_{sym}", index=0)
        sel_trap  = df_r.iloc[drop_labels.index(sel_label)].to_dict()

        if st.button(f"Show Chart — {sym}", type="primary", key=f"btn_{sym}"):
            fig = build_75min_chart(df_tf, sel_trap, context_bars=chart_context)
            st.plotly_chart(fig, use_container_width=True)
            status = sel_trap["Status"]
            s_col  = "#E65100" if status == "OPEN" else "#6B7280"
            st.markdown(f"""
<div style="background:#F5F7FA;border:1px solid #DDE1E7;border-radius:8px;
            padding:14px 22px;margin-top:10px;display:flex;gap:36px;flex-wrap:wrap;">
  <div><div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Contract</div>
       <div style="color:#1565C0;font-size:18px;font-weight:bold;">{sym}</div></div>
  <div><div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Who Trapped</div>
       <div style="color:#1B5E20;font-size:18px;font-weight:bold;">BEARS</div></div>
  <div><div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Signal</div>
       <div style="color:#1B5E20;font-size:18px;font-weight:bold;">BULLISH (BUY)</div></div>
  <div><div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Your Entry</div>
       <div style="color:#1565C0;font-size:18px;font-weight:bold;">{sel_trap['SL Level']:.2f}</div></div>
  <div><div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Your SL</div>
       <div style="color:#B71C1C;font-size:18px;font-weight:bold;">{sel_trap['Entry Level']:.2f}</div></div>
  <div><div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Status</div>
       <div style="color:{s_col};font-size:18px;font-weight:bold;">{status}</div></div>
</div>""", unsafe_allow_html=True)
    else:
        st.info(f"No bearish traps detected for {sym} in this window.")

    if open_traps:
        st.markdown("**Open traps — market NOT returned to entry yet:**")
        rows = [{"Trapped On"  : e["trapped_on"].strftime("%d %b %y %H:%M"),
                 "Entry Level" : f"{e['entry']:.2f}",
                 "SL Level"    : f"{e['sl']:.2f}"}
                for e in sorted(open_traps, key=lambda x: x["trapped_on"])]
        st.dataframe(pd.DataFrame(rows), use_container_width=True,
                     height=180, hide_index=True)

    with st.expander(f"Raw {tf_minutes}-min bars — {sym}"):
        rc = df_tf.copy()
        rc["datetime"] = rc["datetime"].dt.strftime("%d %b %y %H:%M")
        rc["type"] = rc.apply(lambda r: "BULL" if r["close"] > r["open"] else "BEAR", axis=1)
        def ct(v):
            return "color:#1B5E20;font-weight:bold;" if v=="BULL" else "color:#B71C1C;font-weight:bold;"
        st.dataframe(rc.style.map(ct, subset=["type"]),
                     use_container_width=True, height=350, hide_index=True)


def render_option_scanner(round_step: int, tf_minutes: int, weeks_back: int, chart_context: int):
    sec("STEP 1 : Prev-Day Nifty Spot")

    with st.spinner("Fetching prev-day Nifty spot..."):
        spot = fetch_spot_prev_day()

    H, L, C = spot["high"], spot["low"], spot["close"]

    # ── Pivot levels (raw + rounded) ──────────────────────────────────────────
    pivot_raw = (H + L + C) / 3
    r1_raw    = (pivot_raw * 2) - L
    s1_raw    = (pivot_raw * 2) - H
    r2_raw    = pivot_raw + (H - L)
    s2_raw    = pivot_raw - (H - L)

    pivot = round_n(pivot_raw, round_step)
    r1    = round_n(r1_raw,    round_step)
    s1    = round_n(s1_raw,    round_step)
    r2    = round_n(r2_raw,    round_step)
    s2    = round_n(s2_raw,    round_step)

    expiry     = next_tuesday(date.today())
    expiry_api = expiry.strftime("%Y-%m-%d")
    to_date    = datetime.today().strftime("%Y-%m-%d")
    from_date  = (datetime.today() - timedelta(weeks=weeks_back + 1)).strftime("%Y-%m-%d")

    # ── Prev day cards ────────────────────────────────────────────────────────
    c1,c2,c3,c4,c5 = st.columns(5)
    card(c1, "Prev Day",   spot["date"],          "blue")
    card(c2, "Prev High",  f"{H:,.2f}",           "green")
    card(c3, "Prev Low",   f"{L:,.2f}",           "red")
    card(c4, "Prev Close", f"{C:,.2f}",           "")
    card(c5, "Expiry",     expiry_label(expiry),  "blue")

    st.markdown("<br>", unsafe_allow_html=True)
    sec(f"STEP 2 : Pivot Levels  (raw  →  rounded to ×{round_step})")

    # Show raw and rounded side by side
    cols = st.columns(10)
    for i, (name, raw, rounded, cls) in enumerate([
        ("Pivot",  pivot_raw, pivot, "orange"),
        ("R1",     r1_raw,    r1,    "green"),
        ("R2",     r2_raw,    r2,    "green"),
        ("S1",     s1_raw,    s1,    "red"),
        ("S2",     s2_raw,    s2,    "red"),
    ]):
        card(cols[i*2],     f"{name} (raw)", f"{raw:,.2f}",   "")
        card(cols[i*2+1],   f"{name} ×{round_step}", f"{rounded:,}", cls)

    # CE strikes = S1, S2  (support below pivot → CE is ITM)
    # PE strikes = R1, R2  (resistance above pivot → PE is ITM)
    st.markdown("<br>", unsafe_allow_html=True)
    sec(f"STEP 3 : ITM Strikes  |  CE = S1, S2  |  PE = R1, R2")

    c1, c2, c3, c4 = st.columns(4)
    card(c1, "S1 CE Strike", f"{s1:,}", "green")
    card(c2, "S2 CE Strike", f"{s2:,}", "green")
    card(c3, "R1 PE Strike", f"{r1:,}", "red")
    card(c4, "R2 PE Strike", f"{r2:,}", "red")

    st.markdown("<br>", unsafe_allow_html=True)
    sec(f"STEP 4 : Trap Scan  |  {tf_minutes}-min  |  BEARISH TRAPS only")

    tab_s1ce, tab_s2ce, tab_r1pe, tab_r2pe = st.tabs([
        f"S1 CE — {s1:,}",
        f"S2 CE — {s2:,}",
        f"R1 PE — {r1:,}",
        f"R2 PE — {r2:,}",
    ])
    with tab_s1ce:
        _scan_one_contract("S1 CE", s1, "CE", "green",
                           expiry, expiry_api, from_date, to_date, tf_minutes, chart_context)
    with tab_s2ce:
        _scan_one_contract("S2 CE", s2, "CE", "green",
                           expiry, expiry_api, from_date, to_date, tf_minutes, chart_context)
    with tab_r1pe:
        _scan_one_contract("R1 PE", r1, "PE", "red",
                           expiry, expiry_api, from_date, to_date, tf_minutes, chart_context)
    with tab_r2pe:
        _scan_one_contract("R2 PE", r2, "PE", "red",
                           expiry, expiry_api, from_date, to_date, tf_minutes, chart_context)


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 2 — DAILY NIFTY TRAP SCANNER (52-week validation)
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=300)
def fetch_daily_candles(from_date: str, to_date: str) -> pd.DataFrame:
    url = (f"https://api.upstox.com/v2/historical-candle/"
           f"NSE_INDEX%7CNifty%2050/day/{to_date}/{from_date}")
    r = requests.get(url, headers=HEADERS, timeout=20)
    body = r.json()
    if body.get("status") != "success":
        st.error(f"Daily candle API error: {body}")
        st.stop()
    candles = list(reversed(body["data"]["candles"]))
    df = pd.DataFrame(candles, columns=["ts","open","high","low","close","vol","oi"])
    df["date"] = pd.to_datetime(df["ts"].str[:10])
    df[["open","high","low","close"]] = df[["open","high","low","close"]].round(2)
    df["candle"] = df.apply(lambda r: "BULL" if r["close"] > r["open"] else "BEAR", axis=1)
    return df[["date","open","high","low","close","candle"]].reset_index(drop=True)


def scan_daily(df: pd.DataFrame):
    entries = []
    events  = []

    def make(kind, ref_date, entry_level, sl_level):
        return {"kind": kind, "ref_date": ref_date, "entry": entry_level,
                "sl": sl_level, "status": "ACTIVE",
                "trapped_on": None, "closed_on": None, "event_idx": None}

    for i in range(1, len(df)):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]
        dt   = curr["date"]
        for e in entries:
            if e["status"] == "CLOSED":
                continue
            if e["kind"] == "BEAR":
                if e["status"] == "ACTIVE" and curr["high"] > e["sl"]:
                    e["status"]     = "TRAPPED"
                    e["trapped_on"] = dt
                    e["event_idx"]  = len(events)
                    events.append({"Trap Date": dt, "Who Got Trapped": "BEARS",
                                   "Ref Candle": e["ref_date"],
                                   "Entry Level": e["entry"], "SL Level": e["sl"],
                                   "Status": "OPEN", "Close Date": pd.NaT})
                if e["status"] == "TRAPPED" and curr["low"] <= e["entry"]:
                    e["status"]    = "CLOSED"
                    e["closed_on"] = dt
                    events[e["event_idx"]]["Status"]     = "CLOSED"
                    events[e["event_idx"]]["Close Date"] = dt
            else:
                if e["status"] == "ACTIVE" and curr["low"] < e["sl"]:
                    e["status"]     = "TRAPPED"
                    e["trapped_on"] = dt
                    e["event_idx"]  = len(events)
                    events.append({"Trap Date": dt, "Who Got Trapped": "BULLS",
                                   "Ref Candle": e["ref_date"],
                                   "Entry Level": e["entry"], "SL Level": e["sl"],
                                   "Status": "OPEN", "Close Date": pd.NaT})
                if e["status"] == "TRAPPED" and curr["high"] >= e["entry"]:
                    e["status"]    = "CLOSED"
                    e["closed_on"] = dt
                    events[e["event_idx"]]["Status"]     = "CLOSED"
                    events[e["event_idx"]]["Close Date"] = dt
        if curr["low"] < prev["low"]:
            entries.append(make("BEAR", prev["date"], prev["low"], prev["high"]))
        if curr["high"] > prev["high"]:
            entries.append(make("BULL", prev["date"], prev["high"], prev["low"]))

    return pd.DataFrame(events) if events else pd.DataFrame(), entries


def build_daily_chart(df_candles: pd.DataFrame, trap_row: dict, context_bars: int = 30) -> go.Figure:
    trap_date  = pd.Timestamp(trap_row["Trap Date"])
    close_date = pd.Timestamp(trap_row["Close Date"]) if pd.notna(trap_row["Close Date"]) else None
    entry_lvl  = float(trap_row["Entry Level"])
    sl_lvl     = float(trap_row["SL Level"])
    who        = trap_row["Who Got Trapped"]
    status     = trap_row["Status"]
    trade_dir  = "BULLISH (BUY)" if who == "BEARS" else "BEARISH (SELL)"
    your_entry = sl_lvl
    your_sl    = entry_lvl

    trap_idx = df_candles[df_candles["date"] <= trap_date].index
    if len(trap_idx) == 0:
        return go.Figure()
    start_idx = max(0, trap_idx[-1] - context_bars)
    end_idx   = len(df_candles) - 1
    if close_date is not None:
        ci = df_candles[df_candles["date"] <= close_date].index
        if len(ci):
            end_idx = min(len(df_candles) - 1, ci[-1] + 10)

    window = df_candles.iloc[start_idx : end_idx + 1].copy()
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=window["date"],
        open=window["open"], high=window["high"],
        low=window["low"],   close=window["close"],
        increasing_line_color="#1B5E20", increasing_fillcolor="#1B5E20",
        decreasing_line_color="#B71C1C", decreasing_fillcolor="#B71C1C",
        name="Nifty 50", showlegend=False,
    ))
    x0, x1 = window["date"].iloc[0], window["date"].iloc[-1]
    fig.add_shape(type="line", x0=x0, x1=x1, y0=sl_lvl, y1=sl_lvl,
                  line=dict(color="#E65100", width=1.5, dash="dash"))
    fig.add_annotation(x=x1, y=sl_lvl, text=f"  Trap {sl_lvl:.2f}",
                       showarrow=False, xanchor="left", font=dict(color="#E65100", size=11))
    fig.add_shape(type="line", x0=x0, x1=x1, y0=your_sl, y1=your_sl,
                  line=dict(color="#B71C1C", width=1.5, dash="dash"))
    fig.add_annotation(x=x0, y=your_sl, text=f"SL {your_sl:.2f}  ",
                       showarrow=False, xanchor="right", font=dict(color="#B71C1C", size=11))
    fig.add_vline(x=trap_date, line_width=1.5, line_dash="dot", line_color="#E65100",
                  annotation_text="Trap", annotation_position="top",
                  annotation_font_color="#E65100")
    if close_date is not None:
        fig.add_vline(x=close_date, line_width=1.5, line_dash="dot", line_color="#6B7280",
                      annotation_text="Closed", annotation_position="top",
                      annotation_font_color="#6B7280")
    status_badge = "🟠 OPEN" if status == "OPEN" else "⚪ CLOSED"
    fig.update_layout(
        title=dict(
            text=f"{who} TRAPPED → {trade_dir}  |  {status_badge}  |  "
                 f"Trap: {trap_date.strftime('%d %b %Y')}",
            font=dict(color="#1A1A2E", size=14),
        ),
        paper_bgcolor="#FFFFFF", plot_bgcolor="#FAFBFC", font=dict(color="#1A1A2E"),
        xaxis=dict(gridcolor="#E8ECF0", rangeslider=dict(visible=False), type="category"),
        yaxis=dict(gridcolor="#E8ECF0"),
        margin=dict(l=60, r=120, t=60, b=40), height=500,
    )
    return fig


def render_daily_scanner(weeks: int, filter_who: list, filter_status: list, context_bars: int):
    to_date   = datetime.today().strftime("%Y-%m-%d")
    from_date = (datetime.today() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")

    with st.spinner("Fetching candles..."):
        df_candles = fetch_daily_candles(from_date, to_date)
    with st.spinner("Scanning..."):
        df_events, all_entries = scan_daily(df_candles)

    total  = len(df_events)
    closed = int((df_events["Status"] == "CLOSED").sum()) if total else 0
    open_  = total - closed
    bears  = int((df_events["Who Got Trapped"] == "BEARS").sum()) if total else 0
    bulls  = int((df_events["Who Got Trapped"] == "BULLS").sum()) if total else 0

    c1,c2,c3,c4,c5 = st.columns(5)
    card(c1, "Total Traps",   str(total),  "blue")
    card(c2, "Closed",        str(closed), "")
    card(c3, "Still Open",    str(open_),  "orange")
    card(c4, "Bears Trapped", str(bears),  "green")
    card(c5, "Bulls Trapped", str(bulls),  "red")
    st.markdown("<br>", unsafe_allow_html=True)

    df_show = df_events.copy()
    if filter_who:
        df_show = df_show[df_show["Who Got Trapped"].isin(filter_who)]
    if filter_status:
        df_show = df_show[df_show["Status"].isin(filter_status)]

    sec("ALL TRAP EVENTS")
    if df_show.empty:
        st.info("No events match the current filters.")
        return

    display = df_show.copy()
    display["Trap Date"]  = display["Trap Date"].dt.strftime("%Y-%m-%d")
    display["Ref Candle"] = display["Ref Candle"].dt.strftime("%Y-%m-%d")
    display["Close Date"] = display["Close Date"].apply(
        lambda x: x.strftime("%Y-%m-%d") if pd.notna(x) else "-")
    display["Entry Level"] = display["Entry Level"].map("{:.2f}".format)
    display["SL Level"]    = display["SL Level"].map("{:.2f}".format)

    def row_color(row):
        bg = ("background-color: rgba(27,94,32,0.08);"
              if row["Who Got Trapped"] == "BEARS"
              else "background-color: rgba(183,28,28,0.08);")
        return [bg] * len(row)

    def status_color(val):
        if val == "OPEN":   return "color:#E65100;font-weight:bold;"
        if val == "CLOSED": return "color:#6B7280;"
        return ""

    st.dataframe(
        display[["Trap Date","Who Got Trapped","Ref Candle",
                 "Entry Level","SL Level","Status","Close Date"]]
        .style.apply(row_color, axis=1).map(status_color, subset=["Status"]),
        use_container_width=True, height=400, hide_index=True,
    )

    # Chart drilldown
    st.markdown("<br>", unsafe_allow_html=True)
    sec("CHART DRILLDOWN")
    df_show_r = df_show.reset_index(drop=True)
    labels = [
        f"{row['Trap Date'].strftime('%d %b %Y')}  |  {row['Who Got Trapped']} TRAPPED  "
        f"|  Entry {row['Entry Level']:.2f}  SL {row['SL Level']:.2f}  |  {row['Status']}"
        for _, row in df_show_r.iterrows()
    ]
    sel_label = st.selectbox("Select Trap", labels, index=0, key="daily_sel")
    sel_trap  = df_show_r.iloc[labels.index(sel_label)].to_dict()

    if st.button("Show Chart", type="primary", key="daily_chart_btn"):
        fig = build_daily_chart(df_candles, sel_trap, context_bars)
        st.plotly_chart(fig, use_container_width=True)

    # Open traps
    open_entries = [e for e in all_entries if e["status"] == "TRAPPED"]
    if open_entries:
        sec("CURRENTLY OPEN TRAPS")
        rows = [{"Trapped On"  : e["trapped_on"].strftime("%Y-%m-%d"),
                 "Kind"        : e["kind"],
                 "Entry Level" : f"{e['entry']:.2f}",
                 "SL Level"    : f"{e['sl']:.2f}",
                 "Signal"      : "BULLISH" if e["kind"]=="BEAR" else "BEARISH"}
                for e in sorted(open_entries, key=lambda x: x["trapped_on"], reverse=True)]
        def sig_c(val):
            if val == "BULLISH": return "color:#1B5E20;font-weight:bold;"
            if val == "BEARISH": return "color:#B71C1C;font-weight:bold;"
            return ""
        st.dataframe(pd.DataFrame(rows).style.map(sig_c, subset=["Signal"]),
                     use_container_width=True, height=300, hide_index=True)

    with st.expander("Raw Candle Data"):
        rc = df_candles.copy()
        rc["date"] = rc["date"].dt.strftime("%Y-%m-%d")
        def ct(v):
            return "color:#1B5E20;font-weight:bold;" if v=="BULL" else "color:#B71C1C;font-weight:bold;"
        st.dataframe(rc.style.map(ct, subset=["candle"]),
                     use_container_width=True, height=400, hide_index=True)

    st.caption(f"Data: {from_date} to {to_date} · {len(df_candles)} candles")


# ══════════════════════════════════════════════════════════════════════════════
#  SIDEBAR  +  PAGE HEADER
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## Nifty Trap Scanner")
    st.markdown("---")

    # ── Token input ────────────────────────────────────────────────────────────
    st.markdown("**Upstox API Token**")
    token_input = st.text_input(
        "Paste today's token",
        value=st.session_state.get("live_token", ""),
        type="password",
        placeholder="eyJ0eX...",
        label_visibility="collapsed",
    )
    if st.button("Set Token", use_container_width=True):
        if token_input.strip():
            st.session_state["live_token"] = token_input.strip()
            st.cache_data.clear()
            st.success("Token saved — data cache cleared.")
        else:
            st.error("Paste a valid token first.")

    current_token = _get_token()
    if current_token:
        st.caption(f"Token: `...{current_token[-12:]}`  ✓ set")
    else:
        st.warning("No token set — API calls will fail.")

    st.markdown("---")
    active_tab = st.radio("View", ["Option 75-min Traps", "Daily Nifty Traps (52w)"],
                          index=0)
    st.markdown("---")

    if active_tab == "Option 75-min Traps":
        st.markdown("**Settings**")
        round_step   = st.number_input("Round-off (pts)", value=100, step=50, min_value=50,
                                        help="All pivot levels rounded to nearest this value (multiples of 50)")
        tf_minutes   = st.number_input("Timeframe (minutes)", value=75, step=5, min_value=5,
                                        help="Bar size for trap detection")
        weeks_back   = st.selectbox("Data window",
                                    [1, 2, 3, 4], index=1,
                                    format_func=lambda w: f"Prev {w} week(s) + current")
        chart_ctx    = st.slider("Chart context (bars)", 5, 30, 10)
    else:
        weeks        = st.selectbox("Data Range (weeks)", [4, 8, 13, 26, 52], index=4,
                                    format_func=lambda w: f"{w} weeks")
        filter_who    = st.multiselect("Who Got Trapped", ["BEARS","BULLS"],
                                       default=["BEARS","BULLS"])
        filter_status = st.multiselect("Status", ["OPEN","CLOSED"],
                                       default=["OPEN","CLOSED"])
        chart_ctx     = st.slider("Chart context (daily bars)", 10, 60, 30)

    st.markdown("---")
    if st.button("🔄 Refresh / Fetch Data", use_container_width=True, type="primary"):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.markdown("""
**BEARS trapped** → BULLISH trade
**BULLS trapped** → BEARISH trade

Entry = where trapped side's SL was hit
SL = trapped side's original entry
OPEN = not yet returned to entry
CLOSED = 0-loss exit happened
""")

# ── wire HEADERS now that sidebar has resolved the token ──────────────────────
HEADERS["Authorization"] = f"Bearer {_get_token()}"
HEADERS["Accept"] = "application/json"

# ══════════════════════════════════════════════════════════════════════════════
#  RENDER
# ══════════════════════════════════════════════════════════════════════════════
if active_tab == "Option 75-min Traps":
    st.markdown(f"# Option {tf_minutes}-min Trap Scanner")
    st.markdown(f"*Prev-day Nifty → Pivot/R1/R2/S1/S2 (×{round_step}) → S1/S2 CE + R1/R2 PE → {tf_minutes}-min bars → Bearish trap detection*")
    render_option_scanner(round_step, tf_minutes, weeks_back, chart_ctx)
else:
    st.markdown("# Daily Nifty Trap Scanner  *(52-week validation)*")
    st.markdown("*Validates trap logic on Nifty 50 daily candles*")
    render_daily_scanner(weeks, filter_who, filter_status, chart_ctx)
