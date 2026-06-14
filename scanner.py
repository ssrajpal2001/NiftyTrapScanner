"""
scanner.py — trap detection + backtest engine
No Streamlit imports — pure logic, testable standalone.

HTF scan  : scan_htf(df)      → bearish traps on any timeframe bars
LTF scan  : scan_ltf(df, zone)→ bearish traps inside a given HTF zone
Backtest  : backtest(df, traps, buffer, qty, lot_size) → trade P&L DataFrame
"""

import pandas as pd
from config import DEFAULT_SL_BUFFER, DEFAULT_QTY, DEFAULT_LOT_SIZE


# ── HTF (75-min) scan ─────────────────────────────────────────────────────────

def scan_htf(df: pd.DataFrame) -> tuple:
    """
    Scan any-timeframe OHLCV bars for BEARISH TRAPS.

    Returns
    -------
    events : pd.DataFrame   — one row per trapped event (OPEN or CLOSED)
    entries : list[dict]    — raw entry state dicts (includes ACTIVE entries)

    Zone is defined the moment the bearish entry is detected:
        Zone HIGH = Ref Bar LOW          (where bears shorted)
        Zone LOW  = Next Bar LOW         (bar immediately after Ref Bar)
        Zone Trigger = Zone LOW + (Zone HIGH - Zone LOW) / 3   ← your entry
        Target = Ref Bar HIGH            (bears' SL = your target)
    CLOSED when: after TRAPPED, any future bar LOW <= Zone Trigger
    """
    entries = []
    events  = []

    def make(ref_ts, entry, sl, next_low):
        zone_high    = entry
        zone_low     = next_low
        zone_trigger = zone_low + (zone_high - zone_low) / 3
        return {
            "ref_ts"      : ref_ts,
            "entry"       : entry,
            "sl"          : sl,
            "zone_high"   : zone_high,
            "zone_low"    : zone_low,
            "zone_trigger": zone_trigger,
            "status"      : "ACTIVE",
            "trapped_on"  : None,
            "closed_on"   : None,
            "event_idx"   : None,
        }

    for i in range(1, len(df)):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]
        ts   = curr["datetime"]

        for e in entries:
            if e["status"] == "CLOSED":
                continue

            # ── TRAP fires ────────────────────────────────────────────────────
            if e["status"] == "ACTIVE" and curr["high"] > e["sl"]:
                e["status"]     = "TRAPPED"
                e["trapped_on"] = ts
                e["event_idx"]  = len(events)
                events.append({
                    "Trap Bar"   : ts,
                    "Ref Bar"    : e["ref_ts"],
                    "Bear Entry" : round(e["entry"],        2),
                    "SL Level"   : round(e["sl"],           2),
                    "Zone High"  : round(e["zone_high"],    2),
                    "Zone Low"   : round(e["zone_low"],     2),
                    "Your Entry" : round(e["zone_trigger"], 2),
                    "Status"     : "OPEN",
                    "Close Bar"  : pd.NaT,
                })

            # ── CLOSE fires (your entry hit) ──────────────────────────────────
            if e["status"] == "TRAPPED" and curr["low"] <= e["zone_trigger"]:
                e["status"]    = "CLOSED"
                e["closed_on"] = ts
                events[e["event_idx"]]["Status"]    = "CLOSED"
                events[e["event_idx"]]["Close Bar"] = ts

        # New bearish setup detected on this bar
        if curr["low"] < prev["low"]:
            entries.append(make(prev["datetime"], prev["low"], prev["high"], curr["low"]))

    df_events = pd.DataFrame(events) if events else pd.DataFrame()
    return df_events, entries


# ── LTF (5-min) scan inside an HTF zone ───────────────────────────────────────

def scan_ltf(df: pd.DataFrame, htf_zone_high: float, htf_zone_low: float) -> tuple:
    """
    Scan 5-min bars for a bearish trap INSIDE the HTF zone band.
    Only considers bars whose price is within [htf_zone_low, htf_zone_high].

    Returns same structure as scan_htf:
        events   : pd.DataFrame
        entries  : list[dict]

    LTF Zone:
        LTF Zone HIGH = LTF Ref Bar LOW   (where 5-min bears entered)
        LTF Zone LOW  = LTF Next Bar LOW  (next bar after LTF ref bar)
        LTF Trigger   = LTF Zone LOW + (LTF Zone HIGH - LTF Zone LOW) / 3  ← your entry
        SL            = htf_zone_low - buffer   (handled in backtest, not here)
        Target        = htf SL Level            (also passed in backtest)
    """
    # Only work with bars that are inside or near the HTF zone
    zone_df = df[
        (df["low"]  <= htf_zone_high) &
        (df["close"] >= htf_zone_low * 0.95)   # 5% tolerance below zone low
    ].copy()

    if len(zone_df) < 2:
        return pd.DataFrame(), []

    zone_df = zone_df.reset_index(drop=True)
    return scan_htf(zone_df)   # same algorithm, just on filtered 5-min bars


