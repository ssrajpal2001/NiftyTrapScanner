"""
trap_ui.py  --  Nifty 50 Daily Trap Scanner  +  Chart Drilldown
Run: streamlit run trap_ui.py
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import requests
from datetime import datetime, timedelta

# ── page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Nifty Trap Scanner",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Obsidian Dark theme ────────────────────────────────────────────────────────
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
    padding: 16px 20px;
    text-align: center;
}
.metric-label { color: #6B7280; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }
.metric-value { color: #1A1A2E; font-size: 28px; font-weight: bold; margin-top: 4px; }
.metric-value.green  { color: #1B5E20; }
.metric-value.red    { color: #B71C1C; }
.metric-value.blue   { color: #1565C0; }
.metric-value.orange { color: #E65100; }
.section-header {
    background: #EFF3FB;
    border-left: 4px solid #1565C0;
    padding: 8px 14px;
    margin: 18px 0 10px 0;
    font-size: 14px;
    font-weight: 600;
    color: #1565C0;
    letter-spacing: 0.5px;
    border-radius: 0 4px 4px 0;
}
div[data-testid="stDataFrame"] { border: 1px solid #DDE1E7; border-radius: 6px; }
</style>
""", unsafe_allow_html=True)

# ── token (replace daily or move to .env) ─────────────────────────────────────
try:
    TOKEN = st.secrets.get("UPSTOX_TOKEN", "") or ""
except Exception:
    TOKEN = ""

if not TOKEN:
    TOKEN = (
        "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ"
        ".eyJzdWIiOiI0SkNIRDciLCJqdGkiOiI2YTJkNzdjZjU3MmUyNTUzMjYwMDNjOD"
        "QiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaWF0Ij"
        "oxNzgxMzY0Njg3LCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNlcnZpY2UiLCJleHAiOj"
        "E3ODEzODgwMDB9.m7y4u0urqwF2SYUY-i4bg5r6TJarFYa7uyG7_rlsoJI"
    )


# ── fetch ──────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def fetch_candles(from_date: str, to_date: str) -> pd.DataFrame:
    url = (f"https://api.upstox.com/v2/historical-candle/"
           f"NSE_INDEX%7CNifty%2050/day/{to_date}/{from_date}")
    r = requests.get(url, headers={"Authorization": f"Bearer {TOKEN}",
                                   "Accept": "application/json"}, timeout=20)
    body = r.json()
    if body.get("status") != "success":
        st.error(f"API error: {body}")
        st.stop()
    candles = list(reversed(body["data"]["candles"]))
    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "vol", "oi"])
    df["date"] = pd.to_datetime(df["ts"].str[:10])
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].round(2)
    df["candle"] = df.apply(lambda r: "BULL" if r["close"] > r["open"] else "BEAR", axis=1)
    return df[["date", "open", "high", "low", "close", "candle"]].reset_index(drop=True)


