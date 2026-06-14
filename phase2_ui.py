"""
phase2_ui.py - Phase 2: HTF -> LTF Drill-down Entry Engine
============================================================
Run: streamlit run phase2_ui.py

Flow:
  1. Fetch prev-day Nifty spot -> Pivot / R1 / R2 / S1 / S2
  2. Run HTF (75-min) scan on S1/S2 CE + R1/R2 PE
  3. For every triggered HTF trap, show the HTF zone
  4. Fetch 5-min data for the same contract
  5. Run LTF (5-min) scan INSIDE the HTF zone band
  6. Select LOWEST Zone Low LTF trap per HTF zone
  7. Enter when LTF bears get stopped out (LTF Ref Bar HIGH hit)
  8. SL = LTF Zone LOW - buffer  |  Target = HTF bears' SL level
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta, date

from config import (
    HTF_MINUTES, LTF_MINUTES,
    DEFAULT_SL_BUFFER, DEFAULT_QTY, DEFAULT_LOT_SIZE,
    CLR_GREEN, CLR_RED, CLR_BLUE, CLR_ORANGE, CLR_MUTED,
)
from data import (
    fetch_spot_prev_day, fetch_spot_for_date, get_instrument_key, fetch_1min,
    resample_tf, pivot_levels,
)
from scanner import scan_htf, scan_ltf, backtest, trade_summary

st.set_page_config(
    page_title="Trap Scanner - Phase 2",
    page_icon="T",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
html, body, [data-testid="stAppViewContainer"] {
    background-color: #FFFFFF; color: #1A1A2E;
    font-family: 'Segoe UI', sans-serif;
}
[data-testid="stSidebar"] {
    background-color: #F5F7FA; border-right: 1px solid #DDE1E7;
}
h1, h2, h3 { color: #1565C0; }
.metric-card {
    background: #F5F7FA; border: 1px solid #DDE1E7;
    border-radius: 8px; padding: 14px 18px; text-align: center;
}
.metric-label { color: #6B7280; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
.metric-value { color: #1A1A2E; font-size: 22px; font-weight: bold; margin-top: 4px; }
.metric-value.green  { color: #1B5E20; }
.metric-value.red    { color: #B71C1C; }
.metric-value.blue   { color: #1565C0; }
.metric-value.orange { color: #E65100; }
.sec-header {
    background: #EFF3FB; border-left: 4px solid #1565C0;
    padding: 8px 14px; margin: 18px 0 10px 0;
    font-size: 14px; font-weight: 600; color: #1565C0;
    border-radius: 0 4px 4px 0;
}
</style>
""", unsafe_allow_html=True)


# -- helpers -------------------------------------------------------------------
_FALLBACK_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ"
    ".eyJzdWIiOiI0SkNIRDciLCJqdGkiOiI2YTJlNWEyOTFiZTRjMDQyZTg1YTg3MTMiLC"
    "Jpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaWF0IjoxNzgxND"
    "IyNjMzLCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNlcnZpY2UiLCJleHAiOjE3ODE0NzQ0MD"
    "B9.QEijmZUpQ8RRUjpJf3dKyRaXZQ_UJfW_gnXVK0p6TS8"
)


def _get_token() -> str:
    if st.session_state.get("live_token"):
        return st.session_state["live_token"]
    try:
        t = st.secrets.get("UPSTOX_TOKEN", "") or ""
        if t:
            return t
    except Exception:
        pass
    return _FALLBACK_TOKEN


def round_n(v: float, step: int) -> int:
    return int(round(v / step) * step)


def next_tuesday(ref: date) -> date:
    days = (1 - ref.weekday()) % 7
    if days == 0:
        days = 7
    return ref + timedelta(days=days)


def trading_symbol(strike: int, opt_type: str, expiry: date) -> str:
    return f"NIFTY{expiry.strftime('%d%b%y').upper()}{strike}{opt_type}"


def card(col, label, value, cls=""):
    col.markdown(f"""<div class="metric-card">
<div class="metric-label">{label}</div>
<div class="metric-value {cls}">{value}</div>
</div>""", unsafe_allow_html=True)


def sec(text):
    st.markdown(f'<div class="sec-header">{text}</div>', unsafe_allow_html=True)


# -- cached data fetchers ------------------------------------------------------
@st.cache_data(ttl=300)
def _fetch_spot(token):
    return fetch_spot_prev_day({"Authorization": f"Bearer {token}", "Accept": "application/json"})


@st.cache_data(ttl=300)
def _fetch_key(strike, opt_type, expiry_str, token):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return get_instrument_key(strike, opt_type, expiry_str, h)


