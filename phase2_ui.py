"""
phase2_ui.py — Phase 2: HTF → LTF Drill-down Entry Engine
============================================================
Run: streamlit run phase2_ui.py

Flow:
  1. Fetch prev-day Nifty spot → Pivot / R1 / R2 / S1 / S2
  2. Run HTF (75-min) scan on S1/S2 CE + R1/R2 PE
  3. For every OPEN HTF trap, show the HTF zone
  4. Fetch 5-min data for the same contract
  5. Run LTF (5-min) scan INSIDE the HTF zone band
  6. Show LTF trap table + backtest P&L
     Entry  = LTF zone 1/3 trigger
     SL     = HTF Zone LOW - buffer
     Target = HTF SL Level (where HTF bears got stopped)
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import requests
from datetime import datetime, timedelta, date

# ── project modules ────────────────────────────────────────────────────────────
from config import (
    HTF_MINUTES, LTF_MINUTES,
    DEFAULT_SL_BUFFER, DEFAULT_QTY, DEFAULT_LOT_SIZE,
    CLR_GREEN, CLR_RED, CLR_BLUE, CLR_ORANGE, CLR_MUTED,
)
from data   import (
    fetch_spot_prev_day, get_instrument_key, fetch_1min,
    resample_tf, pivot_levels, url_encode,
)
from scanner import scan_htf, scan_ltf, backtest, trade_summary

# ── page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Trap Scanner — Phase 2",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── white theme ────────────────────────────────────────────────────────────────
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


# ── helpers ────────────────────────────────────────────────────────────────────
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
        if t: return t
    except Exception:
        pass
    return _FALLBACK_TOKEN

def round_n(v: float, step: int) -> int:
    return int(round(v / step) * step)

def next_tuesday(ref: date) -> date:
    days = (1 - ref.weekday()) % 7
    if days == 0: days = 7
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

HEADERS: dict = {}


# ── cached data fetchers (wrap module functions with st.cache_data) ────────────
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


# ══════════════════════════════════════════════════════════════════════════════
#  BACKTEST TABLE RENDER
# ══════════════════════════════════════════════════════════════════════════════
def render_backtest_table(df_trades: pd.DataFrame, summary: dict):
    if df_trades.empty:
        st.info("No closed LTF trades in this window.")
        return

    mc = st.columns(6)
    card(mc[0], "Trades",    str(summary["total"]),            "blue")
    card(mc[1], "Wins",      str(summary["wins"]),             "green")
    card(mc[2], "Losses",    str(summary["losses"]),           "red")
    card(mc[3], "Win Rate",  f"{summary['win_rate']}%",        "green" if summary["win_rate"] >= 50 else "red")
    card(mc[4], "Net P&L",   f"₹{summary['net_pnl']:,.0f}",   "green" if summary["net_pnl"] >= 0 else "red")
    card(mc[5], "Best/Worst",f"₹{summary['best']:,.0f} / ₹{summary['worst']:,.0f}", "")

    st.markdown("<br>", unsafe_allow_html=True)

    def pnl_clr(v):
        if isinstance(v, (int, float)):
            return f"color:{CLR_GREEN};font-weight:bold;" if v > 0 else f"color:{CLR_RED};font-weight:bold;"
        return ""
    def exit_clr(v):
        return {"TARGET": f"color:{CLR_GREEN};font-weight:bold;",
                "SL":     f"color:{CLR_RED};font-weight:bold;",
                "SQUAREOFF": f"color:{CLR_MUTED};"}.get(v, "")

    # Column order: HTF parent context first, then LTF trade details
    htf_cols = ["HTF Ref Bar", "HTF Trap Bar", "HTF Zone High", "HTF Zone Low", "HTF Target"]
    ltf_cols = ["LTF Date", "LTF Entry Time", "LTF Exit Time",
                "LTF Entry", "LTF Target", "LTF SL", "LTF Exit Price",
                "Exit", "P&L (₹)", "Cumulative P&L"]
    show_cols = [c for c in htf_cols + ltf_cols if c in df_trades.columns]

    st.dataframe(
        df_trades[show_cols].style
            .map(pnl_clr,  subset=["P&L (₹)"])
            .map(exit_clr, subset=["Exit"]),
        use_container_width=True,
        height=min(42 * len(df_trades) + 50, 500),
        hide_index=True,
    )

    # Equity curve
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(range(1, len(df_trades) + 1)),
        y=df_trades["Cumulative P&L"],
        mode="lines+markers",
        line=dict(color=CLR_BLUE, width=2),
        marker=dict(
            color=[CLR_GREEN if p > 0 else CLR_RED for p in df_trades["P&L (₹)"]],
            size=8,
        ),
        fill="tozeroy",
        fillcolor="rgba(21,101,192,0.08)",
    ))
    fig.update_layout(
        title="Cumulative P&L  (₹)",
        xaxis_title="Trade #", yaxis_title="₹",
        paper_bgcolor="#FFFFFF", plot_bgcolor="#FAFBFC",
        height=280, margin=dict(l=40, r=20, t=40, b=40),
    )
    st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
#  CORE: scan one contract through HTF → LTF pipeline
# ══════════════════════════════════════════════════════════════════════════════
def scan_one_contract(label, strike, opt_type, cls,
                      expiry, expiry_api, from_date, to_date,
                      htf_minutes, ltf_minutes,
                      sl_buffer, round_step, token):
    sym = trading_symbol(strike, opt_type, expiry)
    sec(f"{label}  —  {sym}")

    # ── instrument key ────────────────────────────────────────────────────────
    key, err = _fetch_key(strike, opt_type, expiry_api, token)
    if err or not key:
        from data import url_encode as _ue
        key = f"NSE_FO|NIFTY{expiry.strftime('%y')}{expiry.month}{expiry.strftime('%d')}{strike}{opt_type}"
        st.caption(f"⚠️ Chain lookup failed — using constructed key: `{key}`")
    else:
        st.caption(f"✓ Instrument key: `{key}`")

    # ── 1-min fetch ───────────────────────────────────────────────────────────
    with st.spinner(f"Fetching 1-min data for {sym}..."):
        df1, err1 = _fetch_1min(key, from_date, to_date, token)

    if err1:
        st.error(f"1-min fetch failed: {err1}")
        return
    if df1 is None or df1.empty:
        st.warning(f"No data for `{sym}` — contract may not be active.")
        return

    # ── HTF resample + scan ───────────────────────────────────────────────────
    df_htf = resample_tf(df1, htf_minutes)
    if df_htf.empty:
        st.warning("HTF resample produced no bars.")
        return

    df_htf_events, htf_entries = scan_htf(df_htf)
    htf_open = [e for e in htf_entries if e["status"] == "TRAPPED"]   # OPEN HTF traps

    st.caption(f"{len(df1):,} 1-min bars  →  {len(df_htf)} bars at {htf_minutes}-min  |  "
               f"HTF traps: {len(df_htf_events)}  (OPEN: {len(htf_open)})")

    # ── HTF trap table ────────────────────────────────────────────────────────
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
            disp[["Trap Bar","Ref Bar","Bear Entry","SL Level",
                  "Zone High","Zone Low","Your Entry","Status","Close Bar"]]
            .style.map(sc, subset=["Status"]),
            use_container_width=True, height=220, hide_index=True,
        )
    else:
        st.info(f"No HTF bearish traps found for {sym}.")

    # ── LTF drill-down for each OPEN HTF trap ─────────────────────────────────
    if not htf_open:
        st.caption("No OPEN HTF traps — nothing to drill into LTF.")
    else:
        st.markdown(f"---\n#### LTF ({ltf_minutes}-min) Drill-Down  |  {len(htf_open)} OPEN HTF trap(s)")

    all_ltf_entries = []    # collect across all HTF traps for combined backtest
    htf_target_map  = {}    # closed_on_ts → htf target price

    for htf_e in htf_open:
        zh  = htf_e["zone_high"]
        zl  = htf_e["zone_low"]
        tgt = htf_e["sl"]   # HTF target = where HTF bears were stopped

        st.markdown(
            f"**HTF Zone:** {zh:.2f} → {zl:.2f}  |  "
            f"HTF Target (bears SL): **{tgt:.2f}**  |  "
            f"Trapped on: {htf_e['trapped_on'].strftime('%d %b %y %H:%M') if htf_e['trapped_on'] else '-'}"
        )

        # ── LTF resample + scan inside HTF zone ───────────────────────────────
        df_ltf = resample_tf(df1, ltf_minutes)
        if df_ltf.empty:
            st.caption("  No LTF bars.")
            continue

        htf_ref_label  = htf_e["ref_ts"].strftime("%d %b %y %H:%M")  if htf_e.get("ref_ts")      else "—"
        htf_trap_label = htf_e["trapped_on"].strftime("%d %b %y %H:%M") if htf_e.get("trapped_on") else "—"

        df_ltf_events, ltf_entries = scan_ltf(
            df_ltf, zh, zl,
            htf_ref_bar  = htf_ref_label,
            htf_trap_bar = htf_trap_label,
            htf_target   = tgt,
        )
        ltf_closed = [e for e in ltf_entries if e["status"] == "CLOSED"]

        if df_ltf_events.empty:
            st.caption(f"  No LTF bearish traps inside zone {zh:.2f}–{zl:.2f}.")
            continue

        # ── LTF trap table ─────────────────────────────────────────────────────
        disp_ltf = df_ltf_events.copy()
        for col in ["Trap Bar", "Ref Bar", "Close Bar"]:
            disp_ltf[col] = disp_ltf[col].apply(
                lambda x: x.strftime("%d %b %y %H:%M") if pd.notna(x) else "-")
        for col in ["Bear Entry", "SL Level", "Zone High", "Zone Low", "Your Entry"]:
            if col in disp_ltf.columns:
                disp_ltf[col] = disp_ltf[col].map("{:.2f}".format)

        def sc2(v):
            if v == "OPEN":   return "color:#E65100;font-weight:bold;"
            if v == "CLOSED": return "color:#6B7280;"
            return ""

        st.dataframe(
            disp_ltf[["Trap Bar","Ref Bar","Bear Entry","SL Level",
                       "Zone High","Zone Low","Your Entry","Status","Close Bar"]]
            .style.map(sc2, subset=["Status"]),
            use_container_width=True, height=200, hide_index=True,
        )

        # Map each LTF closed entry → HTF target (for backtest)
        for e in ltf_closed:
            if e.get("closed_on"):
                htf_target_map[e["closed_on"]] = tgt

        all_ltf_entries.extend(ltf_entries)

    # ── Combined LTF backtest ─────────────────────────────────────────────────
    if all_ltf_entries:
        st.markdown("---")
        sec(f"BACKTEST — LTF Trades  |  Qty {DEFAULT_QTY} × Lot {DEFAULT_LOT_SIZE} = {DEFAULT_QTY * DEFAULT_LOT_SIZE} units  |  Intraday")

        df_ltf_full = resample_tf(df1, ltf_minutes)
        df_trades   = backtest(
            df_ltf_full, all_ltf_entries,
            htf_target_map=htf_target_map,
            buffer=sl_buffer,
            qty=DEFAULT_QTY, lot_size=DEFAULT_LOT_SIZE,
        )
        summary = trade_summary(df_trades)
        render_backtest_table(df_trades, summary)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN RENDER
# ══════════════════════════════════════════════════════════════════════════════
def main():
    # ── sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("## 🎯 Phase 2 — HTF→LTF Engine")
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
            st.caption(f"Token: `...{token[-12:]}`  ✓")
        else:
            st.warning("No token set.")

        st.markdown("---")
        st.markdown("**Settings**")
        round_step  = st.number_input("Round-off (pts)",     value=100, step=50,  min_value=50)
        htf_minutes = st.number_input("HTF Timeframe (min)", value=HTF_MINUTES, step=5, min_value=5)
        ltf_minutes = st.number_input("LTF Timeframe (min)", value=LTF_MINUTES, step=1, min_value=1)
        weeks_back  = st.selectbox("Data window",            [1, 2, 3, 4], index=1,
                                   format_func=lambda w: f"Prev {w} week(s) + current")
        sl_buffer   = st.number_input("SL Buffer (pts)",     value=DEFAULT_SL_BUFFER, step=0.5, min_value=0.0)

        st.markdown("---")
        if st.button("🔄 Refresh / Fetch Data", use_container_width=True, type="primary"):
            st.cache_data.clear()
            st.rerun()

        st.markdown("---")
        st.markdown("""