# ── scanner ────────────────────────────────────────────────────────────────────
def scan(df: pd.DataFrame):
    """
    Returns:
      events  : list of dicts — every TRAPPED and CLOSED event
      entries : list of dicts — all trap candidates (including still-ACTIVE ones)
    """
    entries = []
    events  = []

    def make(kind, ref_date, entry_level, sl_level):
        return {
            "kind": kind,
            "ref_date": ref_date,
            "entry": entry_level,
            "sl": sl_level,
            "status": "ACTIVE",
            "trapped_on": None,
            "closed_on": None,
            "event_idx": None,
        }

    for i in range(1, len(df)):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]
        date = curr["date"]

        for e in entries:
            if e["status"] == "CLOSED":
                continue
            if e["kind"] == "BEAR":
                if e["status"] == "ACTIVE" and curr["high"] > e["sl"]:
                    e["status"]     = "TRAPPED"
                    e["trapped_on"] = date
                    e["event_idx"]  = len(events)
                    events.append({
                        "Trap Date":      date,
                        "Who Got Trapped":"BEARS",
                        "Ref Candle":     e["ref_date"],
                        "Entry Level":    e["entry"],
                        "SL Level":       e["sl"],
                        "Status":         "OPEN",
                        "Close Date":     pd.NaT,
                    })
                if e["status"] == "TRAPPED" and curr["low"] <= e["entry"]:
                    e["status"]    = "CLOSED"
                    e["closed_on"] = date
                    events[e["event_idx"]]["Status"]     = "CLOSED"
                    events[e["event_idx"]]["Close Date"] = date
            else:
                if e["status"] == "ACTIVE" and curr["low"] < e["sl"]:
                    e["status"]     = "TRAPPED"
                    e["trapped_on"] = date
                    e["event_idx"]  = len(events)
                    events.append({
                        "Trap Date":      date,
                        "Who Got Trapped":"BULLS",
                        "Ref Candle":     e["ref_date"],
                        "Entry Level":    e["entry"],
                        "SL Level":       e["sl"],
                        "Status":         "OPEN",
                        "Close Date":     pd.NaT,
                    })
                if e["status"] == "TRAPPED" and curr["high"] >= e["entry"]:
                    e["status"]    = "CLOSED"
                    e["closed_on"] = date
                    events[e["event_idx"]]["Status"]     = "CLOSED"
                    events[e["event_idx"]]["Close Date"] = date

        if curr["low"] < prev["low"]:
            entries.append(make("BEAR", prev["date"], prev["low"], prev["high"]))
        if curr["high"] > prev["high"]:
            entries.append(make("BULL", prev["date"], prev["high"], prev["low"]))

    return pd.DataFrame(events) if events else pd.DataFrame(), entries


