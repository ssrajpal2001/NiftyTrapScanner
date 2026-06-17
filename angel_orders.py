"""
angel_orders.py - Angel One SmartAPI integration.

PAPER_MODE = True  ->  logs orders, zero real API calls, P&L tracked from WS prices.
PAPER_MODE = False ->  live trading (flip only after paper testing sign-off).

Paper trade lifecycle:
  signal fires -> log_entry() -> track via WS prices -> SL/T1 from original zone
  -> log_exit() -> append to paper_trades log
"""

from __future__ import annotations
import os
import math
import pyotp
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Angel One credentials (from .env — never hardcode) ─────────────────────────
ANGEL_API_KEY    = os.environ.get("ANGEL_API_KEY", "")
ANGEL_API_SECRET = os.environ.get("ANGEL_API_SECRET", "")
ANGEL_CLIENT_ID  = os.environ.get("ANGEL_CLIENT_ID", "")
ANGEL_TOTP_SEC   = os.environ.get("ANGEL_TOTP_SECRET", "")
ANGEL_PASSWORD   = os.environ.get("ANGEL_PASSWORD", "")   # MPIN / login password

# ── Safety flag ─────────────────────────────────────────────────────────────────
PAPER_MODE: bool = False   # Live trading enabled — set True to revert to paper

# ── Shared paper trade store ────────────────────────────────────────────────────
_lock         = threading.Lock()
_open_trades:  list[dict] = []
_closed_trades: list[dict] = []
_session_obj  = None             # Angel One session (live mode)

LOG_PATH    = Path(__file__).parent / "logs" / "paper_trades.log"
_STATE_PATH = Path(__file__).parent / "logs" / "paper_state.json"
LOG_PATH.parent.mkdir(exist_ok=True)


def _save_state() -> None:
    import json
    try:
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"open": _open_trades, "closed": _closed_trades}, f, default=str)
    except Exception as e:
        _log(f"STATE SAVE ERROR: {e}")


def _load_state() -> None:
    import json
    from datetime import date as _date
    global _open_trades, _closed_trades
    if not _STATE_PATH.exists():
        return
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        today = str(_date.today())
        _open_trades   = [t for t in data.get("open",   [])
                          if str(t.get("entry_time", "")).startswith(today)]
        _closed_trades = [t for t in data.get("closed", [])
                          if str(t.get("entry_time", "")).startswith(today)]
        if _open_trades or _closed_trades:
            _log(f"STATE RESTORED: {len(_open_trades)} open, {len(_closed_trades)} closed trades from today")
    except Exception as e:
        _log(f"STATE LOAD ERROR: {e}")


# ── Logging helper ──────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode())
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


_load_state()


# ── Symbol builders ─────────────────────────────────────────────────────────────
def sensex_symbol(strike: int, side: str, expiry: date) -> str:
    """Angel One BFO symbol: SENSEX2661876500CE  format={YY}{M}{DD}{STRIKE}{side}
    Confirmed from live order book — M has no leading zero."""
    return f"SENSEX{expiry.strftime('%y')}{expiry.month}{expiry.strftime('%d')}{strike}{side}"


def crudeoil_symbol(strike: int, side: str, expiry: date) -> str:
    """Angel One MCX symbol: CRUDEOIL16JUL267350CE  format={DD}{MON}{YY}{STRIKE}{side}
    Confirmed from live order book."""
    return f"CRUDEOIL{expiry.strftime('%d%b%y').upper()}{strike}{side}"


def get_1itm_strike(spot_ltp: float, side: str, step: int = 100) -> int:
    if side == "CE":
        return int(math.floor(spot_ltp / step) * step)
    else:
        return int(math.ceil(spot_ltp / step) * step)


# ── Symbol token lookup (required for Angel One live orders) ────────────────────
_token_cache: dict[str, str] = {}