@st.cache_data(ttl=120)
def _fetch_1min(key, from_date, to_date, token):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return fetch_1min(key, from_date, to_date, h)


# ==============================================================================
#  BACKTEST TABLE RENDER
# ==============================================================================
def render_backtest_table(df_trades: pd.DataFrame, summary: dict, key: str = ""):
    if df_trades.empty:
        st.info("No LTF trades in this window.")
        return

    mc = st.columns(6)
    card(mc[0], "Trades",     str(summary["total"]),                               "blue")
    card(mc[1], "Wins",       str(summary["wins"]),                                "green")
    card(mc[2], "Losses",     str(summary["losses"]),                              "red")
    card(mc[3], "Win Rate",   f"{summary['win_rate']}%",
         "green" if summary["win_rate"] >= 50 else "red")
    card(mc[4], "Net P&L",    f"Rs.{summary['net_pnl']:,.0f}",
         "green" if summary["net_pnl"] >= 0 else "red")
    card(mc[5], "Best/Worst", f"Rs.{summary['best']:,.0f} / Rs.{summary['worst']:,.0f}", "")

    st.markdown("<br>", unsafe_allow_html=True)

    def pnl_clr(v):
        if isinstance(v, (int, float)):
            return f"color:{CLR_GREEN};font-weight:bold;" if v > 0 else f"color:{CLR_RED};font-weight:bold;"
        return ""

    def exit_clr(v):
        return {
            "TARGET":    f"color:{CLR_GREEN};font-weight:bold;",
            "SL":        f"color:{CLR_RED};font-weight:bold;",
            "SQUAREOFF": f"color:{CLR_MUTED};",
        }.get(v, "")

    htf_cols = ["HTF Ref Bar", "HTF Trap Bar", "HTF Zone High", "HTF Zone Low", "HTF Target"]
    ltf_cols = ["LTF Date", "LTF Entry Time", "LTF Exit Time",
                "LTF Entry", "LTF Zone Low", "LTF Target", "LTF SL", "LTF Exit Price",
                "Exit", "P&L (Rs)", "Cumulative P&L"]
    show_cols = [c for c in htf_cols + ltf_cols if c in df_trades.columns]

    st.dataframe(
        df_trades[show_cols].style
            .map(pnl_clr,  subset=["P&L (Rs)"])
            .map(exit_clr, subset=["Exit"]),
        width="stretch",
        height=min(42 * len(df_trades) + 50, 500),
        hide_index=True,
    )

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(range(1, len(df_trades) + 1)),
        y=df_trades["Cumulative P&L"],
        mode="lines+markers",
        line=dict(color=CLR_BLUE, width=2),
        marker=dict(
            color=[CLR_GREEN if p > 0 else CLR_RED for p in df_trades["P&L (Rs)"]],
            size=8,
        ),
        fill="tozeroy",
        fillcolor="rgba(21,101,192,0.08)",
    ))
    fig.update_layout(
        title="Cumulative P&L (Rs)",
        xaxis_title="Trade #", yaxis_title="Rs",
        paper_bgcolor="#FFFFFF", plot_bgcolor="#FAFBFC",
        height=280, margin=dict(l=40, r=20, t=40, b=40),
    )
    st.plotly_chart(fig, width="stretch")

    # -- Log download ----------------------------------------------------------
    lines = []
    lines.append("=" * 70)
    lines.append(f"TRAP SCANNER PHASE 2 -- BACKTEST LOG")
    lines.append(f"Generated: {datetime.now().strftime('%d %b %Y %H:%M:%S')}")
    lines.append("=" * 70)
    lines.append(f"Trades: {summary['total']}  |  Wins: {summary['wins']}  |  "
                 f"Losses: {summary['losses']}  |  Win Rate: {summary['win_rate']}%")
    lines.append(f"Net P&L: Rs.{summary['net_pnl']:,.2f}  |  "
                 f"Best: Rs.{summary['best']:,.2f}  |  Worst: Rs.{summary['worst']:,.2f}")
    lines.append(f"Avg Win: Rs.{summary.get('avg_win',0):,.2f}  |  "
                 f"Avg Loss: Rs.{summary.get('avg_loss',0):,.2f}")
    lines.append("-" * 70)
    for i, row in df_trades[show_cols].iterrows():
        lines.append(f"\nTrade {i+1}")
        for col in show_cols:
            lines.append(f"  {col:<22}: {row[col]}")
    lines.append("\n" + "=" * 70)
    log_text = "\n".join(lines)

    st.download_button(
        label="Download Full Log (TXT)",
        data=log_text,
        file_name=f"trap_log_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
        mime="text/plain",
        key=f"dl_log_{key}_{datetime.now().strftime('%H%M%S')}",
    )


