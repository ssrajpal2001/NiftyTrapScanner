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

            # ── CLOSE fires when price returns to LTF Ref Bar LOW (bear entry) ──
            if e["status"] == "TRAPPED" and curr["low"] <= e["entry"]:
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

def scan_ltf(df: pd.DataFrame,
             htf_zone_high: float,
             htf_zone_low: float,
             htf_ref_bar: str = "",
             htf_trap_bar: str = "",
             htf_target: float = 0.0) -> tuple:
    """
    Scan 5-min bars for a bearish trap INSIDE the HTF zone band.
    Tags every LTF entry dict with its parent HTF trap for full traceability.

    Extra keys added to each entry:
        htf_ref_bar, htf_trap_bar, htf_zone_high, htf_zone_low, htf_target
    """
    zone_df = df[
        (df["low"]  <= htf_zone_high) &
        (df["close"] >= htf_zone_low * 0.95)
    ].copy()

    if len(zone_df) < 2:
        return pd.DataFrame(), []

    zone_df = zone_df.reset_index(drop=True)
    df_events, entries = scan_htf(zone_df)

    # Tag each LTF entry with parent HTF trap metadata
    for e in entries:
        e["htf_ref_bar"]   = htf_ref_bar
        e["htf_trap_bar"]  = htf_trap_bar
        e["htf_zone_high"] = htf_zone_high
        e["htf_zone_low"]  = htf_zone_low
        e["htf_target"]    = htf_target

    return df_events, entries


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
        # Entry fires when the LTF bears get TRAPPED (high > their SL = Ref Bar HIGH)
        if e["status"] not in ("TRAPPED", "CLOSED") or e.get("trapped_on") is None:
            continue

        # Enter at LTF Ref Bar LOW — where 5-min bears entered short, price returns here
        entry_price = round(e["entry"], 2)
        target      = round(e.get("htf_target") or e["sl"], 2)
        sl_price    = round(e["zone_low"] - buffer, 2)
        entry_ts    = pd.Timestamp(e["closed_on"])   # bar where price hit Ref Bar LOW
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

        # HTF parent trap metadata (populated by scan_ltf tagging)
        htf_ref  = e.get("htf_ref_bar",   "—")
        htf_trap = e.get("htf_trap_bar",  "—")
        htf_zh   = e.get("htf_zone_high", "—")
        htf_zl   = e.get("htf_zone_low",  "—")
        htf_tgt  = e.get("htf_target",    target)

        trades.append({
            # ── HTF parent trap (for traceability) ────────────────────────────
            "HTF Ref Bar"    : htf_ref,
            "HTF Trap Bar"   : htf_trap,
            "HTF Zone High"  : htf_zh,
            "HTF Zone Low"   : htf_zl,
            "HTF Target"     : htf_tgt,
            # ── LTF trade ─────────────────────────────────────────────────────
            "LTF Date"       : entry_date.strftime("%d %b %y"),
            "LTF Entry Time" : entry_ts.strftime("%H:%M"),
            "LTF Exit Time"  : exit_ts.strftime("%H:%M") if hasattr(exit_ts, "strftime") else str(exit_ts),
            "LTF Entry"      : entry_price,
            "LTF Zone Low"   : round(e["zone_low"], 2),
            "LTF Target"     : target,
            "LTF SL"         : sl_price,
            "LTF Exit Price" : exit_price,
            "Exit"           : exit_reason,
            "P&L (Rs)"      : pnl,
        })

    df_trades = pd.DataFrame(trades)
    if not df_trades.empty:
        df_trades["Cumulative P&L"] = df_trades["P&L (Rs)"].cumsum().round(2)
    return df_trades


