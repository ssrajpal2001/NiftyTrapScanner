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
    return ""

HEADERS: dict = {}   # populated after sidebar renders


# ══════════════════════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def round50(v: float) -> int:
    return int(round(v / 50) * 50)

def next_tuesday(ref: date) -> date:
    days = (1 - ref.weekday()) % 7
    if days == 0:
        days = 7
    return ref + timedelta(days=days)

def expiry_label(d: date) -> str:
    return d.strftime("%d %b %Y")

def option_key_upstox(strike: int, opt_type: str, expiry: date) -> str:
    yy = expiry.strftime("%y")
    m  = str(expiry.month)
    dd = expiry.strftime("%d")
    return f"NSE_FO|NIFTY{yy}{m}{dd}{strike}{opt_type}"

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
    today    = datetime.today().strftime("%Y-%m-%d")
    week_ago = (datetime.today() - timedelta(days=7)).strftime("%Y-%m-%d")
    url = (f"https://api.upstox.com/v2/historical-candle/"
           f"NSE_INDEX%7CNifty%2050/day/{today}/{week_ago}")
    r = requests.get(url, headers=HEADERS, timeout=15)
    body = r.json()
    if body.get("status") != "success":
        st.error(f"Spot API error: {body}")
        st.stop()
    candles = list(reversed(body["data"]["candles"]))
    row = candles[-2] if len(candles) >= 2 else candles[-1]
    return {
        "date" : row[0][:10],
        "open" : round(row[1], 2),
        "high" : round(row[2], 2),
        "low"  : round(row[3], 2),
        "close": round(row[4], 2),
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


def resample_75min(df_1min: pd.DataFrame) -> pd.DataFrame:
    if df_1min.empty:
        return pd.DataFrame()
    bars = []
    for day, day_df in df_1min.groupby(df_1min.index.date):
        d = day_df.between_time("09:15", "15:29")
        if d.empty:
            continue
        tz     = d.index.tz
        origin = pd.Timestamp(f"{day} 09:15:00", tz=tz)
        r75 = d.resample("75min", origin=origin).agg(
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
    Scans for BOTH bearish traps AND bullish traps on 75-min option bars.

    BEARISH TRAP (bears entered short):
      curr LOW < prev LOW  → BEAR entry at prev LOW, SL at prev HIGH
      future HIGH > SL     → BEARS TRAPPED
      future LOW <= entry  → BEARS CLOSED (0-loss exit)

    BULLISH TRAP (bulls entered long):
      curr HIGH > prev HIGH → BULL entry at prev HIGH, SL at prev LOW
      future LOW < SL       → BULLS TRAPPED
      future HIGH >= entry  → BULLS CLOSED (0-loss exit)
    """
    entries = []
    events  = []

    def make(kind, ref_ts, entry, sl):
        return {"kind": kind, "ref_ts": ref_ts, "entry": entry, "sl": sl,
                "status": "ACTIVE", "trapped_on": None, "closed_on": None, "event_idx": None}

    for i in range(1, len(df75)):
        prev = df75.iloc[i - 1]
        curr = df75.iloc[i]
        ts   = curr["datetime"]

        for e in entries:
            if e["status"] == "CLOSED":
                continue
            if e["kind"] == "BEAR":
                if e["status"] == "ACTIVE" and curr["high"] > e["sl"]:
                    e["status"]     = "TRAPPED"
                    e["trapped_on"] = ts
                    e["event_idx"]  = len(events)
                    events.append({
                        "Trap Bar"    : ts,
                        "Who Trapped" : "BEARS",
                        "Ref Bar"     : e["ref_ts"],
                        "Entry Level" : e["entry"],
                        "SL Level"    : e["sl"],
                        "Status"      : "OPEN",
                        "Close Bar"   : pd.NaT,
                    })
                if e["status"] == "TRAPPED" and curr["low"] <= e["entry"]:
                    e["status"]    = "CLOSED"
                    e["closed_on"] = ts
                    events[e["event_idx"]]["Status"]    = "CLOSED"
                    events[e["event_idx"]]["Close Bar"] = ts
            else:  # BULL
                if e["status"] == "ACTIVE" and curr["low"] < e["sl"]:
                    e["status"]     = "TRAPPED"
                    e["trapped_on"] = ts
                    e["event_idx"]  = len(events)
                    events.append({
                        "Trap Bar"    : ts,
                        "Who Trapped" : "BULLS",
                        "Ref Bar"     : e["ref_ts"],
                        "Entry Level" : e["entry"],
                        "SL Level"    : e["sl"],
                        "Status"      : "OPEN",
                        "Close Bar"   : pd.NaT,
                    })
                if e["status"] == "TRAPPED" and curr["high"] >= e["entry"]:
                    e["status"]    = "CLOSED"
                    e["closed_on"] = ts
                    events[e["event_idx"]]["Status"]    = "CLOSED"
                    events[e["event_idx"]]["Close Bar"] = ts

        if curr["low"] < prev["low"]:
            entries.append(make("BEAR", prev["datetime"], prev["low"], prev["high"]))
        if curr["high"] > prev["high"]:
            entries.append(make("BULL", prev["datetime"], prev["high"], prev["low"]))

    return pd.DataFrame(events) if events else pd.DataFrame(), entries


def build_75min_chart(df75: pd.DataFrame, trap_row: dict, context_bars: int = 10) -> go.Figure:
    trap_ts  = pd.Timestamp(trap_row["Trap Bar"])
    close_ts = pd.Timestamp(trap_row["Close Bar"]) if pd.notna(trap_row.get("Close Bar")) else None
    entry    = float(trap_row["Entry Level"])
    sl       = float(trap_row["SL Level"])
    who      = trap_row["Who Trapped"]
    status   = trap_row["Status"]

    your_entry = sl      # where trap fired = your trade entry
    your_sl    = entry   # trapped side's original entry = your SL
    trade_dir  = "BULLISH (BUY)" if who == "BEARS" else "BEARISH (SELL)"

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
    x_vals = w["datetime"].dt.strftime("%d-%b %H:%M")

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=x_vals,
        open=w["open"], high=w["high"], low=w["low"], close=w["close"],
        increasing_line_color="#1B5E20", increasing_fillcolor="#1B5E20",
        decreasing_line_color="#B71C1C", decreasing_fillcolor="#B71C1C",
        name="75-min bar", showlegend=False,
    ))

    x0, x1 = x_vals.iloc[0], x_vals.iloc[-1]

    # Trap SL level — orange dashed
    fig.add_shape(type="line", x0=x0, x1=x1, y0=sl, y1=sl,
                  line=dict(color="#E65100", width=1.5, dash="dash"))
    fig.add_annotation(x=x1, y=sl, text=f"  Trap/Entry {sl:.2f}",
                       showarrow=False, xanchor="left", font=dict(color="#E65100", size=11))

    # Your SL — red dashed
    fig.add_shape(type="line", x0=x0, x1=x1, y0=your_sl, y1=your_sl,
                  line=dict(color="#B71C1C", width=1.5, dash="dash"))
    fig.add_annotation(x=x0, y=your_sl, text=f"SL {your_sl:.2f}  ",
                       showarrow=False, xanchor="right", font=dict(color="#B71C1C", size=11))

    # Trap vertical line
    trap_x = trap_ts.strftime("%d-%b %H:%M")
    if trap_x in x_vals.values:
        fig.add_vline(x=trap_x, line_width=1.5, line_dash="dot", line_color="#E65100",
                      annotation_text="Trap", annotation_position="top",
                      annotation_font_color="#E65100")

    # Close vertical line
    if close_ts is not None:
        close_x = close_ts.strftime("%d-%b %H:%M")
        if close_x in x_vals.values:
            fig.add_vline(x=close_x, line_width=1.5, line_dash="dot", line_color="#6B7280",
                          annotation_text="Closed", annotation_position="top",
                          annotation_font_color="#6B7280")

    status_badge = "🟠 OPEN" if status == "OPEN" else "⚪ CLOSED"
    fig.update_layout(
        title=dict(
            text=f"{who} TRAPPED → {trade_dir}  |  {status_badge}  |  "
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


def render_option_scanner(itm_offset: int, weeks_back: int, chart_context: int):
    sec("STEP 1–2 : Prev-Day Nifty Spot  →  Pivot  →  Round to ×50")

    with st.spinner("Fetching prev-day Nifty spot..."):
        spot = fetch_spot_prev_day()

    pivot_raw = (spot["high"] + spot["low"] + spot["close"]) / 3
    pivot     = round50(pivot_raw)
    ce_strike = pivot - itm_offset
    pe_strike = pivot + itm_offset
    atm       = round50(spot["close"])
    expiry    = next_tuesday(date.today())
    expiry_api = expiry.strftime("%Y-%m-%d")

    c1,c2,c3,c4,c5,c6,c7,c8 = st.columns(8)
    card(c1, "Prev Day",         spot["date"],              "blue")
    card(c2, "Prev High",        f"{spot['high']:,.2f}",    "green")
    card(c3, "Prev Low",         f"{spot['low']:,.2f}",     "red")
    card(c4, "Prev Close",       f"{spot['close']:,.2f}",   "")
    card(c5, "Pivot (raw)",      f"{pivot_raw:,.2f}",       "")
    card(c6, "Pivot (×50)",      f"{pivot:,}",              "orange")
    card(c7, "ATM Strike",       f"{atm:,}",                "blue")
    card(c8, "Expiry (Tue)",     expiry_label(expiry),      "blue")

    st.markdown("<br>", unsafe_allow_html=True)
    sec(f"STEP 3 : ITM Strikes  (offset ±{itm_offset} pts from Pivot)")

    c1, c2 = st.columns(2)
    card(c1, f"CE Strike  (Pivot − {itm_offset})",  f"{ce_strike:,}",  "green")
    card(c2, f"PE Strike  (Pivot + {itm_offset})",  f"{pe_strike:,}",  "red")

    st.markdown("<br>", unsafe_allow_html=True)
    to_date   = datetime.today().strftime("%Y-%m-%d")
    from_date = (datetime.today() - timedelta(weeks=weeks_back + 1)).strftime("%Y-%m-%d")

    for label, strike, opt_type, cls in [
        ("CE — CALL (ITM)", ce_strike, "CE", "green"),
        ("PE — PUT  (ITM)", pe_strike, "PE", "red"),
    ]:
        sec(f"STEPS 4–7 : {label}  |  75-min Trap Scan")

        # ── Step 4: instrument key ─────────────────────────────────────────────
        with st.spinner(f"Looking up instrument key for {strike}{opt_type}..."):
            key, err = get_instrument_key(strike, opt_type, expiry_api)

        if err or not key:
            # fall back to constructed key
            key = option_key_upstox(strike, opt_type, expiry)
            st.warning(f"Option chain lookup failed ({err}). Using constructed key: `{key}`")
        else:
            st.success(f"Instrument key: `{key}`")

        c1, c2 = st.columns(2)
        card(c1, "Instrument Key", key,      cls)
        card(c2, "Data Window",    f"{from_date}  →  {to_date}", "")
        st.markdown("<br>", unsafe_allow_html=True)

        # ── Step 5: fetch 1-min ───────────────────────────────────────────────
        with st.spinner(f"Fetching 1-min data for {label}..."):
            df1min, err = fetch_1min(key, from_date, to_date)

        if err:
            st.error(f"1-min fetch failed: {err}")
            continue
        if df1min is None or df1min.empty:
            st.warning(f"No 1-min bars returned for `{key}`. Contract may not have started trading yet.")
            continue

        st.info(f"Fetched **{len(df1min):,}** 1-min bars")

        # ── Step 6: resample → 75-min ─────────────────────────────────────────
        with st.spinner("Resampling to 75-min..."):
            df75 = resample_75min(df1min)

        if df75.empty:
            st.warning("Resample produced no 75-min bars.")
            continue

        st.info(f"Resampled to **{len(df75)}** bars of 75-min")

        # ── Step 7: scan ──────────────────────────────────────────────────────
        df_events, all_entries = scan_75min(df75)

        total  = len(df_events)
        closed = int((df_events["Status"] == "CLOSED").sum()) if total else 0
        open_  = total - closed
        open_traps = [e for e in all_entries if e["status"] == "TRAPPED"]

        m1, m2, m3, m4 = st.columns(4)
        card(m1, "Traps Fired",      str(total),  "blue")
        card(m2, "Bears Trapped",    str(int((df_events["Who Trapped"]=="BEARS").sum())) if total else "0", "green")
        card(m3, "Bulls Trapped",    str(int((df_events["Who Trapped"]=="BULLS").sum())) if total else "0", "red")
        card(m4, "Still Open",       str(open_),  "orange")
        st.markdown("<br>", unsafe_allow_html=True)

        # ── Trap events table ─────────────────────────────────────────────────
        if not df_events.empty:
            disp = df_events.copy()
            for col in ["Trap Bar","Ref Bar","Close Bar"]:
                disp[col] = disp[col].apply(
                    lambda x: x.strftime("%d %b %H:%M") if pd.notna(x) else "-")
            disp["Entry Level"] = disp["Entry Level"].map("{:.2f}".format)
            disp["SL Level"]    = disp["SL Level"].map("{:.2f}".format)

            def sc(val):
                if val == "OPEN":   return "color:#E65100;font-weight:bold;"
                if val == "CLOSED": return "color:#6B7280;"
                return ""

            def wc(val):
                if val == "BEARS": return "color:#1B5E20;font-weight:bold;"
                if val == "BULLS": return "color:#B71C1C;font-weight:bold;"
                return ""

            styled = (disp.style
                      .map(sc, subset=["Status"])
                      .map(wc, subset=["Who Trapped"]))
            st.dataframe(styled, use_container_width=True, height=300, hide_index=True)

            # ── Chart drilldown ───────────────────────────────────────────────
            st.markdown("<br>", unsafe_allow_html=True)
            sec(f"CHART DRILLDOWN — {label}")

            df_events_reset = df_events.reset_index(drop=True)
            labels = [
                f"{row['Trap Bar'].strftime('%d %b %H:%M')}  |  {row['Who Trapped']} TRAPPED  "
                f"|  Entry {row['Entry Level']:.2f}  SL {row['SL Level']:.2f}  |  {row['Status']}"
                for _, row in df_events_reset.iterrows()
            ]
            sel_label = st.selectbox(f"Select trap ({label})", labels,
                                     key=f"sel_{opt_type}", index=0)
            sel_idx   = labels.index(sel_label)
            sel_trap  = df_events_reset.iloc[sel_idx].to_dict()

            if st.button(f"Show Chart — {label}", type="primary", key=f"btn_{opt_type}"):
                fig = build_75min_chart(df75, sel_trap, context_bars=chart_context)
                st.plotly_chart(fig, use_container_width=True)

                who    = sel_trap["Who Trapped"]
                status = sel_trap["Status"]
                trade  = "BULLISH (BUY)" if who == "BEARS" else "BEARISH (SELL)"
                color  = "#1B5E20" if who == "BEARS" else "#B71C1C"
                s_col  = "#E65100" if status == "OPEN" else "#6B7280"

                st.markdown(f"""
<div style="background:#F5F7FA;border:1px solid #DDE1E7;border-radius:8px;
            padding:16px 24px;margin-top:10px;display:flex;gap:40px;flex-wrap:wrap;">
  <div><div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Who Trapped</div>
       <div style="color:{color};font-size:20px;font-weight:bold;">{who}</div></div>
  <div><div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Your Signal</div>
       <div style="color:{color};font-size:20px;font-weight:bold;">{trade}</div></div>
  <div><div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Your Entry</div>
       <div style="color:#1565C0;font-size:20px;font-weight:bold;">{sel_trap['SL Level']:.2f}</div></div>
  <div><div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Your SL</div>
       <div style="color:#B71C1C;font-size:20px;font-weight:bold;">{sel_trap['Entry Level']:.2f}</div></div>
  <div><div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Status</div>
       <div style="color:{s_col};font-size:20px;font-weight:bold;">{status}</div></div>
</div>""", unsafe_allow_html=True)
        else:
            st.info(f"No traps detected for {label} in this window.")

        # ── Open traps summary ────────────────────────────────────────────────
        if open_traps:
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown(f"**Currently open traps for {label} (market has NOT returned to entry):**")
            rows = [{"Trapped On"   : e["trapped_on"].strftime("%d %b %H:%M"),
                     "Kind"         : e["kind"],
                     "Entry Level"  : f"{e['entry']:.2f}",
                     "SL Level"     : f"{e['sl']:.2f}",
                     "Signal"       : "BULLISH" if e["kind"]=="BEAR" else "BEARISH"}
                    for e in sorted(open_traps, key=lambda x: x["trapped_on"])]
            def sig_color(val):
                if val == "BULLISH": return "color:#1B5E20;font-weight:bold;"
                if val == "BEARISH": return "color:#B71C1C;font-weight:bold;"
                return ""
            st.dataframe(pd.DataFrame(rows).style.map(sig_color, subset=["Signal"]),
                         use_container_width=True, height=200, hide_index=True)

        # ── Raw 75-min bars ───────────────────────────────────────────────────
        with st.expander(f"Raw 75-min bars — {label}"):
            rc = df75.copy()
            rc["datetime"] = rc["datetime"].dt.strftime("%d %b %H:%M")
            rc["type"] = rc.apply(lambda r: "BULL" if r["close"] > r["open"] else "BEAR", axis=1)
            def ct(v):
                return "color:#1B5E20;font-weight:bold;" if v=="BULL" else "color:#B71C1C;font-weight:bold;"
            st.dataframe(rc.style.map(ct, subset=["type"]),
                         use_container_width=True, height=400, hide_index=True)

        st.markdown("---")


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
        itm_offset   = st.number_input("ITM Offset (pts)", value=500, step=50,
                                        help="CE = Pivot − offset, PE = Pivot + offset")
        weeks_back   = st.selectbox("Data window",
                                    [1, 2, 3, 4], index=1,
                                    format_func=lambda w: f"Prev {w} week(s) + current")
        chart_ctx    = st.slider("Chart context (75-min bars)", 5, 30, 10)
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
    st.markdown("# Option 75-min Trap Scanner")
    st.markdown("*Prev-day Nifty → Pivot → ITM Strikes → Instrument Keys → 75-min bars → Trap detection*")
    render_option_scanner(itm_offset, weeks_back, chart_ctx)
else:
    st.markdown("# Daily Nifty Trap Scanner  *(52-week validation)*")
    st.markdown("*Validates trap logic on Nifty 50 daily candles*")
    render_daily_scanner(weeks, filter_who, filter_status, chart_ctx)