def _lookup_token(exchange: str, symbol: str) -> str:
    """Fetch Angel One instrument token via searchScrip. Cached per session."""
    global _session_obj
    key = f"{exchange}:{symbol}"
    if key in _token_cache:
        return _token_cache[key]
    if _session_obj is None:
        _log(f"TOKEN LOOKUP: session not ready for {symbol}")
        return ""
    try:
        result = _session_obj.searchScrip(exchange, symbol)
        if result and result.get("status") and result.get("data"):
            token = result["data"][0].get("symboltoken", "")
            if token:
                _token_cache[key] = token
                _log(f"TOKEN OK: {symbol} -> {token}")
                return token
        _log(f"TOKEN NOT FOUND: {exchange} {symbol}  resp={result}")
    except Exception as e:
        _log(f"TOKEN LOOKUP ERROR: {e}")
    return ""


# ── Angel One login ─────────────────────────────────────────────────────────────
def login(api_key: str = "", client_id: str = "", password: str = "",
          totp_secret: str = "") -> bool:
    """Login to Angel One SmartAPI. Uses .env credentials if args omitted."""
    global _session_obj
    if PAPER_MODE:
        _log("PAPER MODE: login skipped")
        return True
    _ak  = api_key    or ANGEL_API_KEY
    _cid = client_id  or ANGEL_CLIENT_ID
    _pw  = password   or ANGEL_PASSWORD
    _ts  = totp_secret or ANGEL_TOTP_SEC
    if not all([_ak, _cid, _pw, _ts]):
        _log(f"Angel login SKIPPED — missing credentials (check .env): "
             f"api_key={'OK' if _ak else 'NO'} client={'OK' if _cid else 'NO'} "
             f"password={'OK' if _pw else 'NO'} totp={'OK' if _ts else 'NO'}")
        return False
    try:
        from SmartApi import SmartConnect
        totp = pyotp.TOTP(_ts).now()
        obj  = SmartConnect(api_key=_ak)
        data = obj.generateSession(_cid, _pw, totp)
        if data.get("status"):
            _session_obj = obj
            _log(f"Angel One login SUCCESS: {_cid}")
            return True
        _log(f"Angel One login FAILED: {data}")
        return False
    except Exception as e:
        _log(f"Angel One login ERROR: {e}")
        return False


# ── Trade entry ─────────────────────────────────────────────────────────────────
def log_entry(
    side: str,
    spot_ltp: float,
    expiry: date,
    qty: int,
    sl_price: float,
    target_price: float,
    tracked_sym: str,
    signal_src: str = "",
    lots: int = 2,
    lot_size: int = 20,
    strike: int | None = None,        # pass explicitly for MCX (already known)
    exchange: str = "BFO",            # "BFO" for Sensex, "MCX" for CrudeOil, "NSE" for Nifty
    index_name: str = "Sensex",       # used to pick symbol builder
    product_type: str = "CARRYFORWARD",  # CARRYFORWARD=NRML, INTRADAY=MIS
    paper_override: bool | None = None,  # None=use global PAPER_MODE; True/False=force per-script
    live_key: str = "",                  # Upstox instrument key of the actual order strike (1-ITM)
) -> Optional[dict]:
    """
    Record a trade entry. In live mode places a MARKET BUY on Angel One.
    """
    # Build Angel One symbol
    _is_paper = PAPER_MODE if paper_override is None else paper_override
    _step = 100 if index_name == "CrudeOil" else 100
    if strike is None:
        strike = get_1itm_strike(spot_ltp, side, _step)

    if index_name == "CrudeOil":
        symbol = crudeoil_symbol(strike, side, expiry)
    else:
        symbol = sensex_symbol(strike, side, expiry)

    trade = {
        "id"           : f"{'LIVE' if not PAPER_MODE else 'PT'}_{datetime.now().strftime('%H%M%S')}_{side}",
        "side"         : side,
        "strike"       : strike,
        "symbol"       : symbol,
        "exchange"     : exchange,
        "index_name"   : index_name,
        "qty"          : qty,
        "entry_time"   : datetime.now().isoformat(),
        "entry_px"     : None,
        "sl"           : sl_price,
        "target"       : target_price,
        "tracked_sym"  : tracked_sym,
        "signal_src"   : signal_src,
        "spot_at_entry": spot_ltp,
        "status"       : "OPEN",
        "exit_time"    : None,
        "exit_px"      : None,
        "pnl"          : None,
        "exit_reason"  : None,
        "lots"         : lots,
        "lot_size"     : lot_size,
        "product_type" : product_type,
        "paper_mode"   : _is_paper,
        "t1_booked"    : False,
        "trailing_mode": False,
        "trail_sl"     : sl_price,
        "trail_zones"  : [],
        "qty_remaining": qty,
        "live_key"     : live_key,     # Upstox key of actual order strike (1-ITM or same as scan strike)
    }

    with _lock:
        # Block any new entry for this index while ANY trade (CE or PE) is still open
        already = [t for t in _open_trades
                   if t.get("index_name") == index_name and t["status"] == "OPEN"]
        if already:
            _log(f"SKIP {side}: {already[0]['side']} trade already open for {index_name} ({already[0]['symbol']})")
            return None
        _open_trades.append(trade)
        _save_state()

    mode_tag  = "PAPER" if _is_paper else "LIVE"
    _log(f"{mode_tag} ENTRY [{side}]  {symbol}  Strike:{strike}  "
         f"Qty:{qty}  SL:{sl_price:.1f}  T1:{target_price:.1f}  "
         f"Spot:{spot_ltp:.0f}  Src:{signal_src}")

    if not _is_paper:
        tok = _lookup_token(exchange, symbol)
        order_id = _place_live_order(symbol, qty, "BUY", exchange=exchange, sym_token=tok,
                                     product_type=product_type)
        with _lock:
            trade["entry_order_id"] = order_id

    return trade