def backtest_phase3(df_active: pd.DataFrame,
                    active_entries: list,
                    counterpart_entries: list,
                    qty: int = DEFAULT_QTY,
                    lot_size: int = DEFAULT_LOT_SIZE) -> pd.DataFrame:
    """
    Phase 3 backtest with cross-pair SL/target rules:
      SL     = when counterpart LTF entry fires (counterpart closed_on timestamp)
      Target = htf_target on active side (HTF ref bar HIGH)

    Only ONE side trade is active at a time — counterpart entry = active SL.
    """
    units = qty * lot_size

    # Build sorted list of counterpart entry timestamps
    cp_times = sorted([
        pd.Timestamp(e["closed_on"])
        for e in counterpart_entries
        if e.get("closed_on") and e["status"] in ("CLOSED", "TRAPPED")
    ])

    trades = []
    for e in active_entries:
        if e["status"] not in ("TRAPPED", "CLOSED") or not e.get("closed_on"):
            continue

        entry_price = round(e["entry"], 2)
        target      = round(e.get("htf_target") or e["sl"], 2)
        entry_ts    = pd.Timestamp(e["closed_on"])
        entry_date  = entry_ts.date()

        # Next counterpart entry strictly after our entry = SL trigger
        next_cp_ts = next((t for t in cp_times if t > entry_ts), None)

        # Find the counterpart entry price at that timestamp (exit price for SL)
        cp_exit_price = None
        if next_cp_ts:
            match = next((x for x in counterpart_entries
                          if x.get("closed_on") and
                          pd.Timestamp(x["closed_on"]) == next_cp_ts), None)
            cp_exit_price = round(match["entry"], 2) if match else None

        future = df_active[
            (df_active["datetime"] > entry_ts) &
            (df_active["datetime"].dt.date == entry_date)
        ]

        exit_price  = None
        exit_reason = None
        exit_ts     = None

        for _, bar in future.iterrows():
            bar_ts = pd.Timestamp(bar["datetime"])

            # SL fires when counterpart entry bar is reached
            if next_cp_ts and bar_ts >= next_cp_ts:
                exit_price  = cp_exit_price if cp_exit_price else round(bar["close"], 2)
                exit_reason = "SL"
                exit_ts     = next_cp_ts
                break

            if bar["high"] >= target:
                exit_price  = target
                exit_reason = "TARGET"
                exit_ts     = bar_ts
                break

        if exit_price is None:
            ref = future.iloc[-1] if len(future) > 0 else None
            if ref is None:
                rows = df_active[df_active["datetime"] == entry_ts]
                ref  = rows.iloc[0] if len(rows) > 0 else None
            if ref is None:
                continue
            exit_price  = round(ref["close"], 2)
            exit_reason = "SQUAREOFF"
            exit_ts     = ref["datetime"]

        pnl = round((exit_price - entry_price) * units, 2)

        sl_label = (next_cp_ts.strftime("%H:%M") if next_cp_ts else "none")
        trades.append({
            "HTF Ref Bar"    : e.get("htf_ref_bar",   "—"),
            "HTF Trap Bar"   : e.get("htf_trap_bar",  "—"),
            "HTF Zone High"  : e.get("htf_zone_high", "—"),
            "HTF Zone Low"   : e.get("htf_zone_low",  "—"),
            "HTF Target"     : round(e.get("htf_target", target), 2),
            "LTF Date"       : entry_date.strftime("%d %b %y"),
            "LTF Entry Time" : entry_ts.strftime("%H:%M"),
            "LTF Exit Time"  : exit_ts.strftime("%H:%M") if hasattr(exit_ts, "strftime") else str(exit_ts),
            "LTF Entry"      : entry_price,
            "LTF Zone Low"   : round(e["zone_low"], 2),
            "LTF Target"     : target,
            "CP SL Trigger"  : sl_label,
            "LTF Exit Price" : exit_price,
            "Exit"           : exit_reason,
            "P&L (Rs)"       : pnl,
        })

    df_out = pd.DataFrame(trades)
    if not df_out.empty:
        df_out["Cumulative P&L"] = df_out["P&L (Rs)"].cumsum().round(2)
    return df_out


def trade_summary(df_trades: pd.DataFrame) -> dict:
    """Compute summary stats from a backtest trades DataFrame."""
    if df_trades.empty:
        return {}
    total    = len(df_trades)
    wins     = int((df_trades["P&L (Rs)"] > 0).sum())
    losses   = int((df_trades["P&L (Rs)"] <= 0).sum())
    net_pnl  = round(df_trades["P&L (Rs)"].sum(), 2)
    win_rate = round(wins / total * 100, 1) if total else 0
    best     = round(df_trades["P&L (Rs)"].max(), 2)
    worst    = round(df_trades["P&L (Rs)"].min(), 2)
    avg_win  = round(df_trades[df_trades["P&L (Rs)"] > 0]["P&L (Rs)"].mean(), 2) if wins else 0
    avg_loss = round(df_trades[df_trades["P&L (Rs)"] <= 0]["P&L (Rs)"].mean(), 2) if losses else 0
    return {
        "total": total, "wins": wins, "losses": losses,
        "net_pnl": net_pnl, "win_rate": win_rate,
        "best": best, "worst": worst,
        "avg_win": avg_win, "avg_loss": avg_loss,
    }