# ── Intraday backtest ──────────────────────────────────────────────────────────

def backtest(df: pd.DataFrame,
             all_entries: list,
             htf_target_map: dict | None = None,
             buffer: float  = DEFAULT_SL_BUFFER,
             qty: int        = DEFAULT_QTY,
             lot_size: int   = DEFAULT_LOT_SIZE) -> pd.DataFrame:
    """
    Simulate intraday trades from scan results.

    Parameters
    ----------
    df            : OHLCV DataFrame (same timeframe as was scanned)
    all_entries   : list of entry dicts from scan_htf() or scan_ltf()
    htf_target_map: optional dict mapping closed_on timestamp → HTF target price.
                    If None, uses e["sl"] as target (HTF SL = Ref Bar HIGH).
    buffer        : points below Zone LOW for stop-loss
    qty / lot_size: position sizing

    Exit priority (same bar):
        1. SL hit first (worst case if both SL and target in same bar)
        2. Target hit
        3. Intraday square-off at last bar close of the same day
    """
    units  = qty * lot_size
    trades = []

    for e in all_entries:
        if e["status"] != "CLOSED" or e.get("closed_on") is None:
            continue

        entry_price = round(e["zone_trigger"], 2)
        target      = round(htf_target_map.get(e["closed_on"], e["sl"]) if htf_target_map else e["sl"], 2)
        sl_price    = round(e["zone_low"] - buffer, 2)
        entry_ts    = pd.Timestamp(e["closed_on"])
        entry_date  = entry_ts.date()

        # Bars strictly after entry on same calendar day
        future = df[
            (df["datetime"] > entry_ts) &
            (df["datetime"].dt.date == entry_date)
        ]

        exit_price  = None
        exit_reason = None
        exit_ts     = None

        for _, bar in future.iterrows():
            hit_sl  = bar["low"]  <= sl_price
            hit_tgt = bar["high"] >= target
            if hit_sl:                        # SL checked first (worst case)
                exit_price, exit_reason, exit_ts = sl_price, "SL", bar["datetime"]
                break
            if hit_tgt:
                exit_price, exit_reason, exit_ts = target,   "TARGET", bar["datetime"]
                break

        # No exit hit → intraday square-off
        if exit_price is None:
            ref = future.iloc[-1] if len(future) > 0 else None
            if ref is None:
                # Entry on last bar of day — use that bar's close
                ref_rows = df[df["datetime"] == entry_ts]
                ref = ref_rows.iloc[0] if len(ref_rows) > 0 else None
            if ref is None:
                continue
            exit_price  = round(ref["close"], 2)
            exit_reason = "SQUAREOFF"
            exit_ts     = ref["datetime"]

        pnl = round((exit_price - entry_price) * units, 2)

        trades.append({
            "Date"       : entry_date.strftime("%d %b %y"),
            "Entry Time" : entry_ts.strftime("%H:%M"),
            "Exit Time"  : exit_ts.strftime("%H:%M") if hasattr(exit_ts, "strftime") else str(exit_ts),
            "Entry"      : entry_price,
            "Target"     : target,
            "SL"         : sl_price,
            "Exit Price" : exit_price,
            "Exit"       : exit_reason,
            "P&L (₹)"   : pnl,
        })

    df_trades = pd.DataFrame(trades)
    if not df_trades.empty:
        df_trades["Cumulative P&L"] = df_trades["P&L (₹)"].cumsum().round(2)
    return df_trades


def trade_summary(df_trades: pd.DataFrame) -> dict:
    """Compute summary stats from a backtest trades DataFrame."""
    if df_trades.empty:
        return {}
    total    = len(df_trades)
    wins     = int((df_trades["P&L (₹)"] > 0).sum())
    losses   = int((df_trades["P&L (₹)"] <= 0).sum())
    net_pnl  = round(df_trades["P&L (₹)"].sum(), 2)
    win_rate = round(wins / total * 100, 1) if total else 0
    best     = round(df_trades["P&L (₹)"].max(), 2)
    worst    = round(df_trades["P&L (₹)"].min(), 2)
    avg_win  = round(df_trades[df_trades["P&L (₹)"] > 0]["P&L (₹)"].mean(), 2) if wins else 0
    avg_loss = round(df_trades[df_trades["P&L (₹)"] <= 0]["P&L (₹)"].mean(), 2) if losses else 0
    return {
        "total": total, "wins": wins, "losses": losses,
        "net_pnl": net_pnl, "win_rate": win_rate,
        "best": best, "worst": worst,
        "avg_win": avg_win, "avg_loss": avg_loss,
    }