# ── chart builder ──────────────────────────────────────────────────────────────
def build_trap_chart(df_candles: pd.DataFrame, trap_row: dict, context_bars: int = 30) -> go.Figure:
    """
    Candlestick chart centred around a trap event.
    Shows:
      - Candles from (Trap Date - context_bars) to end of data (or Close Date + buffer)
      - Orange dashed line  : Trap Level (SL of trapped side = where trap fired)
      - Green  solid  line  : Entry Level (where trapped side entered = your trade entry)
      - Red    dashed line  : Stop Loss   (= Entry Level of trapped side, i.e. where YOU exit if wrong)
      - Vertical blue line  : Trap Date
      - Vertical grey line  : Close Date (if CLOSED)
      - Title shows status badge OPEN / CLOSED
    """
    trap_date  = pd.Timestamp(trap_row["Trap Date"])
    close_date = pd.Timestamp(trap_row["Close Date"]) if pd.notna(trap_row["Close Date"]) else None
    entry_lvl  = float(trap_row["Entry Level"])
    sl_lvl     = float(trap_row["SL Level"])
    who        = trap_row["Who Got Trapped"]   # "BEARS" or "BULLS"
    status     = trap_row["Status"]

    # Bears trapped → you BUY (bullish trade). Entry = SL of bear, SL = entry of bear.
    # Bulls trapped → you SELL (bearish trade). Entry = SL of bull, SL = entry of bull.
    trade_dir = "BULLISH TRADE" if who == "BEARS" else "BEARISH TRADE"
    your_entry = sl_lvl     # trap level = your trade entry
    your_sl    = entry_lvl  # original entry of trapped side = your SL

    # window: start 30 bars before trap date
    trap_idx = df_candles[df_candles["date"] <= trap_date].index
    if len(trap_idx) == 0:
        return go.Figure()
    start_idx = max(0, trap_idx[-1] - context_bars)
    end_idx   = len(df_candles) - 1
    if close_date is not None:
        close_idx = df_candles[df_candles["date"] <= close_date].index
        if len(close_idx):
            end_idx = min(len(df_candles) - 1, close_idx[-1] + 10)

    window = df_candles.iloc[start_idx : end_idx + 1].copy()

    # ── candlestick base ──
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=window["date"],
        open=window["open"], high=window["high"],
        low=window["low"],   close=window["close"],
        increasing_line_color="#238636", increasing_fillcolor="#238636",
        decreasing_line_color="#DA3633", decreasing_fillcolor="#DA3633",
        name="Nifty 50",
        showlegend=False,
    ))

    x0, x1 = window["date"].iloc[0], window["date"].iloc[-1]

    # ── Trap level — orange dashed (where trap fired / SL of trapped side) ──
    fig.add_shape(type="line", x0=x0, x1=x1, y0=sl_lvl, y1=sl_lvl,
                  line=dict(color="#f0a500", width=1.5, dash="dash"))
    fig.add_annotation(x=x1, y=sl_lvl, text=f"  Trap @ {sl_lvl:.2f}",
                       showarrow=False, xanchor="left",
                       font=dict(color="#f0a500", size=11))

    # ── Your entry — green solid (= trap level, same line but labelled separately) ──
    fig.add_shape(type="line", x0=x0, x1=x1, y0=your_entry, y1=your_entry,
                  line=dict(color="#58A6FF", width=1.5, dash="dot"))
    fig.add_annotation(x=x0, y=your_entry, text=f"Entry {your_entry:.2f}  ",
                       showarrow=False, xanchor="right",
                       font=dict(color="#58A6FF", size=11))

    # ── Your SL — red dashed (= original entry of trapped side) ──
    fig.add_shape(type="line", x0=x0, x1=x1, y0=your_sl, y1=your_sl,
                  line=dict(color="#DA3633", width=1.5, dash="dash"))
    fig.add_annotation(x=x0, y=your_sl, text=f"SL {your_sl:.2f}  ",
                       showarrow=False, xanchor="right",
                       font=dict(color="#DA3633", size=11))

    # ── Vertical line: Trap Date ──
    fig.add_vline(x=trap_date, line_width=1.5, line_dash="dot", line_color="#f0a500",
                  annotation_text="Trap", annotation_position="top",
                  annotation_font_color="#f0a500")

    # ── Vertical line: Close Date (if closed) ──
    if close_date is not None:
        fig.add_vline(x=close_date, line_width=1.5, line_dash="dot", line_color="#8B949E",
                      annotation_text="Closed", annotation_position="top",
                      annotation_font_color="#8B949E")

    status_badge = "🟠 OPEN" if status == "OPEN" else "⚪ CLOSED"
    fig.update_layout(
        title=dict(
            text=f"{who} TRAPPED → {trade_dir}  |  {status_badge}  |  "
                 f"Trap: {trap_date.strftime('%d %b %Y')}",
            font=dict(color="#1A1A2E", size=15),
        ),
        paper_bgcolor="#FFFFFF",
        plot_bgcolor="#FAFBFC",
        font=dict(color="#1A1A2E"),
        xaxis=dict(
            gridcolor="#E8ECF0",
            rangeslider=dict(visible=False),
            type="category",
        ),
        yaxis=dict(gridcolor="#E8ECF0"),
        margin=dict(l=60, r=120, t=60, b=40),
        height=520,
    )
    return fig