**Phase 2 Logic:**
1. HTF 75-min bearish trap found
2. Zone = Ref Bar LOW → Next Bar LOW
3. Market returns → OPEN trap
4. Drill to 5-min inside zone
5. Find 5-min bear trap
6. Enter at LTF 1/3 trigger
7. SL = HTF Zone LOW − buffer
8. Target = HTF bears' SL level
""")

    # ── token headers ─────────────────────────────────────────────────────────
    token = _get_token()

    # ── main page ─────────────────────────────────────────────────────────────
    st.markdown("# 🎯 Phase 2 — HTF → LTF Entry Engine")
    st.markdown(
        f"*Prev-day Nifty → Pivot/R1/R2/S1/S2 → {htf_minutes}-min HTF traps → "
        f"{ltf_minutes}-min LTF drill-down inside zone → Entry + Backtest*"
    )

    # ── Step 1: Prev-day spot + pivot levels ──────────────────────────────────
    sec("STEP 1 : Prev-Day Nifty Spot + Pivot Levels")
    with st.spinner("Fetching prev-day spot..."):
        try:
            spot = _fetch_spot(token)
        except Exception as ex:
            st.error(f"Spot fetch failed: {ex}")
            st.stop()

    H, L, C = spot["high"], spot["low"], spot["close"]
    levels   = pivot_levels(H, L, C)

    pivot_r = levels["pivot"];  r1_r = levels["r1"];  r2_r = levels["r2"]
    s1_r    = levels["s1"];     s2_r = levels["s2"]

    pivot = round_n(pivot_r, round_step)
    r1    = round_n(r1_r,    round_step)
    r2    = round_n(r2_r,    round_step)
    s1    = round_n(s1_r,    round_step)
    s2    = round_n(s2_r,    round_step)

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    card(c1, "Prev Day",    spot["date"],      "blue")
    card(c2, "Prev High",   f"{H:,.2f}",       "green")
    card(c3, "Prev Low",    f"{L:,.2f}",       "red")
    card(c4, "Prev Close",  f"{C:,.2f}",       "")
    card(c5, f"S1 CE ×{round_step}", f"{s1:,}", "green")
    card(c6, f"S2 CE ×{round_step}", f"{s2:,}", "green")
    card(c7, f"R1 PE ×{round_step}", f"{r1:,}", "red")

    expiry     = next_tuesday(date.today())
    expiry_api = expiry.strftime("%Y-%m-%d")
    to_date    = datetime.today().strftime("%Y-%m-%d")
    from_date  = (datetime.today() - timedelta(weeks=weeks_back + 1)).strftime("%Y-%m-%d")

    # ── Step 2: Scan all 4 contracts ──────────────────────────────────────────
    sec("STEP 2 : HTF → LTF Scan  |  S1/S2 CE + R1/R2 PE")

    tabs = st.tabs([
        f"S1 CE — {s1:,}",
        f"S2 CE — {s2:,}",
        f"R1 PE — {r1:,}",
        f"R2 PE — {r2:,}",
    ])

    contracts = [
        ("S1 CE", s1, "CE", "green"),
        ("S2 CE", s2, "CE", "green"),
        ("R1 PE", r1, "PE", "red"),
        ("R2 PE", r2, "PE", "red"),
    ]

    for tab, (label, strike, opt_type, cls) in zip(tabs, contracts):
        with tab:
            scan_one_contract(
                label, strike, opt_type, cls,
                expiry, expiry_api, from_date, to_date,
                htf_minutes, ltf_minutes,
                sl_buffer, round_step, token,
            )


if __name__ == "__main__":
    main()
else:
    main()