# ── Update entry price from first WS tick ──────────────────────────────────────
def update_entry_px(trade_id: str, ltp: float) -> None:
    with _lock:
        for t in _open_trades:
            if t["id"] == trade_id and t["entry_px"] is None:
                t["entry_px"] = ltp
                _log(f"ENTRY FILL [{trade_id}]: {ltp:.1f}")
                break


# ── Check SL / Target on every WS tick ─────────────────────────────────────────
def check_exits(tracked_prices: dict[str, float]) -> list[dict]:
    closed = []
    with _lock:
        for t in list(_open_trades):
            ref_ltp = tracked_prices.get(t["tracked_sym"])
            if ref_ltp is None:
                continue
            if t["entry_px"] is None:
                t["entry_px"] = ref_ltp

            sl_level = t["trail_sl"] if t["trailing_mode"] else t["sl"]
            if ref_ltp <= sl_level:
                t["status"]      = "CLOSED"
                t["exit_time"]   = datetime.now().isoformat()
                t["exit_px"]     = ref_ltp
                t["exit_reason"] = "TRAIL_SL_HIT" if t["trailing_mode"] else "SL_HIT"
                qty_out          = t["qty_remaining"]
                t["pnl"]         = round((ref_ltp - (t["entry_px"] or ref_ltp)) * qty_out, 2)
                _open_trades.remove(t)
                _closed_trades.append(t)
                _save_state()
                closed.append(t)
                _log(f"EXIT [{t['id']}] {t['exit_reason']}  "
                     f"qty={qty_out}  entry={t['entry_px']}  exit={ref_ltp:.1f}  "
                     f"P&L={'+'if t['pnl']>=0 else ''}{t['pnl']:,.0f}")
                if not t.get("paper_mode", PAPER_MODE):
                    tok = _lookup_token(t.get("exchange", "BSE"), t["symbol"])
                    _place_live_order(t["symbol"], qty_out, "SELL",
                                      exchange=t.get("exchange", "BSE"), sym_token=tok,
                                      product_type=t.get("product_type", "CARRYFORWARD"))
                continue

            if not t["t1_booked"] and ref_ltp >= t["target"]:
                half_qty        = max(1, t["qty"] // 2)
                t1_pnl          = round((ref_ltp - (t["entry_px"] or ref_ltp)) * half_qty, 2)
                t["t1_booked"]  = True
                t["trailing_mode"] = True
                t["qty_remaining"] = t["qty"] - half_qty
                t["trail_sl"]   = t["sl"]
                _save_state()
                _log(f"T1 PARTIAL [{t['id']}]  50% booked  "
                     f"qty={half_qty}  exit={ref_ltp:.1f}  "
                     f"Partial P&L={'+'if t1_pnl>=0 else ''}{t1_pnl:,.0f}  "
                     f"Remaining->TRAILING")
                if not t.get("paper_mode", PAPER_MODE):
                    tok = _lookup_token(t.get("exchange", "BSE"), t["symbol"])
                    _place_live_order(t["symbol"], half_qty, "SELL",
                                      exchange=t.get("exchange", "BSE"), sym_token=tok,
                                      product_type=t.get("product_type", "CARRYFORWARD"))

    return closed


# ── Trail SL update ─────────────────────────────────────────────────────────────
def update_trail_sl(df5_live: "pd.DataFrame") -> None:
    import pandas as pd
    if df5_live is None or df5_live.empty:
        return

    with _lock:
        for t in _open_trades:
            if not t["trailing_mode"]:
                continue
            entry_ref = t["entry_px"] or t["spot_at_entry"]
            bars = df5_live.copy().reset_index()
            col  = "dt" if "dt" in bars.columns else bars.columns[0]
            bars = bars.rename(columns={col: "dt"}).sort_values("dt")

            for i in range(1, len(bars)):
                prev = bars.iloc[i - 1]
                curr = bars.iloc[i]
                zone_low  = float(curr["low"])
                zone_high = float(curr["high"])
                prev_low  = float(prev["low"])

                if zone_low >= entry_ref and zone_low < prev_low:
                    zone_id = f"{zone_low:.1f}_{zone_high:.1f}"
                    watched = next((z for z in t["trail_zones"] if z["id"] == zone_id), None)
                    if watched is None:
                        watched = {"id": zone_id, "zone_low": zone_low,
                                   "zone_high": zone_high, "trapped": False,
                                   "returned": False, "triggered": False}
                        t["trail_zones"].append(watched)
                        _log(f"TRAIL WATCH [{t['id']}] zone: low={zone_low:.1f} high={zone_high:.1f}")
                    if watched["triggered"]:
                        continue

                    later_bars = bars[bars["dt"] > curr["dt"]]
                    if not watched["trapped"]:
                        for _, lb in later_bars.iterrows():
                            if float(lb["high"]) >= zone_high:
                                watched["trapped"] = True
                                _log(f"TRAIL [{t['id']}] bears TRAPPED @ {zone_high:.1f}")
                                break
                    if not watched["trapped"]:
                        continue

                    trap_bars = bars[bars["dt"] > curr["dt"]]
                    if not watched["returned"]:
                        for _, lb in trap_bars.iterrows():
                            if float(lb["low"]) <= zone_low:
                                watched["returned"] = True
                                _log(f"TRAIL [{t['id']}] returned to {zone_low:.1f}")
                                break
                    if not watched["returned"]:
                        continue

                    return_bars = bars[bars["dt"] > curr["dt"]]
                    for _, lb in return_bars.iterrows():
                        if float(lb["low"]) <= zone_low:
                            bounce_bars = bars[bars["dt"] > lb["dt"]]
                            for _, bb in bounce_bars.iterrows():
                                if float(bb["high"]) >= zone_high:
                                    old_sl = t["trail_sl"]
                                    if zone_low > old_sl:
                                        t["trail_sl"]        = zone_low
                                        watched["triggered"] = True
                                        _log(f"TRAIL SL MOVED [{t['id']}] "
                                             f"{old_sl:.1f} -> {zone_low:.1f}")
                                    break
                            break


# ── Square off all open trades ──────────────────────────────────────────────────
def square_off_all(current_prices: dict[str, float]) -> None:
    """Called at EOD auto-close (equity 3:30 PM, MCX 11:30 PM)."""
    with _lock:
        for t in list(_open_trades):
            ref_ltp          = current_prices.get(t["tracked_sym"], t["entry_px"] or 0)
            t["status"]      = "CLOSED"
            t["exit_time"]   = datetime.now().isoformat()
            t["exit_px"]     = ref_ltp
            t["exit_reason"] = "SQ_OFF_AUTO"
            t["pnl"]         = round((ref_ltp - (t["entry_px"] or ref_ltp)) * t["qty"], 2)
            _open_trades.remove(t)
            _closed_trades.append(t)
            _save_state()
            _log(f"SQ_OFF [{t['id']}]  exit={ref_ltp:.1f}  "
                 f"P&L={'+'if t['pnl']>=0 else ''}{t['pnl']:,.0f}")
            if not t.get("paper_mode", PAPER_MODE):
                tok = _lookup_token(t.get("exchange", "BSE"), t["symbol"])
                _place_live_order(t["symbol"], t["qty"], "SELL",
                                  exchange=t.get("exchange", "BSE"), sym_token=tok,
                                  product_type=t.get("product_type", "CARRYFORWARD"))


# ── Public getters ──────────────────────────────────────────────────────────────
def get_open_trades() -> list[dict]:
    global _open_trades, _closed_trades
    today = str(date.today())
    with _lock:
        stale = [t for t in _open_trades
                 if not str(t.get("entry_time", "")).startswith(today)]
        if stale:
            _log(f"DAY BOUNDARY: purging {len(stale)} stale open trade(s) from previous day")
            _open_trades = [t for t in _open_trades
                            if str(t.get("entry_time", "")).startswith(today)]
        return list(_open_trades)

def get_closed_trades() -> list[dict]:
    with _lock:
        return list(_closed_trades)

def get_session_pnl() -> float:
    with _lock:
        return sum(t["pnl"] or 0 for t in _closed_trades)


def get_broker_positions() -> list[dict]:
    """Fetch open positions from Angel One. Returns list of {symbol, netqty, ltp}."""
    if PAPER_MODE or _session_obj is None:
        return []
    try:
        resp = _session_obj.position()
        if resp and resp.get("status") and resp.get("data"):
            return resp["data"]
    except Exception as ex:
        _log(f"get_broker_positions error: {ex}")
    return []


def sync_with_broker() -> list[str]:
    """
    Compare open trades against broker positions.
    Any trade whose symbol has netqty=0 at broker is auto-closed as MANUAL_CLOSE.
    Returns list of symbols that were auto-closed.
    """
    global _open_trades, _closed_trades
    broker_pos = get_broker_positions()
    # Build map: symbol → net qty
    net_qty_map: dict[str, int] = {}
    for p in broker_pos:
        sym = p.get("tradingsymbol") or p.get("symbolname") or ""
        try:
            net_qty_map[sym] = int(p.get("netqty", 0))
        except Exception:
            net_qty_map[sym] = 0

    closed_syms = []
    with _lock:
        still_open = []
        for t in _open_trades:
            broker_sym = t.get("symbol", "")
            net_qty    = net_qty_map.get(broker_sym)
            # If broker has the symbol with netqty=0, or symbol absent entirely → manually closed
            if net_qty is not None and net_qty == 0:
                t["status"]      = "CLOSED"
                t["exit_time"]   = datetime.now().isoformat()
                t["exit_px"]     = 0
                t["exit_reason"] = "MANUAL_CLOSE (broker=0)"
                t["pnl"]         = 0
                _closed_trades.append(t)
                closed_syms.append(broker_sym)
                _log(f"AUTO-CLOSED {broker_sym}: broker netqty=0, removing from tracking")
            else:
                still_open.append(t)
        _open_trades = still_open
    if closed_syms:
        _save_state()
    return closed_syms


def manual_close_trade(trade_id: str, exit_px: float = 0) -> bool:
    """Mark a tracked trade as manually closed (no broker call — just clears tracking)."""
    global _open_trades, _closed_trades
    with _lock:
        for t in list(_open_trades):
            if t.get("id") == trade_id:
                t["status"]      = "CLOSED"
                t["exit_time"]   = datetime.now().isoformat()
                t["exit_px"]     = exit_px
                t["exit_reason"] = "MANUAL_CLOSE"
                t["pnl"]         = round((exit_px - (t.get("entry_px") or 0)) * t.get("qty_remaining", 0), 2)
                _closed_trades.append(t)
                _open_trades.remove(t)
                _log(f"MANUAL CLOSE: {t['symbol']} id={trade_id} exit_px={exit_px}")
                _save_state()
                return True
    return False


# ── Live order placement ────────────────────────────────────────────────────────
def _place_live_order(symbol: str, qty: int, side: str,
                      exchange: str = "BSE", sym_token: str = "",
                      product_type: str = "CARRYFORWARD") -> str | None:
    """Place order on Angel One. Returns order ID string on success, None on failure.
    product_type: CARRYFORWARD=NRML (normal F&O), INTRADAY=MIS (auto sq-off at 3:20/11:30)
    """
    global _session_obj
    if _session_obj is None:
        _log("ERROR: Angel session not initialised — call login() first")
        return None
    product = product_type
    try:
        params = {
            "variety"        : "NORMAL",
            "tradingsymbol"  : symbol,
            "symboltoken"    : sym_token,
            "transactiontype": side,
            "exchange"       : exchange,
            "ordertype"      : "MARKET",
            "producttype"    : product,
            "duration"       : "DAY",
            "quantity"       : str(qty),
        }
        resp = _session_obj.placeOrder(params)
        # smartapi-python returns order ID string directly on success,
        # or a dict with status/message on failure
        if isinstance(resp, str) and resp.strip():
            order_id = resp.strip()   # may be alphanumeric (e.g. AMO: "061630a8198eAO")
            _log(f"LIVE ORDER OK [{side} {symbol} qty={qty} exch={exchange}] orderid={order_id}")
            return order_id
        elif isinstance(resp, dict):
            order_id = (resp.get("data") or {}).get("orderid") or resp.get("orderid")
            if order_id:
                _log(f"LIVE ORDER OK [{side} {symbol} qty={qty}] orderid={order_id}")
                return order_id
            else:
                _log(f"LIVE ORDER REJECTED [{side} {symbol}]: {resp.get('message')} "
                     f"code={resp.get('errorcode') or resp.get('errorCode')}")
                return None
        else:
            _log(f"LIVE ORDER UNEXPECTED RESPONSE [{side} {symbol}]: {resp}")
            return None
    except Exception as e:
        _log(f"LIVE ORDER ERROR [{symbol}]: {e}")
        return None


# ── Daily summary ───────────────────────────────────────────────────────────────
def daily_summary() -> str:
    closed = get_closed_trades()
    if not closed:
        return "No trades today."
    wins  = [t for t in closed if (t["pnl"] or 0) > 0]
    losses = [t for t in closed if (t["pnl"] or 0) < 0]
    total = sum(t["pnl"] or 0 for t in closed)
    lines = [
        f"{'='*50}",
        f"{'LIVE' if not PAPER_MODE else 'PAPER'} TRADE SUMMARY — {date.today()}",
        f"Total: {len(closed)}  Wins: {len(wins)}  Losses: {len(losses)}",
        f"P&L: {'+'if total>=0 else ''}{total:,.0f}",
        f"{'='*50}",
    ]
    for t in closed:
        sign = "+" if (t["pnl"] or 0) >= 0 else ""
        lines.append(
            f"  {t['side']} {t['strike']}  "
            f"entry={t['entry_px']}  exit={t['exit_px']}  "
            f"{t['exit_reason']}  P&L {sign}{t['pnl']:,.0f}"
        )
    return "\n".join(lines)