# ==============================================================================
#  ALGOTEST-STYLE FULL REPORT
# ==============================================================================
def render_full_report(all_trades: pd.DataFrame, title: str = "Backtest Report"):
    if all_trades.empty:
        st.info("No trades found in this date range.")
        return

    df = all_trades.copy()
    df["_date"] = pd.to_datetime(df["LTF Date"], format="%d %b %y", errors="coerce")
    df["_year"]  = df["_date"].dt.year
    df["_month"] = df["_date"].dt.month
    df["_mname"] = df["_date"].dt.strftime("%b")

    pnl_col = "P&L (Rs)"
    total    = len(df)
    wins     = int((df[pnl_col] > 0).sum())
    losses   = int((df[pnl_col] <= 0).sum())
    net_pnl  = round(df[pnl_col].sum(), 2)
    win_rate = round(wins / total * 100, 1) if total else 0
    avg_ppt  = round(net_pnl / total, 2) if total else 0
    avg_win  = round(df[df[pnl_col] > 0][pnl_col].mean(), 2) if wins else 0
    avg_loss = round(df[df[pnl_col] <= 0][pnl_col].mean(), 2) if losses else 0
    max_win  = round(df[pnl_col].max(), 2)
    max_loss = round(df[pnl_col].min(), 2)

    # Drawdown
    cumulative = df[pnl_col].cumsum()
    peak = cumulative.cummax()
    drawdown = cumulative - peak
    max_dd = round(drawdown.min(), 2)

    sec(title)

    # -- Summary stats cards ---------------------------------------------------
    r1c = st.columns(4)
    card(r1c[0], "Overall Profit",      f"Rs.{net_pnl:,.0f}",  "green" if net_pnl >= 0 else "red")
    card(r1c[1], "No. of Trades",       str(total),             "blue")
    card(r1c[2], "Win %",               f"{win_rate}%",         "green" if win_rate >= 50 else "red")
    card(r1c[3], "Avg Profit per Trade",f"Rs.{avg_ppt:,.0f}",  "green" if avg_ppt >= 0 else "red")

    st.markdown("<br>", unsafe_allow_html=True)
    r2c = st.columns(4)
    card(r2c[0], "Avg Win",        f"Rs.{avg_win:,.0f}",   "green")
    card(r2c[1], "Avg Loss",       f"Rs.{avg_loss:,.0f}",  "red")
    card(r2c[2], "Max Single Win", f"Rs.{max_win:,.0f}",   "green")
    card(r2c[3], "Max Drawdown",   f"Rs.{max_dd:,.0f}",    "red")

    st.markdown("<br>", unsafe_allow_html=True)

    # -- Year-wise monthly returns table ---------------------------------------
    st.markdown("#### Year-wise Monthly Returns (Rs)")
    month_order = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    monthly = df.groupby(["_year","_mname","_month"])[pnl_col].sum().reset_index()
    monthly = monthly.sort_values("_month")
    pivot = monthly.pivot(index="_year", columns="_mname", values=pnl_col)
    pivot = pivot.reindex(columns=[m for m in month_order if m in pivot.columns])
    pivot["Total"] = pivot.sum(axis=1)
    pivot.index.name = "Year"

    def month_color(v):
        if isinstance(v, (int, float)):
            return f"color:{CLR_GREEN};font-weight:bold;" if v > 0 else f"color:{CLR_RED};font-weight:bold;"
        return ""

    st.dataframe(
        pivot.style.map(month_color).format("{:,.0f}", na_rep="-"),
        width="stretch", height=min(42 * len(pivot) + 60, 300),
    )

    # -- Equity curve + drawdown -----------------------------------------------
    df_sorted = df.sort_values("_date").reset_index(drop=True)
    cum_pnl   = df_sorted[pnl_col].cumsum()
    peak2     = cum_pnl.cummax()
    dd2       = cum_pnl - peak2

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df_sorted["_date"], y=cum_pnl,
        mode="lines", name="Cumulative P&L",
        line=dict(color=CLR_GREEN, width=2),
        fill="tozeroy", fillcolor="rgba(27,94,32,0.08)",
    ))
    fig.update_layout(
        title="Cumulative P&L (Rs)", xaxis_title="Date", yaxis_title="Rs",
        paper_bgcolor="#FFFFFF", plot_bgcolor="#FAFBFC",
        height=300, margin=dict(l=40, r=20, t=40, b=40),
        legend=dict(orientation="h"),
    )
    st.plotly_chart(fig, width="stretch")

    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(
        x=df_sorted["_date"], y=dd2,
        mode="lines", name="Drawdown",
        line=dict(color=CLR_RED, width=1.5),
        fill="tozeroy", fillcolor="rgba(183,28,28,0.08)",
    ))
    fig2.update_layout(
        title="Drawdown (Rs)", xaxis_title="Date", yaxis_title="Rs",
        paper_bgcolor="#FFFFFF", plot_bgcolor="#FAFBFC",
        height=220, margin=dict(l=40, r=20, t=40, b=40),
    )
    st.plotly_chart(fig2, width="stretch")

    # -- Date-wise grouped P&L -------------------------------------------------
    st.markdown("#### Date-wise P&L Summary")
    date_grp = (
        df.groupby(df["_date"].dt.strftime("%d %b %Y"))[pnl_col]
        .agg(Trades="count", PnL="sum")
        .reset_index()
        .rename(columns={"_date": "Date", "PnL": "P&L (Rs)"})
        .sort_values("Date")
    )
    date_grp["Cumulative (Rs)"] = date_grp["P&L (Rs)"].cumsum().round(2)

    def dg_clr(v):
        if isinstance(v, (int, float)):
            return f"color:{CLR_GREEN};font-weight:bold;" if v > 0 else f"color:{CLR_RED};font-weight:bold;"
        return ""

    st.dataframe(
        date_grp.style.map(dg_clr, subset=["P&L (Rs)", "Cumulative (Rs)"]),
        width="stretch", height=min(42 * len(date_grp) + 50, 400), hide_index=True,
    )

    # -- Full trade log --------------------------------------------------------
    st.markdown("#### Full Trade Log")
    show_cols = [c for c in [
        "LTF Date", "LTF Entry Time", "LTF Exit Time",
        "LTF Entry", "LTF Zone Low", "LTF Target", "LTF SL", "LTF Exit Price",
        "Exit", pnl_col, "Cumulative P&L",
        "HTF Ref Bar", "HTF Trap Bar", "HTF Zone High", "HTF Zone Low", "HTF Target",
    ] if c in df.columns]

    def pnl_c(v):
        if isinstance(v, (int, float)):
            return f"color:{CLR_GREEN};font-weight:bold;" if v > 0 else f"color:{CLR_RED};font-weight:bold;"
        return ""
    def exit_c(v):
        return {"TARGET": f"color:{CLR_GREEN};font-weight:bold;",
                "SL": f"color:{CLR_RED};font-weight:bold;",
                "SQUAREOFF": f"color:{CLR_MUTED};"}.get(v, "")

    st.dataframe(
        df[show_cols].style.map(pnl_c, subset=[pnl_col]).map(exit_c, subset=["Exit"]),
        width="stretch", height=min(42 * len(df) + 50, 500), hide_index=True,
    )

    # -- Log download ----------------------------------------------------------
    log_lines = [
        "=" * 70,
        f"TRAP SCANNER PHASE 2 -- DATE RANGE BACKTEST LOG",
        f"Generated: {datetime.now().strftime('%d %b %Y %H:%M:%S')}",
        "=" * 70,
        f"Total Trades : {total}   Wins: {wins}   Losses: {losses}   Win Rate: {win_rate}%",
        f"Net P&L      : Rs.{net_pnl:,.2f}",
        f"Avg Win      : Rs.{avg_win:,.2f}   Avg Loss: Rs.{avg_loss:,.2f}",
        f"Max Win      : Rs.{max_win:,.2f}   Max Loss: Rs.{max_loss:,.2f}",
        f"Max Drawdown : Rs.{max_dd:,.2f}",
        "-" * 70,
    ]
    for i, row in df[show_cols].iterrows():
        log_lines.append(f"\nTrade {i+1}")
        for col in show_cols:
            log_lines.append(f"  {col:<24}: {row[col]}")
    log_lines.append("\n" + "=" * 70)

    st.download_button(
        label="Download Full Log (TXT)",
        data="\n".join(log_lines),
        file_name=f"trap_backtest_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
        mime="text/plain",
        key="dl_full_report",
    )


