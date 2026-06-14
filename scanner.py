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

def backtest(df: pd.DataFrame | None,
             all_entries: list,
             htf_target_map: dict | None = None,
             buffer: float  = DEFAULT_SL_BUFFER,
             qty: int        = DEFAULT_QTY,
             lot_size: int   = DEFAULT_LOT_SIZE,
             max_entry_time: str | None = None,
             min_rr: float  = 0.0,
             min_zone_width: float = 0.0,
             df_map: dict | None = None) -> pd.DataFrame:
    """
    Simulate intraday trades from scan results.

    Parameters
    ----------
    df              : OHLCV DataFrame (same timeframe as was scanned)
    all_entries     : list of entry dicts from scan_htf() or scan_ltf()
    htf_target_map  : optional dict mapping closed_on timestamp → HTF target price.
                      If None, uses e["sl"] as target (HTF SL = Ref Bar HIGH).
    buffer          : points below Zone LOW for stop-loss
    qty / lot_size  : position sizing
    max_entry_time  : "HH:MM" — skip entries at or after this time (e.g. "13:30")
    min_rr          : minimum reward:risk ratio (target−entry)/(entry−sl); 0 = off
    min_zone_width  : minimum LTF zone width (zone_high−zone_low) in points; 0 = off
    df_map          : dict of contract_key → DataFrame. When provided, each entry
                      must have '_df_key' set; that contract's own price data is used
                      for future bar lookups (enables global one-trade-at-a-time
                      across multiple contracts mixed into all_entries).

    Exit priority (same bar):
        1. SL hit first (worst case if both SL and target in same bar)
        2. Target hit
        3. Intraday square-off at last bar close of the same day
    """
    units  = qty * lot_size
    trades = []

    # Pre-filter and sort by entry time
    eligible = sorted(
        [e for e in all_entries
         if e["status"] in ("TRAPPED", "CLOSED") and e.get("closed_on")],
        key=lambda e: pd.Timestamp(e["closed_on"])
    )
    open_until: pd.Timestamp | None = None

    for e in eligible:
        # Entry fires when the LTF bears get TRAPPED (high > their SL = Ref Bar HIGH)
        if e.get("trapped_on") is None:
            continue

        # Enter at LTF Ref Bar LOW — where 5-min bears entered short, price returns here
        entry_price = round(e["entry"], 2)
        target      = round(e.get("htf_target") or e["sl"], 2)
        sl_price    = round(e["zone_low"] - buffer, 2)
        entry_ts    = pd.Timestamp(e["closed_on"])   # bar where price hit Ref Bar LOW
        entry_date  = entry_ts.date()

        # Skip if a trade is already running
        if open_until is not None and entry_ts <= open_until:
            continue

        # ── Optional filters ─────────────────────────────────────────────────
        # Max entry time — no late-day entries
        if max_entry_time:
            cutoff_h, cutoff_m = map(int, max_entry_time.split(":"))
            if (entry_ts.hour, entry_ts.minute) >= (cutoff_h, cutoff_m):
                continue

        # Min zone width — skip micro/degenerate LTF zones
        if min_zone_width > 0:
            zone_w = e["zone_high"] - e["zone_low"]
            if zone_w < min_zone_width:
                continue

        # Min R:R — skip setups where reward is too small vs risk
        if min_rr > 0:
            risk   = max(entry_price - sl_price, 0.01)
            reward = target - entry_price
            if reward / risk < min_rr:
                continue

        # Use per-contract df when df_map is provided (global multi-contract backtest)
        entry_df = df
        if df_map is not None:
            entry_df = df_map.get(e.get("_df_key", ""), df)

        # Bars strictly after entry on same calendar day
        future = entry_df[
            (entry_df["datetime"] > entry_ts) &
            (entry_df["datetime"].dt.date == entry_date)
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
                ref_rows = entry_df[entry_df["datetime"] == entry_ts]
                ref = ref_rows.iloc[0] if len(ref_rows) > 0 else None
            if ref is None:
                continue
            exit_price  = round(ref["close"], 2)
            exit_reason = "SQUAREOFF"
            exit_ts     = ref["datetime"]

        pnl = round((exit_price - entry_price) * units, 2)
        open_until = pd.Timestamp(exit_ts) if hasattr(exit_ts, "strftime") else pd.Timestamp(str(exit_ts))

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
            "Contract"       : e.get("_contract", "—"),
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


# ── Phase 3 scenario definitions ──────────────────────────────────────────────
# Each scenario is a dict of kwargs passed to backtest_phase3().
# qty=2 is the minimum for partial T1 exit (50% = 1 lot).
PHASE3_SCENARIOS = {
    "Baseline (CP-SL only)": {
        "qty": 1,
        "t1_enabled": False,
        "hard_floor_enabled": False,
        "breakeven_trail": False,
        "description": "Main SL = counterpart fires. No T1 partial, no hard floor. (Current logic)",
    },
    "Scenario A: T1 + Wait CP": {
        "qty": 2,
        "t1_enabled": True,
        "hard_floor_enabled": False,
        "breakeven_trail": False,
        "description": "Book 50% at T1 (HTF target). Hold remaining until counterpart fires.",
    },
    "Scenario B: T1 + Breakeven SL": {
        "qty": 2,
        "t1_enabled": True,
        "hard_floor_enabled": False,
        "breakeven_trail": True,
        "description": "Book 50% at T1. Move SL to entry (breakeven) for remaining 50%.",
    },
    "Scenario C: T1 + Hard Floor SL": {
        "qty": 2,
        "t1_enabled": True,
        "hard_floor_enabled": True,
        "breakeven_trail": False,
        "description": "Book 50% at T1. Hard floor exits ALL units if bar close < zone_low×0.97.",
    },
    "Scenario D: T1 + Breakeven + Hard Floor": {
        "qty": 2,
        "t1_enabled": True,
        "hard_floor_enabled": True,
        "breakeven_trail": True,
        "description": "Book 50% at T1. Breakeven SL for remaining. Hard floor as safety net.",
    },
    "Scenario E: Hard Floor only (no T1)": {
        "qty": 1,
        "t1_enabled": False,
        "hard_floor_enabled": True,
        "breakeven_trail": False,
        "description": "Hard floor SL only, no T1 partial. Exits if bar close < zone_low×0.97.",
    },
}


def backtest_phase3(df_active: pd.DataFrame,
                    active_entries: list,
                    counterpart_entries: list,
                    qty: int = DEFAULT_QTY,
                    lot_size: int = DEFAULT_LOT_SIZE,
                    sl_direction_filter: bool = False,
                    max_entry_time: str | None = None,
                    min_rr: float = 0.0,
                    min_zone_width: float = 0.0,
                    # ── Scenario parameters ──────────────────────────────────
                    t1_enabled: bool = False,
                    t1_partial_pct: float = 0.5,
                    hard_floor_enabled: bool = False,
                    hard_floor_pct: float = 3.0,
                    breakeven_trail: bool = False,
                    ) -> pd.DataFrame:
    """
    Phase 3 backtest with cross-pair SL/target rules.

    Main SL  = counterpart LTF entry fires → exit at active bar CLOSE
    T1       = HTF Ref Bar HIGH → partial exit (t1_partial_pct of units)
    Hard Floor = zone_low × (1 − hard_floor_pct/100) → full exit if bar close < floor
    Breakeven trail = after T1 hit, move SL to entry for remaining units

    Exit priority per bar (checked in order):
      1. Hard floor SL  (if enabled, bar CLOSE < floor → exit all remaining)
      2. Breakeven SL   (if T1 already booked, bar LOW ≤ entry → exit remaining)
      3. T1 partial     (bar HIGH ≥ T1 target → book t1_partial_pct)
      4. Counterpart SL (bar timestamp ≥ next_cp_ts → exit remaining at bar close)
      5. Square-off     (last bar of the day → exit remaining at close)
    """
    units = qty * lot_size

    cp_sorted = sorted(
        [(pd.Timestamp(e["closed_on"]), round(e["entry"], 2))
         for e in counterpart_entries
         if e.get("closed_on") and e["status"] in ("CLOSED", "TRAPPED")],
        key=lambda x: x[0]
    )
    eligible = sorted(
        [e for e in active_entries
         if e["status"] in ("TRAPPED", "CLOSED") and e.get("closed_on")],
        key=lambda e: pd.Timestamp(e["closed_on"])
    )
    open_until: pd.Timestamp | None = None
    trades = []

    for e in eligible:
        entry_price = round(e["entry"], 2)
        target      = round(e.get("htf_target") or e["sl"], 2)
        entry_ts    = pd.Timestamp(e["closed_on"])
        entry_date  = entry_ts.date()

        if open_until is not None and entry_ts <= open_until:
            continue

        if max_entry_time:
            cutoff_h, cutoff_m = map(int, max_entry_time.split(":"))
            if (entry_ts.hour, entry_ts.minute) >= (cutoff_h, cutoff_m):
                continue

        if min_zone_width > 0:
            zone_w = e["zone_high"] - e["zone_low"]
            if zone_w < min_zone_width:
                continue

        if min_rr > 0:
            sl_price = round(e["zone_low"] - DEFAULT_SL_BUFFER, 2)
            risk   = max(entry_price - sl_price, 0.01)
            reward = target - entry_price
            if reward / risk < min_rr:
                continue

        next_cp_ts = None
        for i, (cp_ts, cp_price) in enumerate(cp_sorted):
            if cp_ts <= entry_ts:
                continue
            if sl_direction_filter and i > 0:
                if cp_price <= cp_sorted[i - 1][1]:
                    continue
            next_cp_ts = cp_ts
            break

        future = df_active[
            (df_active["datetime"] > entry_ts) &
            (df_active["datetime"].dt.date == entry_date)
        ]

        hard_floor   = round(e["zone_low"] * (1 - hard_floor_pct / 100), 2)
        units_open   = units
        t1_booked    = False
        t1_units     = 0
        t1_pnl       = 0.0
        exit_price   = None
        exit_reason  = None
        exit_ts      = None
        partial_note = ""

        for _, bar in future.iterrows():
            bar_ts = pd.Timestamp(bar["datetime"])

            # 1. Hard floor SL — exit ALL remaining if bar close < floor
            if hard_floor_enabled and bar["close"] < hard_floor:
                exit_price  = round(bar["close"], 2)
                exit_reason = "HARD_FLOOR_SL"
                exit_ts     = bar_ts
                break

            # 2. Breakeven SL — only active after T1 booked
            if t1_booked and breakeven_trail and bar["low"] <= entry_price:
                exit_price  = entry_price
                exit_reason = "BREAKEVEN_SL"
                exit_ts     = bar_ts
                break

            # 3. T1 partial exit
            if t1_enabled and not t1_booked and bar["high"] >= target:
                t1_units  = max(1, round(units * t1_partial_pct))
                t1_pnl    = round((target - entry_price) * t1_units, 2)
                units_open = units - t1_units
                t1_booked  = True
                partial_note = f"T1@{target}"
                if units_open <= 0:
                    # All units booked at T1 — trade fully closed
                    exit_price  = target
                    exit_reason = "TARGET"
                    exit_ts     = bar_ts
                    break
                continue  # remaining units still tracking

            # 4. Counterpart SL — exit remaining at this bar's close
            if next_cp_ts and bar_ts >= next_cp_ts:
                exit_price  = round(bar["close"], 2)
                exit_reason = "SL"
                exit_ts     = bar_ts
                break

            # 5. Full target reached (no partial booking active)
            if not t1_enabled and bar["high"] >= target:
                exit_price  = target
                exit_reason = "TARGET"
                exit_ts     = bar_ts
                break

        # Square-off if no exit triggered
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

        # Total P&L = T1 partial (if booked) + remaining exit
        remaining_pnl = round((exit_price - entry_price) * units_open, 2)
        total_pnl     = round(t1_pnl + remaining_pnl, 2)

        open_until = pd.Timestamp(exit_ts) if hasattr(exit_ts, "strftime") else pd.Timestamp(str(exit_ts))
        cp_sl_label = next_cp_ts.strftime("%H:%M") if next_cp_ts else "none"

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
            "LTF Target (T1)": target,
            "CP SL Trigger"  : cp_sl_label,
            "LTF Exit Price" : exit_price,
            "T1 Booked"      : f"{t1_units}u @ {target}" if t1_booked else "—",
            "Exit"           : exit_reason,
            "Notes"          : partial_note,
            "P&L (Rs)"       : total_pnl,
        })

    df_out = pd.DataFrame(trades)
    if not df_out.empty:
        df_out["Cumulative P&L"] = df_out["P&L (Rs)"].cumsum().round(2)
    return df_out


