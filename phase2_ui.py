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

import os
import sys
import asyncio
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta, date
from pathlib import Path

# Suppress Windows ProactorEventLoop pipe-close noise (removed in Python 3.16)
if sys.platform == "win32" and sys.version_info < (3, 14):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

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
    fetch_spot_prev_day, fetch_spot_for_date, get_instrument_key, fetch_1min,
    resample_tf, pivot_levels, get_option_expiries, fetch_spot_bars,
)
from scanner import (scan_htf, scan_ltf, scan_htf_spot, scan_ltf_bull,
                     select_best_ltf_entry, backtest, backtest_phase3,
                     run_scenario_comparison, PHASE3_SCENARIOS, trade_summary)

st.set_page_config(
    page_title="Trap Scanner - Phase 3",
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
_ENV_PATH = Path(__file__).parent / ".env"


def _decode_jwt_exp(token: str) -> int | None:
    import base64, json as _json
    try:
        payload_b64 = token.split(".")[1]
        padding = (4 - len(payload_b64) % 4) % 4
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))
        return int(payload["exp"])
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
        new_lines = [l for l in lines if not l.startswith("UPSTOX_TOKEN=")]
        new_lines.append(f"UPSTOX_TOKEN={token}")
        _ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        os.environ["UPSTOX_TOKEN"] = token
    except Exception:
        pass


def _get_token() -> str:
    if st.session_state.get("live_token"):
        return st.session_state["live_token"]
    env_t = os.environ.get("UPSTOX_TOKEN", "")
    if env_t and _token_valid(env_t):
        st.session_state["live_token"] = env_t
        return env_t
    try:
        t = st.secrets.get("UPSTOX_TOKEN", "") or ""
        if t and _token_valid(t):
            st.session_state["live_token"] = t
            return t
    except Exception:
        pass
    return ""


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


@st.cache_data(ttl=120)
def _scan_result(strike, opt_type, expiry_api, from_date, to_date,
                 htf_min, ltf_min, token):
    """
    Returns (selected_ltf_entries, df_ltf) for Phase 3 cross-pair analysis.
    Reuses cached _fetch_1min so no extra API calls.
    """
    expiry = datetime.strptime(expiry_api, "%Y-%m-%d").date()
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    key, err = get_instrument_key(strike, opt_type, expiry_api, h)
    if err or not key:
        key = (f"NSE_FO|NIFTY{expiry.strftime('%y')}"
               f"{expiry.strftime('%m')}{expiry.strftime('%d')}"
               f"{strike}{opt_type}")

    df1, err1 = _fetch_1min(key, from_date, to_date, token)
    if err1 or df1 is None or df1.empty:
        return [], pd.DataFrame()

    df_htf = resample_tf(df1, htf_min)
    if df_htf.empty:
        return [], pd.DataFrame()

    _, htf_entries = scan_htf(df_htf)
    htf_trapped = [e for e in htf_entries if e["status"] in ("TRAPPED", "CLOSED")]

    df_ltf = resample_tf(df1, ltf_min)
    if df_ltf.empty or not htf_trapped:
        return [], df_ltf

    _max_age = st.session_state.get("_max_zone_age")
    _scan_date = datetime.strptime(to_date, "%Y-%m-%d").date()

    selected = []
    for htf_e in htf_trapped:
        if _max_age is not None:
            ref_date = pd.Timestamp(htf_e["ref_ts"]).date()
            if (_scan_date - ref_date).days > _max_age:
                continue
        zh, zl, tgt  = htf_e["zone_high"], htf_e["zone_low"], htf_e["sl"]
        trap_ts       = pd.Timestamp(htf_e["trapped_on"])
        htf_ref_lbl   = pd.Timestamp(htf_e["ref_ts"]).strftime("%d %b %y %H:%M")
        htf_trap_lbl  = trap_ts.strftime("%d %b %y %H:%M")
        df_after      = df_ltf[df_ltf["datetime"] >= trap_ts].copy().reset_index(drop=True)
        if len(df_after) < 2:
            continue
        _, ltf_entries = scan_ltf(df_after, zh, zl,
                                   htf_ref_bar=htf_ref_lbl,
                                   htf_trap_bar=htf_trap_lbl,
                                   htf_target=tgt)
        best = select_best_ltf_entry(ltf_entries)
        if best:
            selected.append(best)

    return selected, df_ltf