# ==============================================================================
#  DATE-RANGE BACKTEST ENGINE
# ==============================================================================
def _scan_contract_for_dates(strike, opt_type, expiry, expiry_api,
                              trade_from, trade_to, htf_min, ltf_min,
                              sl_buffer, token) -> pd.DataFrame:
    """
    Run HTF->LTF scan with 3-week look-back for HTF context.
    Returns only trades whose entry date falls within trade_from..trade_to.
    """
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # Use zero-padded month in constructed key
    key, err = get_instrument_key(strike, opt_type, expiry_api, h)
    if err or not key:
        key = (f"NSE_FO|NIFTY{expiry.strftime('%y')}"
               f"{expiry.strftime('%m')}{expiry.strftime('%d')}"
               f"{strike}{opt_type}")

    # Fetch with 3-week look-back so HTF has enough history
    fetch_from = (datetime.strptime(trade_from, "%Y-%m-%d") - timedelta(weeks=3)).strftime("%Y-%m-%d")

    df1, err1 = fetch_1min(key, fetch_from, trade_to, h)
    if err1 or df1 is None or df1.empty:
        return pd.DataFrame()

    df_htf = resample_tf(df1, htf_min)
    if df_htf.empty:
        return pd.DataFrame()

    _, htf_entries = scan_htf(df_htf)
    htf_trapped = [e for e in htf_entries if e["status"] in ("TRAPPED", "CLOSED")]
    if not htf_trapped:
        return pd.DataFrame()

    df_ltf = resample_tf(df1, ltf_min)
    if df_ltf.empty:
        return pd.DataFrame()

    selected = []
    for htf_e in htf_trapped:
        zh, zl, tgt = htf_e["zone_high"], htf_e["zone_low"], htf_e["sl"]
        trap_ts = pd.Timestamp(htf_e["trapped_on"])
        df_ltf_after = df_ltf[df_ltf["datetime"] >= trap_ts].copy().reset_index(drop=True)
        if len(df_ltf_after) < 2:
            continue
        htf_ref_label  = pd.Timestamp(htf_e["ref_ts"]).strftime("%d %b %y %H:%M")
        htf_trap_label = trap_ts.strftime("%d %b %y %H:%M")
        _, ltf_entries = scan_ltf(df_ltf_after, zh, zl,
                                   htf_ref_bar=htf_ref_label,
                                   htf_trap_bar=htf_trap_label,
                                   htf_target=tgt)
        closed = [e for e in ltf_entries if e["status"] == "CLOSED"]
        if not closed:
            continue
        selected.append(min(closed, key=lambda e: e["zone_low"]))

    if not selected:
        return pd.DataFrame()

    df_trades = backtest(df_ltf, selected, buffer=sl_buffer,
                         qty=DEFAULT_QTY, lot_size=DEFAULT_LOT_SIZE)

    if df_trades.empty:
        return df_trades

    # Filter to only trades within the requested week range
    trade_from_d = datetime.strptime(trade_from, "%Y-%m-%d").date()
    trade_to_d   = datetime.strptime(trade_to,   "%Y-%m-%d").date()
    df_trades["_entry_date"] = pd.to_datetime(df_trades["LTF Date"], format="%d %b %y", errors="coerce").dt.date
    df_trades = df_trades[
        (df_trades["_entry_date"] >= trade_from_d) &
        (df_trades["_entry_date"] <= trade_to_d)
    ].drop(columns=["_entry_date"])

    return df_trades