# ═══════════════════════════════════════════════════════════════════════════════
#  SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## Nifty 50 Trap Scanner")
    st.markdown("---")

    weeks = st.selectbox("Data Range", [4, 8, 13, 26, 52], index=4,
                         format_func=lambda w: f"{w} weeks")
    to_date   = datetime.today().strftime("%Y-%m-%d")
    from_date = (datetime.today() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
    st.caption(f"{from_date}  →  {to_date}")

    st.markdown("---")
    filter_who    = st.multiselect("Who Got Trapped", ["BEARS", "BULLS"],
                                   default=["BEARS", "BULLS"])
    filter_status = st.multiselect("Status", ["OPEN", "CLOSED"],
                                   default=["OPEN", "CLOSED"])

    context_bars = st.slider("Chart context (bars before trap)", 10, 60, 30)

    st.markdown("---")
    if st.button("Refresh Data", use_container_width=True):
        st.cache_data.clear()

    st.markdown("---")
    st.markdown("""
**BEARS trapped** = market went UP
SL Level broken upward → bears losing

**BULLS trapped** = market went DOWN
SL Level broken downward → bulls losing

**Entry Level** = where they entered
**SL Level** = level that was breached
**OPEN** = market not returned yet
**CLOSED** = 0-loss exit happened
""")

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("# Nifty 50 — Daily Trap Scanner")
st.markdown(f"*{weeks}-week candles from Upstox · refreshes every 5 min*")

with st.spinner("Fetching candles..."):
    df_candles = fetch_candles(from_date, to_date)

with st.spinner("Scanning for traps..."):
    df_events, all_entries = scan(df_candles)

# ── metrics row ───────────────────────────────────────────────────────────────
total  = len(df_events)
closed = int((df_events["Status"] == "CLOSED").sum()) if total else 0
open_  = total - closed
bears  = int((df_events["Who Got Trapped"] == "BEARS").sum()) if total else 0
bulls  = int((df_events["Who Got Trapped"] == "BULLS").sum()) if total else 0

c1, c2, c3, c4, c5 = st.columns(5)
for col, label, val, cls in [
    (c1, "Total Traps",   total,  "blue"),
    (c2, "Closed (Void)", closed, ""),
    (c3, "Still Open",    open_,  "orange"),
    (c4, "Bears Trapped", bears,  "green"),
    (c5, "Bulls Trapped", bulls,  "red"),
]:
    col.markdown(f"""
<div class="metric-card">
  <div class="metric-label">{label}</div>
  <div class="metric-value {cls}">{val}</div>
</div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── filter ────────────────────────────────────────────────────────────────────
df_show = df_events.copy()
if filter_who:
    df_show = df_show[df_show["Who Got Trapped"].isin(filter_who)]
if filter_status:
    df_show = df_show[df_show["Status"].isin(filter_status)]

# ── ALL TRAP EVENTS table ─────────────────────────────────────────────────────
st.markdown('<div class="section-header">ALL TRAP EVENTS</div>', unsafe_allow_html=True)
st.caption("Click a row below then press 'Show Chart' to see the candlestick drilldown.")

if df_show.empty:
    st.info("No events match the current filters.")
else:
    display = df_show.copy()
    display["Trap Date"]  = display["Trap Date"].dt.strftime("%Y-%m-%d")
    display["Ref Candle"] = display["Ref Candle"].dt.strftime("%Y-%m-%d")
    display["Close Date"] = display["Close Date"].apply(
        lambda x: x.strftime("%Y-%m-%d") if pd.notna(x) else "-")
    display["Entry Level"] = display["Entry Level"].map("{:.2f}".format)
    display["SL Level"]    = display["SL Level"].map("{:.2f}".format)

    def row_color(row):
        bg = ("background-color: rgba(35,134,54,0.12);"
              if row["Who Got Trapped"] == "BEARS"
              else "background-color: rgba(218,54,51,0.12);")
        return [bg] * len(row)

    def status_color(val):
        if val == "OPEN":   return "color: #f0a500; font-weight: bold;"
        if val == "CLOSED": return "color: #8B949E;"
        return ""

    styled = (display[["Trap Date", "Who Got Trapped", "Ref Candle",
                        "Entry Level", "SL Level", "Status", "Close Date"]]
              .style
              .apply(row_color, axis=1)
              .map(status_color, subset=["Status"]))

    st.dataframe(styled, use_container_width=True, height=400, hide_index=True)

    # ── Chart drilldown ────────────────────────────────────────────────────────
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">TRAP CHART DRILLDOWN</div>', unsafe_allow_html=True)
    st.caption("Select a trap from the list and click Show Chart to visualise Trap / Entry / SL levels on the candlestick chart.")

    # Build label list for selector
    df_show_reset = df_show.reset_index(drop=True)
    labels = [
        f"{row['Trap Date'].strftime('%d %b %Y')}  |  {row['Who Got Trapped']} TRAPPED  "
        f"|  Entry {row['Entry Level']:.2f}  |  SL {row['SL Level']:.2f}  |  {row['Status']}"
        for _, row in df_show_reset.iterrows()
    ]

    selected_label = st.selectbox("Select Trap to Chart", labels, index=0)
    selected_idx   = labels.index(selected_label)
    selected_trap  = df_show_reset.iloc[selected_idx].to_dict()

    if st.button("Show Chart", type="primary", use_container_width=False):
        fig = build_trap_chart(df_candles, selected_trap, context_bars=context_bars)
        st.plotly_chart(fig, use_container_width=True)

        # summary card below chart
        who    = selected_trap["Who Got Trapped"]
        status = selected_trap["Status"]
        trade  = "BULLISH (BUY)" if who == "BEARS" else "BEARISH (SELL)"
        color  = "#1B5E20" if who == "BEARS" else "#B71C1C"
        s_col  = "#E65100" if status == "OPEN" else "#6B7280"

        st.markdown(f"""
<div style="background:#F5F7FA;border:1px solid #DDE1E7;border-radius:8px;
            padding:16px 24px;margin-top:12px;display:flex;gap:40px;flex-wrap:wrap;">
  <div>
    <div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Who Got Trapped</div>
    <div style="color:{color};font-size:20px;font-weight:bold;">{who}</div>
  </div>
  <div>
    <div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Your Trade Signal</div>
    <div style="color:{color};font-size:20px;font-weight:bold;">{trade}</div>
  </div>
  <div>
    <div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Your Entry</div>
    <div style="color:#1565C0;font-size:20px;font-weight:bold;">{selected_trap['SL Level']:.2f}</div>
  </div>
  <div>
    <div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Your Stop Loss</div>
    <div style="color:#B71C1C;font-size:20px;font-weight:bold;">{selected_trap['Entry Level']:.2f}</div>
  </div>
  <div>
    <div style="color:#6B7280;font-size:11px;text-transform:uppercase;">Status</div>
    <div style="color:{s_col};font-size:20px;font-weight:bold;">{status}</div>
  </div>
</div>
""", unsafe_allow_html=True)

# ── OPEN TRAPS detail ─────────────────────────────────────────────────────────
open_entries = [e for e in all_entries if e["status"] == "TRAPPED"]

st.markdown("<br>", unsafe_allow_html=True)
st.markdown('<div class="section-header">CURRENTLY OPEN TRAPS — market has NOT returned to Entry Level yet</div>',
            unsafe_allow_html=True)
st.caption("These are LIVE levels. Market returning to Entry Level = 0-loss exit for institution.")

if open_entries:
    rows = []
    for e in sorted(open_entries, key=lambda x: x["trapped_on"], reverse=True):
        rows.append({
            "Trapped On":  e["trapped_on"].strftime("%Y-%m-%d"),
            "Kind":        e["kind"],
            "Ref Candle":  e["ref_date"].strftime("%Y-%m-%d"),
            "Entry Level": f"{e['entry']:.2f}",
            "SL Level":    f"{e['sl']:.2f}",
            "Signal":      "BULLISH" if e["kind"] == "BEAR" else "BEARISH",
        })
    ot = pd.DataFrame(rows)

    def open_row_color(row):
        bg = ("background-color: rgba(35,134,54,0.15);"
              if row["Kind"] == "BEAR"
              else "background-color: rgba(218,54,51,0.15);")
        return [bg] * len(row)

    def signal_color(val):
        if val == "BULLISH": return "color: #238636; font-weight: bold;"
        if val == "BEARISH": return "color: #DA3633; font-weight: bold;"
        return ""

    st.dataframe(
        ot.style.apply(open_row_color, axis=1).map(signal_color, subset=["Signal"]),
        use_container_width=True, height=400, hide_index=True,
    )
else:
    st.info("No open traps currently.")

# ── RAW CANDLES ───────────────────────────────────────────────────────────────
with st.expander("Raw Candle Data"):
    rc = df_candles.copy()
    rc["date"] = rc["date"].dt.strftime("%Y-%m-%d")

    def candle_color(val):
        if val == "BULL": return "color: #238636; font-weight: bold;"
        if val == "BEAR": return "color: #DA3633; font-weight: bold;"
        return ""

    st.dataframe(rc.style.map(candle_color, subset=["candle"]),
                 use_container_width=True, height=400, hide_index=True)

st.markdown("---")
st.caption(f"Data: Upstox V2 API · Nifty 50 NSE Index · {from_date} to {to_date} · {len(df_candles)} candles")