# ==============================================================================
#  BACKTEST TABLE RENDER
# ==============================================================================
def render_backtest_table(df_trades: pd.DataFrame, summary: dict, key: str = "", key_suffix: str = ""):
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
            "TARGET":        f"color:{CLR_GREEN};font-weight:bold;",
            "SL":            f"color:{CLR_RED};font-weight:bold;",
            "HARD_FLOOR_SL": f"color:{CLR_RED};font-weight:bold;",
            "BREAKEVEN_SL":  f"color:{CLR_ORANGE};font-weight:bold;",
            "TIME_EXIT":     f"color:#9C27B0;font-weight:bold;",
            "PROGRESS_EXIT": f"color:#FF9800;font-weight:bold;",
            "SQUAREOFF":     f"color:{CLR_MUTED};",
        }.get(v, "")

    htf_cols = ["HTF Ref Bar", "HTF Trap Bar", "HTF Zone High", "HTF Zone Low", "HTF Target"]
    ltf_cols = ["LTF Date", "LTF Entry Time", "LTF Exit Time",
                "LTF Entry", "LTF Zone Low", "LTF Target", "LTF SL", "LTF Exit Price",
                "T1 Booked", "Exit", "P&L (Rs)", "Cumulative P&L"]
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
    st.plotly_chart(fig, width="stretch", key=f"bt_cum_{key_suffix or key}")

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
#  HTML REPORT BUILDER  (download → open in Chrome → Ctrl+P → Save as PDF)
# ==============================================================================
def _build_html_report(title, total, wins, losses, win_rate, net_pnl,
                        avg_win, avg_loss, max_win, max_loss, max_dd,
                        df_trades, date_grp):
    g = "#1B5E20"; r = "#B71C1C"; b = "#1565C0"
    pnl_colour = g if net_pnl >= 0 else r

    def stat_card(label, value, colour="#1A1A2E"):
        return (f'<div class="card">'
                f'<div class="label">{label}</div>'
                f'<div class="value" style="color:{colour}">{value}</div>'
                f'</div>')

    # Summary cards HTML
    cards_html = "".join([
        stat_card("Net P&L",         f"₹{net_pnl:,.0f}",  pnl_colour),
        stat_card("Total Trades",    str(total),            b),
        stat_card("Win Rate",        f"{win_rate}%",        g if win_rate >= 50 else r),
        stat_card("Max Drawdown",    f"₹{max_dd:,.0f}",    r),
        stat_card("Avg Win",         f"₹{avg_win:,.0f}",   g),
        stat_card("Avg Loss",        f"₹{avg_loss:,.0f}",  r),
        stat_card("Best Trade",      f"₹{max_win:,.0f}",   g),
        stat_card("Worst Trade",     f"₹{max_loss:,.0f}",  r),
    ])

    # Date-wise summary table
    def dg_row(row):
        c = g if row["P&L (Rs)"] > 0 else r
        return (f'<tr><td>{row.iloc[0]}</td>'
                f'<td>{int(row["Trades"])}</td>'
                f'<td style="color:{c};font-weight:bold">₹{row["P&L (Rs)"]:,.0f}</td>'
                f'<td style="color:{c}">₹{row["Cumulative (Rs)"]:,.0f}</td></tr>')

    dg_rows = "".join(dg_row(row) for _, row in date_grp.iterrows())

    # Full trade log table
    cols = list(df_trades.columns)
    trade_headers = "".join(f"<th>{c}</th>" for c in cols)

    def trade_row(i, row):
        pnl = row.get("P&L (Rs)", 0)
        exit_type = row.get("Exit", "")
        row_colour = ""
        if exit_type == "TARGET":   row_colour = "background:#E8F5E9"
        elif exit_type == "SL":     row_colour = "background:#FFEBEE"
        else:                        row_colour = "background:#FFF8E1"
        cells = "".join(f"<td>{v}</td>" for v in row.values)
        return f'<tr style="{row_colour}">{cells}</tr>'

    trade_rows = "".join(trade_row(i, row) for i, row in df_trades.iterrows())

    generated = datetime.now().strftime("%d %b %Y %H:%M")
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; font-size: 11px; color: #1A1A2E; margin: 20px; }}
  h1   {{ color: #1565C0; font-size: 18px; margin-bottom: 4px; }}
  h2   {{ color: #1565C0; font-size: 13px; margin: 16px 0 6px 0; border-bottom: 2px solid #1565C0; padding-bottom: 4px; }}
  .sub {{ color: #6B7280; font-size: 10px; margin-bottom: 16px; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }}
  .card  {{ border: 1px solid #DDE1E7; border-radius: 6px; padding: 10px 14px; min-width: 130px; background: #F5F7FA; }}
  .label {{ color: #6B7280; font-size: 9px; text-transform: uppercase; letter-spacing: 1px; }}
  .value {{ font-size: 16px; font-weight: bold; margin-top: 2px; }}
  table  {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
  th     {{ background: #1565C0; color: #fff; padding: 5px 7px; text-align: left; font-size: 10px; }}
  td     {{ padding: 4px 7px; border-bottom: 1px solid #EEE; font-size: 10px; }}
  tr:hover {{ background: #F0F4FF !important; }}
  @media print {{ body {{ margin: 10px; }} .no-print {{ display: none; }} }}
</style>
</head><body>
<h1>{title}</h1>
<div class="sub">Generated: {generated} &nbsp;|&nbsp; Trap Scanner Phase 2</div>

<h2>Performance Summary</h2>
<div class="cards">{cards_html}</div>

<h2>Date-wise P&amp;L</h2>
<table>
  <thead><tr><th>Date</th><th>Trades</th><th>P&amp;L (₹)</th><th>Cumulative (₹)</th></tr></thead>
  <tbody>{dg_rows}</tbody>
</table>

<h2>Full Trade Log</h2>
<table>
  <thead><tr>{trade_headers}</tr></thead>
  <tbody>{trade_rows}</tbody>
</table>

<div class="sub" style="margin-top:20px">
  🟢 Green = TARGET hit &nbsp;|&nbsp; 🔴 Red = SL hit &nbsp;|&nbsp; 🟡 Yellow = Square-off
</div>
</body></html>"""
    return html


# ==============================================================================
#  ALGOTEST-STYLE FULL REPORT
# ==============================================================================
def render_full_report(all_trades: pd.DataFrame, title: str = "Backtest Report", key_suffix: str = ""):
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
    st.plotly_chart(fig, width="stretch", key=f"fr_cum_{key_suffix}")

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
    st.plotly_chart(fig2, width="stretch", key=f"fr_dd_{key_suffix}")

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
        "Contract", "LTF Date", "LTF Entry Time", "LTF Exit Time",
        "LTF Entry", "LTF Zone Low", "LTF Target", "LTF SL", "LTF Exit Price",
        "T1 Booked", "Exit", pnl_col, "Cumulative P&L",
        "HTF Ref Bar", "HTF Trap Bar", "HTF Zone High", "HTF Zone Low", "HTF Target",
    ] if c in df.columns]

    def pnl_c(v):
        if isinstance(v, (int, float)):
            return f"color:{CLR_GREEN};font-weight:bold;" if v > 0 else f"color:{CLR_RED};font-weight:bold;"
        return ""
    def exit_c(v):
        return {"TARGET":         f"color:{CLR_GREEN};font-weight:bold;",
                "SL":             f"color:{CLR_RED};font-weight:bold;",
                "HARD_FLOOR_SL":  f"color:{CLR_RED};font-weight:bold;",
                "BREAKEVEN_SL":   f"color:{CLR_ORANGE};font-weight:bold;",
                "TIME_EXIT":      f"color:#9C27B0;font-weight:bold;",
                "PROGRESS_EXIT":  f"color:#FF9800;font-weight:bold;",
                "SQUAREOFF":      f"color:{CLR_MUTED};"}.get(v, "")

    st.dataframe(
        df[show_cols].style.map(pnl_c, subset=[pnl_col]).map(exit_c, subset=["Exit"]),
        width="stretch", height=min(42 * len(df) + 50, 500), hide_index=True,
    )

    # -- Downloads: TXT + HTML (print to PDF from browser) --------------------
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

    dl_col1, dl_col2 = st.columns(2)
    with dl_col1:
        st.download_button(
            label="⬇ Download Log (TXT)",
            data="\n".join(log_lines),
            file_name=f"trap_backtest_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
            mime="text/plain",
            key=f"dl_full_report{key_suffix}",
            use_container_width=True,
        )
    with dl_col2:
        html_bytes = _build_html_report(
            title, total, wins, losses, win_rate, net_pnl,
            avg_win, avg_loss, max_win, max_loss, max_dd,
            df[show_cols], date_grp,
        ).encode("utf-8")
        st.download_button(
            label="⬇ Download Report (HTML → Print to PDF)",
            data=html_bytes,
            file_name=f"trap_report_{datetime.now().strftime('%Y%m%d_%H%M')}.html",
            mime="text/html; charset=utf-8",
            key=f"dl_html_report{key_suffix}",
            use_container_width=True,
        )
    st.caption("To create PDF: open the HTML file in Chrome → Ctrl+P → Save as PDF")


# ==============================================================================
#  SCENARIO COMPARISON RENDERER
# ==============================================================================
def render_scenario_comparison(scenario_results: dict, key_suffix: str = ""):
    """
    Show a side-by-side summary of all scenarios + detailed report for each.
    scenario_results: {scenario_name: df_trades}
    """
    if not scenario_results:
        st.info("No scenario results to show.")
        return

    st.markdown("### Scenario Comparison")

    # Summary table: one row per scenario
    rows = []
    for name, df in scenario_results.items():
        if df.empty:
            rows.append({"Scenario": name, "Trades": 0, "Wins": 0, "Win %": 0,
                         "Net P&L (Rs)": 0, "Max DD (Rs)": 0, "Best (Rs)": 0, "Worst (Rs)": 0})
            continue
        pnl = df["P&L (Rs)"]
        wins = int((pnl > 0).sum())
        total = len(df)
        cum = pnl.cumsum()
        peak = cum.cummax()
        dd = round((cum - peak).min(), 2)
        rows.append({
            "Scenario"   : name,
            "Trades"     : total,
            "Wins"       : wins,
            "Win %"      : round(wins / total * 100, 1) if total else 0,
            "Net P&L (Rs)": round(pnl.sum(), 2),
            "Max DD (Rs)": dd,
            "Best (Rs)"  : round(pnl.max(), 2),
            "Worst (Rs)" : round(pnl.min(), 2),
        })

    df_cmp = pd.DataFrame(rows)

    def colour_pnl(v):
        try:
            f = float(v)
            if f > 0: return f"color:{CLR_GREEN};font-weight:bold"
            if f < 0: return f"color:{CLR_RED};font-weight:bold"
        except: pass
        return ""

    st.dataframe(
        df_cmp.style.map(colour_pnl, subset=["Net P&L (Rs)", "Max DD (Rs)", "Best (Rs)", "Worst (Rs)"]),
        use_container_width=True, hide_index=True,
    )

    # Best scenario highlight
    if rows:
        best = max(rows, key=lambda r: r["Net P&L (Rs)"])
        st.success(f"Best scenario: **{best['Scenario']}** — Net ₹{best['Net P&L (Rs)']:,.0f}  |  "
                   f"Win rate {best['Win %']}%  |  Drawdown ₹{best['Max DD (Rs)']:,.0f}")

    # Equity curve overlay
    fig = go.Figure()
    colours = [CLR_BLUE, CLR_GREEN, CLR_RED, CLR_ORANGE, "#9C27B0", "#00BCD4"]
    for idx, (name, df) in enumerate(scenario_results.items()):
        if df.empty:
            continue
        cum = df["P&L (Rs)"].cumsum().round(2)
        fig.add_trace(go.Scatter(
            x=list(range(1, len(df) + 1)),
            y=cum,
            mode="lines+markers",
            name=name,
            line=dict(color=colours[idx % len(colours)], width=2),
            marker=dict(size=5),
        ))
    fig.update_layout(
        title="Equity Curves — All Scenarios",
        xaxis_title="Trade #", yaxis_title="Cumulative P&L (Rs)",
        paper_bgcolor="#FFFFFF", plot_bgcolor="#FAFBFC",
        height=320, margin=dict(l=40, r=20, t=40, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig, width="stretch", key=f"sc_equity_{key_suffix}")

    # Detailed report per scenario
    st.markdown("### Detailed Reports per Scenario")
    for name, df in scenario_results.items():
        with st.expander(f"📋 {name}", expanded=False):
            if df.empty:
                st.info("No trades in this scenario.")
                continue
            render_full_report(df, title=name,
                               key_suffix=f"{key_suffix}_{name[:15].replace(' ', '_')}")


# ==============================================================================
#  DATE-RANGE BACKTEST ENGINE
# ==============================================================================
def _scan_contract_for_dates(strike, opt_type, expiry, expiry_api,
                              trade_from, trade_to, htf_min, ltf_min,
                              sl_buffer, token,
                              return_entries_only: bool = False):
    """
    Run HTF->LTF scan with 3-week look-back for HTF context.
    return_entries_only=False → returns trades DataFrame (old behaviour).
    return_entries_only=True  → returns (entries_list, df_ltf) for global backtest.
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

    _max_age   = st.session_state.get("_max_zone_age")
    _scan_date = datetime.strptime(trade_to, "%Y-%m-%d").date()

    selected = []
    for htf_e in htf_trapped:
        if _max_age is not None:
            ref_date = pd.Timestamp(htf_e["ref_ts"]).date()
            if (_scan_date - ref_date).days > _max_age:
                continue
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
        best = select_best_ltf_entry(ltf_entries)
        if not best:
            continue
        selected.append(best)

    if not selected:
        return ([], df_ltf) if return_entries_only else pd.DataFrame()

    # Filter entries to trade date window
    trade_from_d = datetime.strptime(trade_from, "%Y-%m-%d").date()
    trade_to_d   = datetime.strptime(trade_to,   "%Y-%m-%d").date()
    selected_in_window = [
        e for e in selected
        if e.get("closed_on") and
        trade_from_d <= pd.Timestamp(e["closed_on"]).date() <= trade_to_d
    ]

    if return_entries_only:
        return selected_in_window, df_ltf

    _met = st.session_state.get("_max_entry_time")
    _mrr = st.session_state.get("_min_rr", 0.0)
    _mzw = st.session_state.get("_min_zone_width", 0.0)
    df_trades = backtest(df_ltf, selected_in_window, buffer=sl_buffer,
                         qty=DEFAULT_QTY, lot_size=DEFAULT_LOT_SIZE,
                         max_entry_time=_met, min_rr=_mrr, min_zone_width=_mzw)

    return df_trades


def _build_spot_signals(from_date: str, to_date: str,
                        htf_min: int, ltf_min: int,
                        token: str, index: str = "Nifty") -> list:
    """
    Fetch Nifty spot bars, run HTF+LTF scan for BOTH bearish and bullish traps,
    return list of {ts: Timestamp, direction: "BULLISH"/"BEARISH"} dicts.

    BULLISH signal = bearish trap in spot (bears trapped → spot UP → CE)
    BEARISH signal = bullish trap in spot (bulls trapped → spot DOWN → PE)
    """
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    # 3-week look-back so HTF has context
    fetch_from = (datetime.strptime(from_date, "%Y-%m-%d") - timedelta(weeks=3)).strftime("%Y-%m-%d")

    df_htf_spot = fetch_spot_bars(fetch_from, to_date, h, index=index, minutes=htf_min)
    df_ltf_spot = fetch_spot_bars(fetch_from, to_date, h, index=index, minutes=ltf_min)

    if df_htf_spot.empty or df_ltf_spot.empty:
        return []

    _, htf_spot_entries = scan_htf_spot(df_htf_spot)
    htf_active = [e for e in htf_spot_entries if e["status"] in ("TRAPPED", "CLOSED")]

    signals = []
    for e in htf_active:
        trap_ts = pd.Timestamp(e["trapped_on"])
        df_ltf_after = df_ltf_spot[df_ltf_spot["datetime"] >= trap_ts].copy().reset_index(drop=True)
        if len(df_ltf_after) < 2:
            continue

        htf_ref  = pd.Timestamp(e["ref_ts"]).strftime("%d %b %y %H:%M") if e.get("ref_ts") else ""
        htf_trap = trap_ts.strftime("%d %b %y %H:%M")

        if e["kind"] == "BEAR":
            # Bearish HTF spot → scan for 5-min bearish LTF trap → BULLISH signal (CE)
            _, ltf_entries = scan_ltf(
                df_ltf_after, e["zone_high"], e["zone_low"],
                htf_ref_bar=htf_ref, htf_trap_bar=htf_trap,
                htf_target=e["sl"],
            )
            direction = "BULLISH"
        else:
            # Bullish HTF spot → scan for 5-min bullish LTF trap → BEARISH signal (PE)
            _, ltf_entries = scan_ltf_bull(
                df_ltf_after, e["zone_high"], e["zone_low"],
                htf_ref_bar=htf_ref, htf_trap_bar=htf_trap,
                htf_target=e["sl"],
            )
            direction = "BEARISH"

        for ltf_e in ltf_entries:
            if ltf_e.get("closed_on") and ltf_e["status"] in ("CLOSED", "TRAPPED"):
                signals.append({
                    "ts"       : pd.Timestamp(ltf_e["closed_on"]),
                    "direction": direction,
                })

    return signals


def _has_spot_signal(entry_ts: pd.Timestamp, direction: str,
                     signals: list, window_min: int = 20) -> bool:
    """
    True if a matching spot signal fired within `window_min` minutes before
    the option entry (or up to 5 min after — handles 5-min bar alignment).
    """
    for sig in signals:
        if sig["direction"] != direction:
            continue
        delta = (entry_ts - sig["ts"]).total_seconds() / 60
        if -5 <= delta <= window_min:
            return True
    return False


def run_daterange_backtest(from_date, to_date, htf_min, ltf_min,
                            sl_buffer, round_step, token,
                            expiry_type="Weekly", index="Nifty"):
    """
    Iterate week by week over date range.
    For each week: compute prev-Mon pivot -> strikes -> scan -> collect trades.
    Expiry is picked from the live available expiry list (not hardcoded Tuesday),
    so past dates still work as long as the chosen contract has data.
    """
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # Fetch available expiries once — use these for ALL weeks
    index_key = "NSE_INDEX|Nifty 50" if index == "Nifty" else "BSE_INDEX|SENSEX"
    available_expiries = get_option_expiries(index_key, h)
    if not available_expiries:
        st.error("Could not fetch expiry list from Upstox. Check token.")
        return

    # Build list of Mondays in range
    fd = datetime.strptime(from_date, "%Y-%m-%d").date()
    td = datetime.strptime(to_date,   "%Y-%m-%d").date()

    cur = fd - timedelta(days=fd.weekday())  # Monday of from_date's week
    weeks = []
    while cur <= td:
        weeks.append(cur)
        cur += timedelta(weeks=1)

    _selected_scen = st.session_state.get("_selected_scenarios") or ["Baseline (CP-SL only)"]
    _hfp          = st.session_state.get("_hard_floor_pct", 3.0)
    all_p3_by_sc: dict = {sc: [] for sc in _selected_scen}

    # Spot filter: build full-range signal list once before iterating weeks
    _spot_on  = st.session_state.get("_spot_filter_on", False)
    _spot_win = int(st.session_state.get("_spot_filter_win", 20))
    spot_signals: list = []
    if _spot_on:
        with st.spinner("Fetching Nifty spot bars for signal filter..."):
            spot_signals = _build_spot_signals(
                from_date, to_date, htf_min, ltf_min, token, index
            )
        st.caption(f"Spot filter active — {len(spot_signals)} signals found ({_spot_win}-min window)")

    progress      = st.progress(0, text="Running Phase 3 scenarios...")
    total_w       = len(weeks)
    sl_dir_filter = st.session_state.get("p3_sl_dir_filter", True)
    _met = st.session_state.get("_max_entry_time")
    _mrr = st.session_state.get("_min_rr", 0.0)
    _mzw = st.session_state.get("_min_zone_width", 0.0)

    for i, monday in enumerate(weeks):
        progress.progress((i + 1) / total_w,
                          text=f"Week {i+1}/{total_w} — {monday.strftime('%d %b %Y')}")

        spot = fetch_spot_for_date(monday.strftime("%Y-%m-%d"), h, index)
        if spot is None:
            continue

        levels = pivot_levels(spot["high"], spot["low"], spot["close"])
        s1 = round_n(levels["s1"], round_step)
        s2 = round_n(levels["s2"], round_step)
        r1 = round_n(levels["r1"], round_step)
        r2 = round_n(levels["r2"], round_step)

        expiry_api = _pick_expiry(available_expiries, monday.strftime("%Y-%m-%d"), expiry_type)
        if not expiry_api:
            continue
        expiry = datetime.strptime(expiry_api, "%Y-%m-%d").date()

        friday     = monday + timedelta(days=4)
        trade_from = max(monday, fd).strftime("%Y-%m-%d")
        trade_to   = min(friday, td).strftime("%Y-%m-%d")

        active_pairs_strikes = []
        if st.session_state.get("_pair_s1r1", True):
            active_pairs_strikes.append((s1, r1, "P1"))
        if st.session_state.get("_pair_s2r2", True):
            active_pairs_strikes.append((s2, r2, "P2"))

        for ce_strike, pe_strike, plbl in active_pairs_strikes:
            ce_entries, ce_ltf = _scan_contract_for_dates(
                ce_strike, "CE", expiry, expiry_api,
                trade_from, trade_to, htf_min, ltf_min, sl_buffer, token,
                return_entries_only=True,
            )
            pe_entries, pe_ltf = _scan_contract_for_dates(
                pe_strike, "PE", expiry, expiry_api,
                trade_from, trade_to, htf_min, ltf_min, sl_buffer, token,
                return_entries_only=True,
            )

            # Apply spot filter: drop option entries with no matching spot signal
            if _spot_on and spot_signals:
                ce_entries = [
                    e for e in ce_entries
                    if _has_spot_signal(
                        pd.Timestamp(e["closed_on"]), "BULLISH", spot_signals, _spot_win
                    )
                ] if ce_entries else []
                pe_entries = [
                    e for e in pe_entries
                    if _has_spot_signal(
                        pd.Timestamp(e["closed_on"]), "BEARISH", spot_signals, _spot_win
                    )
                ] if pe_entries else []

            for sc_name in _selected_scen:
                sc_params = PHASE3_SCENARIOS.get(sc_name, {})
                sc_week = []

                if ce_entries and ce_ltf is not None and not ce_ltf.empty:
                    df_ce = backtest_phase3(
                        ce_ltf, ce_entries, pe_entries,
                        qty=sc_params.get("qty", DEFAULT_QTY), lot_size=DEFAULT_LOT_SIZE,
                        sl_direction_filter=sl_dir_filter,
                        max_entry_time=_met, min_rr=_mrr, min_zone_width=_mzw,
                        t1_enabled=sc_params.get("t1_enabled", False),
                        hard_floor_enabled=sc_params.get("hard_floor_enabled", False),
                        breakeven_trail=sc_params.get("breakeven_trail", False),
                        hard_floor_pct=_hfp,
                    )
                    if not df_ce.empty:
                        df_ce.insert(0, "Side", "CE")
                        df_ce["Pair"] = f"S{plbl[-1]} CE↔R{plbl[-1]} PE"
                        df_ce["Week"] = monday.strftime("%d %b %Y")
                        sc_week.append(df_ce)

                if pe_entries and pe_ltf is not None and not pe_ltf.empty:
                    df_pe = backtest_phase3(
                        pe_ltf, pe_entries, ce_entries,
                        qty=sc_params.get("qty", DEFAULT_QTY), lot_size=DEFAULT_LOT_SIZE,
                        sl_direction_filter=sl_dir_filter,
                        max_entry_time=_met, min_rr=_mrr, min_zone_width=_mzw,
                        t1_enabled=sc_params.get("t1_enabled", False),
                        hard_floor_enabled=sc_params.get("hard_floor_enabled", False),
                        breakeven_trail=sc_params.get("breakeven_trail", False),
                        hard_floor_pct=_hfp,
                    )
                    if not df_pe.empty:
                        df_pe.insert(0, "Side", "PE")
                        df_pe["Pair"] = f"S{plbl[-1]} CE↔R{plbl[-1]} PE"
                        df_pe["Week"] = monday.strftime("%d %b %Y")
                        sc_week.append(df_pe)

                if sc_week:
                    all_p3_by_sc[sc_name].extend(sc_week)

    progress.empty()

    any_trades = any(len(v) > 0 for v in all_p3_by_sc.values())
    if not any_trades:
        st.warning("No Phase 3 trades found. Either no option data exists, or no traps fired.")
        st.caption(f"Available expiries from API: {', '.join(available_expiries[:6])}")
        return

    scenario_final = {}
    for sc_name, sc_list in all_p3_by_sc.items():
        if sc_list:
            comb = pd.concat(sc_list, ignore_index=True)
            if "Cumulative P&L" in comb.columns:
                comb = comb.drop(columns=["Cumulative P&L"])
            comb = comb.sort_values("LTF Date").reset_index(drop=True)
            comb["Cumulative P&L"] = comb["P&L (Rs)"].cumsum().round(2)
            scenario_final[sc_name] = comb

    render_scenario_comparison(scenario_final, key_suffix="_daterange")


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

    _max_age   = st.session_state.get("_max_zone_age")
    _scan_date = datetime.strptime(to_date, "%Y-%m-%d").date()

    for htf_e in htf_trapped:
        if _max_age is not None:
            ref_date = pd.Timestamp(htf_e["ref_ts"]).date()
            if (_scan_date - ref_date).days > _max_age:
                continue
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

        best = select_best_ltf_entry(ltf_entries)
        if not best:
            continue
        selected_ltf_entries.append(best)

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
        st.success(
            f"{len(selected_ltf_entries)} entry point(s) selected for Phase 3 pairing. "
            f"See Phase 3 Paired Report below."
        )


# ==============================================================================
#  MAIN
# ==============================================================================
def main():
    with st.sidebar:
        st.markdown("## Phase 3 - HTF->LTF Engine")
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
        st.markdown("**Active Pairs**")
        p1 = st.checkbox("Pair 1 — S1 CE ↔ R1 PE", value=True, key="p2_pair_s1r1")
        p2 = st.checkbox("Pair 2 — S2 CE ↔ R2 PE", value=True, key="p2_pair_s2r2")
        if not p1 and not p2:
            st.warning("At least one pair must be active.")
            p1 = True
        st.session_state["_pair_s1r1"] = p1
        st.session_state["_pair_s2r2"] = p2

        st.markdown("---")
        st.markdown("**Scenario Selection**")
        st.caption("Select which exit scenarios to compare. Run backtest to see results side-by-side.")
        all_scenario_names = list(PHASE3_SCENARIOS.keys())
        selected_scenarios = []
        for sname in all_scenario_names:
            desc = PHASE3_SCENARIOS[sname]["description"]
            checked = st.checkbox(sname, value=(sname in ["Scenario C: T1 + Hard Floor SL"]),
                                  key=f"sc_{sname[:20]}", help=desc)
            if checked:
                selected_scenarios.append(sname)
        if not selected_scenarios:
            selected_scenarios = ["Baseline (CP-SL only)"]
        st.session_state["_selected_scenarios"] = selected_scenarios

        hard_floor_pct = st.number_input("Hard Floor SL %", value=3.0, step=0.5,
                                          min_value=0.5, max_value=10.0,
                                          help="Used by scenarios with Hard Floor enabled. % below Zone Low.")
        st.session_state["_hard_floor_pct"] = hard_floor_pct

        st.markdown("---")
        st.markdown("**Trade Filters**")
        with st.expander("Advanced Filters", expanded=False):
            max_entry_time_on = st.checkbox("Max Entry Time", value=True, key="fil_met_on")
            max_entry_time_val = st.time_input(
                "Cut-off (no entries at/after)", value=datetime.strptime("13:30", "%H:%M").time(),
                key="fil_met_val", disabled=not max_entry_time_on
            )
            max_entry_time = max_entry_time_val.strftime("%H:%M") if max_entry_time_on else None
            st.session_state["_max_entry_time"] = max_entry_time

            min_rr_on  = st.checkbox("Min Reward:Risk", value=False, key="fil_rr_on")
            min_rr_val = st.number_input("Min R:R ratio", value=1.5, step=0.25,
                                          min_value=0.5, max_value=10.0,
                                          key="fil_rr_val", disabled=not min_rr_on)
            min_rr = min_rr_val if min_rr_on else 0.0
            st.session_state["_min_rr"] = min_rr

            min_zw_on  = st.checkbox("Min LTF Zone Width", value=False, key="fil_zw_on")
            min_zw_val = st.number_input("Min zone width (pts)", value=5.0, step=1.0,
                                          min_value=1.0, max_value=100.0,
                                          key="fil_zw_val", disabled=not min_zw_on)
            min_zone_width = min_zw_val if min_zw_on else 0.0
            st.session_state["_min_zone_width"] = min_zone_width

            st.markdown("**Shadow Scenarios (F / G / H)**")
            hold_on  = st.checkbox("Time Exit — max hold (min)", value=False, key="fil_hold_on")
            hold_val = st.number_input("Max hold minutes", value=60, step=5,
                                        min_value=15, max_value=240,
                                        key="fil_hold_val", disabled=not hold_on)
            # Override max_hold_minutes in Scenario F/H from sidebar
            if hold_on:
                PHASE3_SCENARIOS["Scenario F: T1 + Hard Floor + Time Exit"]["max_hold_minutes"] = hold_val
                PHASE3_SCENARIOS["Scenario H: T1 + Hard Floor + Time + Progress"]["max_hold_minutes"] = hold_val

            prog_on  = st.checkbox("Progress Exit — min % at T+30", value=False, key="fil_prog_on")
            prog_val = st.number_input("Min progress % (0–100)", value=30, step=5,
                                        min_value=5, max_value=80,
                                        key="fil_prog_val", disabled=not prog_on)
            if prog_on:
                PHASE3_SCENARIOS["Scenario G: T1 + Hard Floor + Progress Check"]["min_progress_pct"] = prog_val / 100
                PHASE3_SCENARIOS["Scenario H: T1 + Hard Floor + Time + Progress"]["min_progress_pct"] = prog_val / 100

            max_zone_age_on  = st.checkbox("Max HTF Zone Age", value=False, key="fil_age_on",
                                            help="Skip HTF zones whose Ref Bar is older than N calendar days")
            max_zone_age_val = st.number_input("Max age (calendar days)", value=10, step=1,
                                               min_value=1, max_value=90,
                                               key="fil_age_val", disabled=not max_zone_age_on)
            max_zone_age = max_zone_age_val if max_zone_age_on else None
            st.session_state["_max_zone_age"] = max_zone_age

            st.markdown("**Nifty Spot Filter (Option B)**")
            spot_filter_on = st.checkbox(
                "Use Nifty Spot Filter",
                value=False,
                key="fil_spot_on",
                help=(
                    "Fetches Nifty 75-min + 5-min SPOT bars. "
                    "CE entries are only taken when Nifty spot also shows a bearish LTF trap (BULLISH bias). "
                    "PE entries only when spot shows a bullish LTF trap (BEARISH bias). "
                    "Both must align within a 20-min window."
                ),
            )
            spot_window_val = st.number_input(
                "Signal window (min) — how far before option entry spot must have fired",
                value=20, step=5, min_value=5, max_value=60,
                key="fil_spot_win", disabled=not spot_filter_on,
            )
            st.session_state["_spot_filter_on"]  = spot_filter_on
            st.session_state["_spot_filter_win"]  = spot_window_val if spot_filter_on else 20

        st.markdown("---")
        if st.button("Refresh / Clear Cache", use_container_width=True, type="primary"):
            st.cache_data.clear()
            st.rerun()

    token = _get_token()
    st.markdown("# Phase 3 - HTF -> LTF Entry Engine")

    mode_tab, bt_tab, daily_tab = st.tabs(["Live Scan (Recent Data)", "Date Range Backtest", "Daily Rotating Backtest"])

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

        # -- Phase 3: Paired SL/Target analysis --------------------------------
        st.markdown("---")
        sec("PHASE 3 — Paired Trade Analysis  (S1 CE ↔ R1 PE  |  S2 CE ↔ R2 PE)")
        st.caption("SL = counterpart LTF entry fires  |  Target = same-side HTF ref bar HIGH")

        sl_dir_filter = st.checkbox(
            "SL Direction Filter (recommended ON)",
            value=True, key="p3_sl_dir_filter",
            help=(
                "Phase 3 SL fires when the counterpart (PE/CE) LTF entry fires inside "
                "its HTF trap zone.\n\n"
                "When ON: the SL only triggers if the counterpart's new entry price is "
                "HIGHER than its previous entry price — meaning the counterpart is gaining "
                "upward momentum and the market is genuinely reversing.\n\n"
                "When OFF: any counterpart LTF entry triggers the SL, even if the "
                "counterpart is still in a downtrend (declining entry prices = counterpart "
                "sellers still being cleared at lower and lower levels = market NOT reversing).\n\n"
                "Example: CE trade open. PE fires at ₹147, then ₹125 (lower) → PE still "
                "falling → Nifty still rising → keep CE open. PE fires at ₹160 (higher than "
                "₹147) → PE gaining momentum → Nifty reversing → EXIT CE."
            )
        )

        pairs_p3 = [
            ("Pair 1  S1 CE ↔ R1 PE", s1, "CE", r1, "PE"),
            ("Pair 2  S2 CE ↔ R2 PE", s2, "CE", r2, "PE"),
        ]

        for pair_lbl, ce_strike, ce_type, pe_strike, pe_type in pairs_p3:
            st.markdown(f"#### {pair_lbl}")

            ce_sel, ce_ltf = _scan_result(ce_strike, ce_type, expiry_api,
                                           from_date, to_date,
                                           htf_minutes, ltf_minutes, token)
            pe_sel, pe_ltf = _scan_result(pe_strike, pe_type, expiry_api,
                                           from_date, to_date,
                                           htf_minutes, ltf_minutes, token)

            ce_sym = trading_symbol(ce_strike, ce_type, expiry)
            pe_sym = trading_symbol(pe_strike, pe_type, expiry)

            col1, col2 = st.columns(2)
            col1.caption(f"CE ({ce_sym}): {len(ce_sel)} entries")
            col2.caption(f"PE ({pe_sym}): {len(pe_sel)} entries")

            if not ce_sel and not pe_sel:
                st.info("No entries fired on either side for this pair.")
                continue

            # Run all selected scenarios for each side and combine
            _met  = st.session_state.get("_max_entry_time")
            _mrr  = st.session_state.get("_min_rr", 0.0)
            _mzw  = st.session_state.get("_min_zone_width", 0.0)
            _hfp  = st.session_state.get("_hard_floor_pct", 3.0)
            _scen = st.session_state.get("_selected_scenarios")
            safe  = pair_lbl.replace(" ", "_").replace("↔", "x")

            # Per-scenario: merge CE + PE results
            scenario_combined: dict = {}
            for sc_name in (_scen or list(PHASE3_SCENARIOS.keys())):
                sc_params = PHASE3_SCENARIOS.get(sc_name, {})
                sc_trades = []
                if ce_sel and not ce_ltf.empty:
                    df_ce = backtest_phase3(
                        ce_ltf, ce_sel, pe_sel,
                        qty=sc_params.get("qty", DEFAULT_QTY), lot_size=DEFAULT_LOT_SIZE,
                        sl_direction_filter=sl_dir_filter,
                        max_entry_time=_met, min_rr=_mrr, min_zone_width=_mzw,
                        t1_enabled=sc_params.get("t1_enabled", False),
                        hard_floor_enabled=sc_params.get("hard_floor_enabled", False),
                        breakeven_trail=sc_params.get("breakeven_trail", False),
                        hard_floor_pct=_hfp,
                    )
                    if not df_ce.empty:
                        df_ce.insert(0, "Side", "CE")
                        sc_trades.append(df_ce)
                if pe_sel and not pe_ltf.empty:
                    df_pe = backtest_phase3(
                        pe_ltf, pe_sel, ce_sel,
                        qty=sc_params.get("qty", DEFAULT_QTY), lot_size=DEFAULT_LOT_SIZE,
                        sl_direction_filter=sl_dir_filter,
                        max_entry_time=_met, min_rr=_mrr, min_zone_width=_mzw,
                        t1_enabled=sc_params.get("t1_enabled", False),
                        hard_floor_enabled=sc_params.get("hard_floor_enabled", False),
                        breakeven_trail=sc_params.get("breakeven_trail", False),
                        hard_floor_pct=_hfp,
                    )
                    if not df_pe.empty:
                        df_pe.insert(0, "Side", "PE")
                        sc_trades.append(df_pe)
                if sc_trades:
                    comb = pd.concat(sc_trades, ignore_index=True)
                    comb = comb.sort_values("LTF Date").reset_index(drop=True)
                    comb["Cumulative P&L"] = comb["P&L (Rs)"].cumsum().round(2)
                    scenario_combined[sc_name] = comb

            if not scenario_combined:
                st.info("No Phase 3 trades to show for this pair.")
                continue

            render_scenario_comparison(scenario_combined, key_suffix=f"_p3_{safe}")

    # ==========================================================================
    #  TAB 2 — DATE RANGE BACKTEST
    # ==========================================================================
    with bt_tab:
        st.markdown("### Date Range Backtest")
        st.markdown(
            "Select any date range. Each week: **Monday's prev-day pivot → S1/S2/R1/R2 strikes → "
            "scan that week's option data**. Expiry is picked from currently available contracts."
        )

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            bt_from = st.date_input("From Date", value=date.today() - timedelta(weeks=2),
                                    key="bt_from")
        with col2:
            bt_to   = st.date_input("To Date",   value=date.today(), key="bt_to")
        with col3:
            bt_index = st.selectbox("Index", ["Nifty", "Sensex"], key="bt_index")
        with col4:
            bt_expiry_type = st.selectbox(
                "Expiry",
                ["Weekly", "Next Week", "Monthly"],
                key="bt_expiry_type",
                help=(
                    "Weekly = nearest available expiry\n"
                    "Next Week = second nearest\n"
                    "Monthly = last expiry of the current calendar month"
                )
            )

        if st.button("Run Date Range Backtest", type="primary", use_container_width=True):
            if bt_from >= bt_to:
                st.error("From Date must be before To Date.")
            else:
                run_daterange_backtest(
                    bt_from.strftime("%Y-%m-%d"),
                    bt_to.strftime("%Y-%m-%d"),
                    htf_minutes, ltf_minutes,
                    sl_buffer, round_step, token,
                    expiry_type=bt_expiry_type,
                    index=bt_index,
                )

    # ==========================================================================
    #  TAB 3 — DAILY ROTATING BACKTEST
    # ==========================================================================
    with daily_tab:
        st.markdown("### Daily Rotating Backtest")
        st.markdown(
            "Each trading day: prev-day spot → pivot → fresh S1/S2/R1/R2 strikes → "
            "scan only that day's data. Shows exactly what would have happened day by day."
        )

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            dr_from = st.date_input("From Date", value=date.today() - timedelta(days=14),
                                    key="dr_from")
        with col2:
            dr_to   = st.date_input("To Date",   value=date.today(), key="dr_to")
        with col3:
            dr_index = st.selectbox("Index", ["Nifty", "Sensex"], key="dr_index")
        with col4:
            dr_expiry_type = st.selectbox(
                "Expiry",
                ["Weekly", "Next Week", "Monthly"],
                key="dr_expiry_type",
                help=(
                    "Weekly = nearest expiry (current week)\n"
                    "Next Week = second nearest expiry\n"
                    "Monthly = last expiry of the current calendar month"
                )
            )

        dr_sl_filter = st.checkbox(
            "Phase 3 SL Direction Filter (recommended ON)", value=True, key="dr_sl_dir",
            help=(
                "Only trigger Phase 3 SL when counterpart entry price is HIGHER than the "
                "previous counterpart entry (counterpart gaining momentum upward = market "
                "reversing). Blocks false SL signals when counterpart is still in a downtrend "
                "(declining entry prices = market still moving in our favour)."
            )
        )

        if st.button("Run Daily Rotating Backtest", type="primary", use_container_width=True):
            if dr_from >= dr_to:
                st.error("From Date must be before To Date.")
            else:
                run_daily_rotating_backtest(
                    dr_from.strftime("%Y-%m-%d"),
                    dr_to.strftime("%Y-%m-%d"),
                    htf_minutes, ltf_minutes,
                    sl_buffer, round_step, dr_index,
                    dr_sl_filter, dr_expiry_type, token,
                    max_entry_time=st.session_state.get("_max_entry_time"),
                    min_rr=st.session_state.get("_min_rr", 0.0),
                    min_zone_width=st.session_state.get("_min_zone_width", 0.0),
                )


def _pick_expiry(expiries: list, trade_date_str: str, expiry_type: str) -> str | None:
    """
    From sorted expiry list, pick the right one for this trade_date.
    Weekly   = nearest expiry on/after trade_date
    Next Week= second nearest expiry
    Monthly  = expiry where the NEXT expiry is in a different month
               (i.e. current expiry is the last one of its month = monthly expiry)
    """
    available = [e for e in expiries if e >= trade_date_str]
    if not available:
        return None
    if expiry_type == "Weekly":
        return available[0]
    if expiry_type == "Next Week":
        return available[1] if len(available) > 1 else available[0]
    if expiry_type == "Monthly":
        # Find the expiry where next expiry is in a different month
        for idx in range(len(available) - 1):
            curr_month = available[idx][:7]   # "YYYY-MM"
            next_month = available[idx + 1][:7]
            if curr_month != next_month:
                return available[idx]
        return available[-1]   # fallback: last available
    return available[0]


def run_daily_rotating_backtest(from_date, to_date, htf_min, ltf_min,
                                sl_buffer, round_step, index,
                                sl_dir_filter, expiry_type, token,
                                max_entry_time=None, min_rr=0.0, min_zone_width=0.0):
    """
    True daily backtest: each trading day uses THAT day's prev-day pivot
    to compute fresh S1/S2/R1/R2 strikes. Scans only that day's data.
    expiry_type: "Weekly" | "Next Week" | "Monthly"
    """
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    fd = datetime.strptime(from_date, "%Y-%m-%d").date()
    td = datetime.strptime(to_date,   "%Y-%m-%d").date()

    # Build list of weekdays (Mon-Fri) in range
    trading_days = []
    cur = fd
    while cur <= td:
        if cur.weekday() < 5:
            trading_days.append(cur)
        cur += timedelta(days=1)

    if not trading_days:
        st.warning("No trading days in selected range.")
        return

    # Get expiries once (current expiry list from API)
    index_key = "NSE_INDEX|Nifty 50" if index == "Nifty" else "BSE_INDEX|SENSEX"
    expiries = get_option_expiries(index_key, h)

    _selected_scen = st.session_state.get("_selected_scenarios") or ["Baseline (CP-SL only)"]
    _hfp           = st.session_state.get("_hard_floor_pct", 3.0)

    all_day_summaries    = []
    all_p3_by_scenario: dict = {sc: [] for sc in _selected_scen}

    progress = st.progress(0, text="Running daily backtest...")
    total    = len(trading_days)

    for i, trade_date in enumerate(trading_days):
        progress.progress((i + 1) / total,
                          text=f"Day {i+1}/{total} — {trade_date.strftime('%d %b %Y')}")

        # -- Prev day OHLC → pivot → strikes ----------------------------------
        spot = fetch_spot_for_date(trade_date.strftime("%Y-%m-%d"), h, index)
        if spot is None:
            all_day_summaries.append({
                "Date": trade_date.strftime("%d %b %y"),
                "Prev Close": "—", "S1": "—", "S2": "—", "R1": "—", "R2": "—",
                "Trades": 0, "P&L (Rs)": 0, "Note": "No spot data",
            })
            continue

        levels = pivot_levels(spot["high"], spot["low"], spot["close"])
        s1 = round_n(levels["s1"], round_step)
        s2 = round_n(levels["s2"], round_step)
        r1 = round_n(levels["r1"], round_step)
        r2 = round_n(levels["r2"], round_step)

        # -- Expiry for this trade date ----------------------------------------
        expiry_api = _pick_expiry(expiries, trade_date.strftime("%Y-%m-%d"), expiry_type)
        if not expiry_api:
            all_day_summaries.append({
                "Date": trade_date.strftime("%d %b %y"),
                "Prev Close": spot["close"], "S1": s1, "S2": s2, "R1": r1, "R2": r2,
                "Trades": 0, "P&L (Rs)": 0, "Note": "No expiry found",
            })
            continue
        # -- HTF lookback: 3 weeks before trade_date --------------------------
        htf_from = (trade_date - timedelta(weeks=3)).strftime("%Y-%m-%d")
        trade_date_str = trade_date.strftime("%Y-%m-%d")

        day_pnl    = 0
        day_trades = 0
        day_note   = ""

        # Determine active pairs from UI toggle
        active_pairs_strikes = []
        if st.session_state.get("_pair_s1r1", True):
            active_pairs_strikes.append((s1, r1))
        if st.session_state.get("_pair_s2r2", True):
            active_pairs_strikes.append((s2, r2))
        if not active_pairs_strikes:
            active_pairs_strikes = [(s1, r1)]   # at least one pair always active

        active_contracts = []
        for cs, ps in active_pairs_strikes:
            active_contracts += [(cs, "CE"), (ps, "PE")]

        ce_sel_map: dict = {}
        pe_sel_map: dict = {}
        ce_ltf_map: dict = {}
        pe_ltf_map: dict = {}

        # Collect all entries for GLOBAL one-trade-at-a-time across all contracts
        day_entries: list = []
        day_df_map:  dict = {}

        for strike, opt_type in active_contracts:
            key, err = get_instrument_key(strike, opt_type, expiry_api, h, index)
            if not key:
                day_note += f"{strike}{opt_type}:no-key "
                continue

            df1, ferr = fetch_1min(key, htf_from, trade_date_str, h)
            if ferr or df1 is None or df1.empty:
                day_note += f"{strike}{opt_type}:no-data "
                continue

            df_htf = resample_tf(df1, htf_min)
            _, htf_entries = scan_htf(df_htf)
            htf_active = [e for e in htf_entries if e["status"] in ("TRAPPED", "CLOSED")]

            df_ltf     = resample_tf(df1, ltf_min)
            df_ltf_day = df_ltf[df_ltf["datetime"].dt.date == trade_date].copy()

            _max_age = st.session_state.get("_max_zone_age")

            selected = []
            for htf_e in htf_active:
                if _max_age is not None:
                    ref_date = pd.Timestamp(htf_e["ref_ts"]).date()
                    if (trade_date - ref_date).days > _max_age:
                        continue
                zh  = htf_e["zone_high"]
                zl  = htf_e["zone_low"]
                tgt = htf_e["sl"]
                htf_ref  = str(htf_e["ref_ts"])[:16] if htf_e.get("ref_ts") else ""
                htf_trap = str(htf_e.get("trapped_on", ""))[:16]
                _, ltf_entries = scan_ltf(
                    df_ltf_day, zh, zl,
                    htf_ref_bar=htf_ref, htf_trap_bar=htf_trap, htf_target=tgt
                )
                best = select_best_ltf_entry(ltf_entries)
                if not best:
                    continue
                selected.append(best)

            if not selected:
                continue

            contract_key = f"{strike}{opt_type}"
            for e in selected:
                e["_df_key"]   = contract_key
                e["_contract"] = contract_key
            day_entries.extend(selected)
            day_df_map[contract_key] = df_ltf_day

            # Store for Phase 3
            if opt_type == "CE":
                ce_sel_map[strike] = selected
                ce_ltf_map[strike] = df_ltf_day
            else:
                pe_sel_map[strike] = selected
                pe_ltf_map[strike] = df_ltf_day

        # -- Phase 3 pairing for this day -------------------------------------
        for ce_strike, pe_strike in active_pairs_strikes:
            ce_sel = ce_sel_map.get(ce_strike, [])
            pe_sel = pe_sel_map.get(pe_strike, [])
            ce_ltf = ce_ltf_map.get(ce_strike, pd.DataFrame())
            pe_ltf = pe_ltf_map.get(pe_strike, pd.DataFrame())

            pair_lbl = f"S{1 if ce_strike==s1 else 2} CE↔R{1 if pe_strike==r1 else 2} PE"
            baseline_counted = False

            for sc_name in _selected_scen:
                sc_params = PHASE3_SCENARIOS.get(sc_name, {})
                sc_trades_day = []

                if ce_sel and not ce_ltf.empty:
                    df_ce = backtest_phase3(
                        ce_ltf, ce_sel, pe_sel,
                        qty=sc_params.get("qty", DEFAULT_QTY), lot_size=DEFAULT_LOT_SIZE,
                        sl_direction_filter=sl_dir_filter,
                        max_entry_time=max_entry_time, min_rr=min_rr, min_zone_width=min_zone_width,
                        t1_enabled=sc_params.get("t1_enabled", False),
                        hard_floor_enabled=sc_params.get("hard_floor_enabled", False),
                        breakeven_trail=sc_params.get("breakeven_trail", False),
                        hard_floor_pct=_hfp,
                    )
                    if not df_ce.empty:
                        df_ce.insert(0, "Side", "CE")
                        df_ce["Date"] = trade_date.strftime("%d %b %y")
                        df_ce["Pair"] = pair_lbl
                        df_ce["Prev Close"] = spot["close"]
                        sc_trades_day.append(df_ce)

                if pe_sel and not pe_ltf.empty:
                    df_pe = backtest_phase3(
                        pe_ltf, pe_sel, ce_sel,
                        qty=sc_params.get("qty", DEFAULT_QTY), lot_size=DEFAULT_LOT_SIZE,
                        sl_direction_filter=sl_dir_filter,
                        max_entry_time=max_entry_time, min_rr=min_rr, min_zone_width=min_zone_width,
                        t1_enabled=sc_params.get("t1_enabled", False),
                        hard_floor_enabled=sc_params.get("hard_floor_enabled", False),
                        breakeven_trail=sc_params.get("breakeven_trail", False),
                        hard_floor_pct=_hfp,
                    )
                    if not df_pe.empty:
                        df_pe.insert(0, "Side", "PE")
                        df_pe["Date"] = trade_date.strftime("%d %b %y")
                        df_pe["Pair"] = pair_lbl
                        df_pe["Prev Close"] = spot["close"]
                        sc_trades_day.append(df_pe)

                if sc_trades_day:
                    all_p3_by_scenario[sc_name].extend(sc_trades_day)
                    # Day summary P&L uses first selected scenario (baseline)
                    if not baseline_counted:
                        for df_t in sc_trades_day:
                            day_pnl    += df_t["P&L (Rs)"].sum()
                            day_trades += len(df_t)
                        baseline_counted = True

        all_day_summaries.append({
            "Date"      : trade_date.strftime("%d %b %y"),
            "Prev Close": spot["close"],
            "Expiry"    : expiry_api,
            "S1 CE"     : s1, "S2 CE": s2, "R1 PE": r1, "R2 PE": r2,
            "Trades"    : day_trades,
            "P&L (Rs)"  : round(day_pnl, 2),
            "Note"      : day_note.strip(),
        })

    progress.empty()

    # -- Day summary table ----------------------------------------------------
    st.markdown("### Daily Strike Summary")
    df_summary = pd.DataFrame(all_day_summaries)
    df_summary["Cumulative P&L"] = df_summary["P&L (Rs)"].cumsum().round(2)

    def color_pnl(v):
        try:
            return "color:#238636;font-weight:bold" if float(v) > 0 else \
                   "color:#DA3633;font-weight:bold" if float(v) < 0 else ""
        except: return ""

    st.dataframe(
        df_summary.style.map(color_pnl, subset=["P&L (Rs)", "Cumulative P&L"]),
        use_container_width=True, hide_index=True,
    )

    def sort_by_trade_date(df: pd.DataFrame) -> pd.DataFrame:
        """Sort by actual trade date (stored as 'DD Mon YY' string) + entry time."""
        df = df.copy()
        df["_sort_dt"] = pd.to_datetime(df["Date"], format="%d %b %y", errors="coerce")
        df = df.sort_values(["_sort_dt", "LTF Entry Time"]).drop(columns=["_sort_dt"])
        return df.reset_index(drop=True)

    # -- Phase 3 scenario comparison report -----------------------------------
    st.markdown("---")
    st.markdown("### Phase 3 — Scenario Comparison")
    scenario_final = {}
    any_trades = False
    for sc_name, sc_list in all_p3_by_scenario.items():
        if sc_list:
            combined = pd.concat(sc_list, ignore_index=True)
            if "Cumulative P&L" in combined.columns:
                combined = combined.drop(columns=["Cumulative P&L"])
            combined = sort_by_trade_date(combined)
            combined["Cumulative P&L"] = combined["P&L (Rs)"].cumsum().round(2)
            scenario_final[sc_name] = combined
            any_trades = True
    if any_trades:
        render_scenario_comparison(scenario_final, key_suffix="_daily_p3")
    else:
        st.info("No Phase 3 trades found in this date range.")


if __name__ == "__main__":
    main()
else:
    main()