def run_daterange_backtest(from_date, to_date, htf_min, ltf_min,
                            sl_buffer, round_step, token):
    """
    Iterate week by week over date range.
    For each week: compute prev-Mon pivot -> strikes -> scan -> collect trades.
    """
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # Build list of Mondays in range
    fd = datetime.strptime(from_date, "%Y-%m-%d").date()
    td = datetime.strptime(to_date,   "%Y-%m-%d").date()

    # Start from Monday of the first week
    cur = fd - timedelta(days=fd.weekday())  # Monday of from_date's week
    weeks = []
    while cur <= td:
        weeks.append(cur)
        cur += timedelta(weeks=1)

    all_trades = []
    progress   = st.progress(0, text="Running backtest...")
    total_w    = len(weeks)

    for i, monday in enumerate(weeks):
        progress.progress((i + 1) / total_w,
                          text=f"Week {i+1}/{total_w} — {monday.strftime('%d %b %Y')}")

        # Prev trading day before this Monday
        spot = fetch_spot_for_date(monday.strftime("%Y-%m-%d"), h)
        if spot is None:
            continue

        levels = pivot_levels(spot["high"], spot["low"], spot["close"])
        s1 = round_n(levels["s1"], round_step)
        s2 = round_n(levels["s2"], round_step)
        r1 = round_n(levels["r1"], round_step)
        r2 = round_n(levels["r2"], round_step)

        # Week's Friday for expiry
        friday  = monday + timedelta(days=4)
        # Find next Tuesday expiry on or after Monday
        days_to_tue = (1 - monday.weekday()) % 7
        if days_to_tue == 0:
            days_to_tue = 7
        expiry     = monday + timedelta(days=days_to_tue)
        expiry_api = expiry.strftime("%Y-%m-%d")

        # Trade window = just this week (HTF uses 3-week look-back internally)
        trade_from = max(monday, fd).strftime("%Y-%m-%d")
        trade_to   = min(friday, td).strftime("%Y-%m-%d")

        contracts = [
            (s1, "CE"), (s2, "CE"), (r1, "PE"), (r2, "PE"),
        ]
        for strike, opt_type in contracts:
            df_t = _scan_contract_for_dates(
                strike, opt_type, expiry, expiry_api,
                trade_from, trade_to, htf_min, ltf_min, sl_buffer, token,
            )
            if not df_t.empty:
                df_t["Contract"] = f"{strike}{opt_type}"
                df_t["Week"]     = monday.strftime("%d %b %Y")
                all_trades.append(df_t)

    progress.empty()

    if not all_trades:
        st.warning("No trades found in the selected date range.")
        return

    combined = pd.concat(all_trades, ignore_index=True)
    if "Cumulative P&L" in combined.columns:
        combined = combined.drop(columns=["Cumulative P&L"])
    combined = combined.sort_values("LTF Date").reset_index(drop=True)
    combined["Cumulative P&L"] = combined["P&L (Rs)"].cumsum().round(2)

    render_full_report(combined, f"Date Range Backtest: {from_date} to {to_date}")