def run_scenario_comparison(df_active: pd.DataFrame,
                             active_entries: list,
                             counterpart_entries: list,
                             selected_scenarios: list | None = None,
                             lot_size: int = DEFAULT_LOT_SIZE,
                             sl_direction_filter: bool = False,
                             max_entry_time: str | None = None,
                             min_rr: float = 0.0,
                             min_zone_width: float = 0.0,
                             hard_floor_pct: float = 3.0) -> dict:
    """
    Run multiple Phase 3 scenarios against the same entries and return a dict of
    {scenario_name: trades_DataFrame}.
    selected_scenarios: list of keys from PHASE3_SCENARIOS; None = run all.
    """
    to_run = selected_scenarios or list(PHASE3_SCENARIOS.keys())
    results = {}
    for name in to_run:
        if name not in PHASE3_SCENARIOS:
            continue
        params = PHASE3_SCENARIOS[name]
        df = backtest_phase3(
            df_active, active_entries, counterpart_entries,
            qty=params["qty"],
            lot_size=lot_size,
            sl_direction_filter=sl_direction_filter,
            max_entry_time=max_entry_time,
            min_rr=min_rr,
            min_zone_width=min_zone_width,
            t1_enabled=params["t1_enabled"],
            hard_floor_enabled=params["hard_floor_enabled"],
            breakeven_trail=params["breakeven_trail"],
            hard_floor_pct=hard_floor_pct,
        )
        results[name] = df
    return results


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