# ==============================================================================
#  CORE: scan one contract through HTF -> LTF pipeline
# ==============================================================================
def scan_one_contract(label, strike, opt_type, cls,
                      expiry, expiry_api, from_date, to_date,
                      htf_minutes, ltf_minutes,
                      sl_buffer, round_step, token):
    sym = trading_symbol(strike, opt_type, expiry)
    sec(f"{label}  --  {sym}")

    key, err = _fetch_key(strike, opt_type, expiry_api, token)
    if err or not key:
        key = (f"NSE_FO|NIFTY{expiry.strftime('%y')}"
               f"{expiry.strftime('%m')}{expiry.strftime('%d')}"
               f"{strike}{opt_type}")
        st.caption(f"Chain lookup failed - using constructed key: `{key}`")
    else:
        st.caption(f"Instrument key: `{key}`")

    with st.spinner(f"Fetching 1-min data for {sym}..."):
        df1, err1 = _fetch_1min(key, from_date, to_date, token)

    if err1:
        st.error(f"1-min fetch failed: {err1}")
        return
    if df1 is None or df1.empty:
        st.warning(f"No data for `{sym}` - contract may not be active.")
        return

    # HTF resample + scan
    df_htf = resample_tf(df1, htf_minutes)
    if df_htf.empty:
        st.warning("HTF resample produced no bars.")
        return

    df_htf_events, htf_entries = scan_htf(df_htf)
    htf_trapped = [e for e in htf_entries if e["status"] in ("TRAPPED", "CLOSED")]

    st.caption(
        f"{len(df1):,} 1-min bars -> {len(df_htf)} bars at {htf_minutes}-min  |  "
        f"HTF traps found: {len(df_htf_events)}  (triggered: {len(htf_trapped)})"
    )

    if not df_htf_events.empty:
        disp = df_htf_events.copy()
        for col in ["Trap Bar", "Ref Bar", "Close Bar"]:
            disp[col] = disp[col].apply(
                lambda x: x.strftime("%d %b %y %H:%M") if pd.notna(x) else "-")
        for col in ["Bear Entry", "SL Level", "Zone High", "Zone Low", "Your Entry"]:
            if col in disp.columns:
                disp[col] = disp[col].map("{:.2f}".format)

        def sc(v):
            if v == "OPEN":   return "color:#E65100;font-weight:bold;"
            if v == "CLOSED": return "color:#6B7280;"
            return ""

        st.markdown(f"**HTF ({htf_minutes}-min) Trap Events**")
        st.dataframe(
            disp[["Trap Bar", "Ref Bar", "Bear Entry", "SL Level",
                  "Zone High", "Zone Low", "Your Entry", "Status", "Close Bar"]]
            .style.map(sc, subset=["Status"]),
            width="stretch", height=220, hide_index=True,
        )
    else:
        st.info(f"No HTF bearish traps found for {sym}.")
        return

    if not htf_trapped:
        st.caption("No HTF traps triggered yet - nothing to drill into LTF.")
        return

    df_ltf = resample_tf(df1, ltf_minutes)
    if df_ltf.empty:
        st.warning("LTF resample produced no bars.")
        return

    st.markdown(f"---\n#### LTF ({ltf_minutes}-min) Drill-Down")

    selected_ltf_entries = []   # ONE lowest-zone-low entry per HTF trap
    all_ltf_rows         = []   # all LTF traps found (informational)

    for htf_e in htf_trapped:
        zh      = htf_e["zone_high"]
        zl      = htf_e["zone_low"]
        tgt     = htf_e["sl"]
        trap_ts = pd.Timestamp(htf_e["trapped_on"])

        htf_ref_label  = pd.Timestamp(htf_e["ref_ts"]).strftime("%d %b %y %H:%M")
        htf_trap_label = trap_ts.strftime("%d %b %y %H:%M")

        df_ltf_after = df_ltf[df_ltf["datetime"] >= trap_ts].copy().reset_index(drop=True)
        if len(df_ltf_after) < 2:
            continue

        df_ltf_events, ltf_entries = scan_ltf(
            df_ltf_after, zh, zl,
            htf_ref_bar  = htf_ref_label,
            htf_trap_bar = htf_trap_label,
            htf_target   = tgt,
        )

        if df_ltf_events.empty:
            continue

        for _, row in df_ltf_events.iterrows():
            all_ltf_rows.append({
                "HTF Ref Bar"   : htf_ref_label,
                "HTF Trap Bar"  : htf_trap_label,
                "HTF Zone High" : round(zh,  2),
                "HTF Zone Low"  : round(zl,  2),
                "HTF Target"    : round(tgt, 2),
                "LTF Trap Bar"  : row["Trap Bar"].strftime("%d %b %y %H:%M") if pd.notna(row["Trap Bar"]) else "-",
                "LTF Ref Bar"   : row["Ref Bar"].strftime("%d %b %y %H:%M")  if pd.notna(row["Ref Bar"])  else "-",
                "LTF Zone High" : row["Zone High"],
                "LTF Zone Low"  : row["Zone Low"],
                "LTF Bear SL"   : row["SL Level"],
                "LTF Status"    : row["Status"],
            })

        # Select lowest Zone Low among CLOSED entries (price returned to bear entry)
        closed_ltf = [e for e in ltf_entries if e["status"] == "CLOSED"]
        if not closed_ltf:
            continue

        lowest = min(closed_ltf, key=lambda e: e["zone_low"])
        selected_ltf_entries.append(lowest)

    if all_ltf_rows:
        def sc3(v):
            if v == "OPEN":    return "color:#E65100;font-weight:bold;"
            if v == "CLOSED":  return "color:#6B7280;"
            if v == "TRAPPED": return "color:#1565C0;font-weight:bold;"
            return ""

        df_ltf_display = pd.DataFrame(all_ltf_rows)
        st.dataframe(
            df_ltf_display.style.map(sc3, subset=["LTF Status"]),
            width="stretch",
            height=min(42 * len(df_ltf_display) + 50, 400),
            hide_index=True,
        )
        if selected_ltf_entries:
            st.success(
                f"{len(selected_ltf_entries)} trade(s) selected - "
                f"one per HTF zone (lowest LTF Zone Low).  "
                f"Entry = LTF Bear SL (where 5-min bears get stopped out)."
            )
    else:
        st.info("No LTF bearish traps found inside any HTF zone.")

    if selected_ltf_entries:
        st.markdown("---")
        sec(
            f"BACKTEST -- {len(selected_ltf_entries)} Trade(s) | "
            f"Lowest LTF Zone per HTF trap | Entry at 5-min Bear SL | Intraday"
        )
        df_trades = backtest(
            df_ltf, selected_ltf_entries,
            buffer=sl_buffer,
            qty=DEFAULT_QTY, lot_size=DEFAULT_LOT_SIZE,
        )
        summary = trade_summary(df_trades)
        render_backtest_table(df_trades, summary, key=label.replace(" ", "_"))


# ==============================================================================
#  MAIN
# ==============================================================================
def main():
    with st.sidebar:
        st.markdown("## Phase 2 - HTF->LTF Engine")
        st.markdown("---")

        token_input = st.text_input("Upstox Daily Token", type="password",
                                    placeholder="Paste bearer token here")
        if st.button("Set Token", use_container_width=True):
            if token_input.strip():
                st.session_state["live_token"] = token_input.strip()
                st.cache_data.clear()
                st.success("Token saved.")
            else:
                st.error("Paste a valid token first.")

        token = _get_token()
        if token:
            st.caption(f"Token: `...{token[-12:]}`  set")
        else:
            st.warning("No token set.")

        st.markdown("---")
        st.markdown("**Settings**")
        round_step  = st.number_input("Round-off (pts)",     value=100, step=50,  min_value=50)
        htf_minutes = st.number_input("HTF Timeframe (min)", value=HTF_MINUTES, step=5, min_value=5)
        ltf_minutes = st.number_input("LTF Timeframe (min)", value=LTF_MINUTES, step=1, min_value=1)
        sl_buffer   = st.number_input("SL Buffer (pts)", value=DEFAULT_SL_BUFFER, step=0.5, min_value=0.0)

        st.markdown("---")
        if st.button("Refresh / Clear Cache", use_container_width=True, type="primary"):
            st.cache_data.clear()
            st.rerun()

    token = _get_token()
    st.markdown("# Phase 2 - HTF -> LTF Entry Engine")

    mode_tab, bt_tab = st.tabs(["Live Scan (Recent Data)", "Date Range Backtest"])

    # ==========================================================================
    #  TAB 1 — LIVE SCAN
    # ==========================================================================
    with mode_tab:
        weeks_back = st.selectbox("Data window", [1, 2, 3, 4], index=1,
                                  format_func=lambda w: f"Prev {w} week(s) + current")

        sec("Prev-Day Nifty Spot + Pivot Levels")
        with st.spinner("Fetching prev-day spot..."):
            try:
                spot = _fetch_spot(token)
            except Exception as ex:
                st.error(f"Spot fetch failed: {ex}")
                st.stop()

        H, L, C = spot["high"], spot["low"], spot["close"]
        levels   = pivot_levels(H, L, C)
        pivot = round_n(levels["pivot"], round_step)
        r1    = round_n(levels["r1"],    round_step)
        r2    = round_n(levels["r2"],    round_step)
        s1    = round_n(levels["s1"],    round_step)
        s2    = round_n(levels["s2"],    round_step)

        c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
        card(c1, "Prev Day",   spot["date"],      "blue")
        card(c2, "Prev High",  f"{H:,.2f}",       "green")
        card(c3, "Prev Low",   f"{L:,.2f}",       "red")
        card(c4, "Prev Close", f"{C:,.2f}",       "")
        card(c5, f"S1 CE x{round_step}", f"{s1:,}", "green")
        card(c6, f"S2 CE x{round_step}", f"{s2:,}", "green")
        card(c7, f"R1 PE x{round_step}", f"{r1:,}", "red")

        expiry     = next_tuesday(date.today())
        expiry_api = expiry.strftime("%Y-%m-%d")
        to_date    = datetime.today().strftime("%Y-%m-%d")
        from_date  = (datetime.today() - timedelta(weeks=weeks_back + 1)).strftime("%Y-%m-%d")

        sec("HTF -> LTF Scan  |  S1/S2 CE + R1/R2 PE")
        ctabs = st.tabs([f"S1 CE -- {s1:,}", f"S2 CE -- {s2:,}",
                         f"R1 PE -- {r1:,}", f"R2 PE -- {r2:,}"])
        contracts = [("S1 CE", s1, "CE", "green"), ("S2 CE", s2, "CE", "green"),
                     ("R1 PE", r1, "PE", "red"),   ("R2 PE", r2, "PE", "red")]
        for tab, (label, strike, opt_type, cls) in zip(ctabs, contracts):
            with tab:
                scan_one_contract(label, strike, opt_type, cls,
                                  expiry, expiry_api, from_date, to_date,
                                  htf_minutes, ltf_minutes,
                                  sl_buffer, round_step, token)

    # ==========================================================================
    #  TAB 2 — DATE RANGE BACKTEST
    # ==========================================================================
    with bt_tab:
        st.markdown("### Date Range Backtest")
        st.markdown(
            "For each week in the range: prev-Friday close -> pivot -> strikes -> "
            "HTF+LTF scan -> trades. Results grouped by date with AlgoTest-style report."
        )

        col1, col2 = st.columns(2)
        with col1:
            bt_from = st.date_input("From Date", value=date.today() - timedelta(weeks=8),
                                    key="bt_from")
        with col2:
            bt_to   = st.date_input("To Date",   value=date.today(), key="bt_to")

        if st.button("Run Date Range Backtest", type="primary", use_container_width=True):
            if bt_from >= bt_to:
                st.error("From Date must be before To Date.")
            else:
                run_daterange_backtest(
                    bt_from.strftime("%Y-%m-%d"),
                    bt_to.strftime("%Y-%m-%d"),
                    htf_minutes, ltf_minutes,
                    sl_buffer, round_step, token,
                )


if __name__ == "__main__":
    main()
else:
    main()
