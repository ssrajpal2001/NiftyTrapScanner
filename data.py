"""
data.py — all Upstox API fetch + resample helpers
No Streamlit imports — pure data layer, safe to import from any module.
"""

import requests
import pandas as pd
from datetime import datetime, timedelta, date
from config import (UPSTOX_BASE, SESSION_START, SESSION_END,
                    MCX_SESSION_START, MCX_SESSION_END)


def url_encode(key: str) -> str:
    return key.replace("|", "%7C").replace(" ", "%20")


SPOT_KEYS = {
    "Nifty":  "NSE_INDEX%7CNifty%2050",
    "Sensex": "BSE_INDEX%7CSENSEX",
}

OPTION_CHAIN_KEYS = {
    "Nifty":  "NSE_INDEX%7CNifty%2050",
    "Sensex": "BSE_INDEX%7CSENSEX",
}

# MCX Crude Oil front-month futures key (updated monthly near expiry)
MCX_CRUDE_FUTURES_KEY = "MCX_FO|499095"   # Jun 18, 2026 — roll over to Jul when expired


def fetch_spot_prev_day(headers: dict, index: str = "Nifty") -> dict:
    """
    Returns the last COMPLETED trading day's OHLC for the given index.
    Picks the most recent candle strictly before today — handles holidays/weekends.
    """
    spot_key  = SPOT_KEYS.get(index, SPOT_KEYS["Nifty"])
    today_str = datetime.today().strftime("%Y-%m-%d")
    from_str  = (datetime.today() - timedelta(days=14)).strftime("%Y-%m-%d")
    url = (f"{UPSTOX_BASE}/historical-candle/"
           f"{spot_key}/day/{today_str}/{from_str}")
    r    = requests.get(url, headers=headers, timeout=15)
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Spot API error: {body}")
    candles    = list(reversed(body["data"]["candles"]))
    today_date = datetime.today().date()
    prev_row   = None
    for c in reversed(candles):
        if datetime.strptime(c[0][:10], "%Y-%m-%d").date() < today_date:
            prev_row = c
            break
    if prev_row is None:
        prev_row = candles[-1]
    return {
        "date" : prev_row[0][:10],
        "open" : round(prev_row[1], 2),
        "high" : round(prev_row[2], 2),
        "low"  : round(prev_row[3], 2),
        "close": round(prev_row[4], 2),
    }


def fetch_spot_for_date(target_date: str, headers: dict, index: str = "Nifty") -> dict | None:
    """
    Returns the prev trading day's OHLC for the given index relative to target_date.
    target_date: 'YYYY-MM-DD'
    """
    spot_key = SPOT_KEYS.get(index, SPOT_KEYS["Nifty"])
    td   = datetime.strptime(target_date, "%Y-%m-%d")
    from_str = (td - timedelta(days=14)).strftime("%Y-%m-%d")
    url  = (f"{UPSTOX_BASE}/historical-candle/"
            f"{spot_key}/day/{target_date}/{from_str}")
    r    = requests.get(url, headers=headers, timeout=15)
    body = r.json()
    if body.get("status") != "success":
        return None
    candles = body["data"]["candles"]
    if not candles:
        return None
    candles = list(reversed(candles))
    target_d = td.date()
    prev_row = None
    for c in reversed(candles):
        if datetime.strptime(c[0][:10], "%Y-%m-%d").date() < target_d:
            prev_row = c
            break
    if prev_row is None:
        return None
    return {
        "date" : prev_row[0][:10],
        "open" : round(prev_row[1], 2),
        "high" : round(prev_row[2], 2),
        "low"  : round(prev_row[3], 2),
        "close": round(prev_row[4], 2),
    }


def get_option_expiries(index_key: str, headers: dict) -> list:
    """Return available option expiry dates for an index key (sorted asc)."""
    encoded = url_encode(index_key)
    url  = f"{UPSTOX_BASE}/option/contract?instrument_key={encoded}"
    r    = requests.get(url, headers=headers, timeout=15)
    body = r.json()
    if body.get("status") != "success":
        return []
    return sorted({item.get("expiry") for item in body.get("data", [])
                   if item.get("expiry")})


def get_instrument_key(strike: int, opt_type: str, expiry_str: str,
                       headers: dict, index: str = "Nifty") -> tuple:
    """Returns (instrument_key, error_or_None)."""
    chain_key = OPTION_CHAIN_KEYS.get(index, OPTION_CHAIN_KEYS["Nifty"])
    url  = (f"{UPSTOX_BASE}/option/chain"
            f"?instrument_key={chain_key}&expiry_date={expiry_str}")
    r    = requests.get(url, headers=headers, timeout=15)
    body = r.json()
    if body.get("status") != "success":
        return None, body
    found_strikes = []
    for item in body.get("data", []):
        for side in ["call_options", "put_options"]:
            opt = item.get(side, {})
            mkt = opt.get("market_data", {})
            key = opt.get("instrument_key") or mkt.get("instrument_key")
            s   = item.get("strike_price") or opt.get("strike_price")
            t   = "CE" if side == "call_options" else "PE"
            if s is not None:
                found_strikes.append(f"{int(float(s))}{t}")
            if key and s is not None and int(float(s)) == strike and t == opt_type:
                return key, None
    sample = found_strikes[:10]
    return None, {"msg": f"Strike {strike}{opt_type} not found. Chain had: {sample}"}


def fetch_1min(instrument_key: str, from_date: str, to_date: str,
               headers: dict) -> tuple:
    """Returns (DataFrame of 1-min OHLCV, error_or_None)."""
    encoded = url_encode(instrument_key)
    url     = (f"{UPSTOX_BASE}/historical-candle/"
               f"{encoded}/1minute/{to_date}/{from_date}")
    r    = requests.get(url, headers=headers, timeout=20)
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


def resample_tf(df_1min: pd.DataFrame, minutes: int,
                session_start: str | None = None,
                session_end: str | None = None) -> pd.DataFrame:
    """
    Resample 1-min bars to any N-min timeframe, session-aware.
    Equity default: 09:15–15:29.  MCX: pass session_start="09:00", session_end="23:29".
    """
    if df_1min.empty:
        return pd.DataFrame()
    ss = session_start or SESSION_START
    se = session_end   or SESSION_END
    bars = []
    for day, day_df in df_1min.groupby(df_1min.index.date):
        d = day_df.between_time(ss, se)
        if d.empty:
            continue
        tz     = d.index.tz
        origin = pd.Timestamp(f"{day} {ss}:00", tz=tz)
        r = d.resample(f"{minutes}min", origin=origin).agg(
            open  =("open",  "first"),
            high  =("high",  "max"),
            low   =("low",   "min"),
            close =("close", "last"),
            volume=("vol",   "sum"),
        ).dropna(subset=["open"])
        bars.append(r)
    if not bars:
        return pd.DataFrame()
    out = pd.concat(bars)
    out.index.name = "datetime"
    return out.reset_index()


def fetch_index_ltp(headers: dict, index: str = "Nifty") -> float | None:
    """
    Fetch current index spot LTP via Upstox market-quote/ltp API.
    Works for both NSE (Nifty) and BSE (Sensex) indices.
    More reliable than 1-min historical for indices during live session.
    """
    key = "NSE_INDEX|Nifty 50" if index == "Nifty" else "BSE_INDEX|SENSEX"
    encoded = key.replace("|", "%7C").replace(" ", "%20")
    url  = f"{UPSTOX_BASE}/market-quote/ltp?instrument_key={encoded}"
    try:
        r    = requests.get(url, headers=headers, timeout=10)
        body = r.json()
        if body.get("status") != "success":
            return None
        data = body.get("data", {})
        # Key format in response: "NSE_INDEX:Nifty 50" or "BSE_INDEX:SENSEX"
        for v in data.values():
            ltp = v.get("last_price")
            if ltp:
                return round(float(ltp), 2)
    except Exception:
        pass
    return None


def fetch_today_open(headers: dict, index: str = "Nifty") -> float | None:
    """
    Returns today's session open price for the index.
    Uses market-quote OHLC API (works for both NSE and BSE indices).
    Falls back to 1-min bar for NSE if OHLC API unavailable.
    """
    key     = "NSE_INDEX|Nifty 50" if index == "Nifty" else "BSE_INDEX|SENSEX"
    encoded = key.replace("|", "%7C").replace(" ", "%20")
    url     = f"{UPSTOX_BASE}/market-quote/ohlc?instrument_key={encoded}&interval=1d"
    try:
        r    = requests.get(url, headers=headers, timeout=10)
        body = r.json()
        if body.get("status") == "success":
            for v in body.get("data", {}).values():
                ohlc = v.get("ohlc") or {}
                if ohlc.get("open"):
                    return round(float(ohlc["open"]), 2)
    except Exception:
        pass
    # Fallback: 1-min bar (works for NSE, may fail for BSE)
    raw_key = "NSE_INDEX|Nifty 50" if index == "Nifty" else "BSE_INDEX|SENSEX"
    today   = datetime.today().strftime("%Y-%m-%d")
    df, err = fetch_1min(raw_key, today, today, headers)
    if err or df is None or df.empty:
        return None
    return float(df["open"].iloc[0])


def fetch_1min_intraday(instrument_key: str, headers: dict) -> tuple:
    """
    Fetch today's intraday 1-min bars via Upstox intraday endpoint.
    Returns (DataFrame, error_or_None).
    Endpoint: GET /v2/historical-candle/intraday/{key}/1minute
    — returns today's live candles including the current incomplete bar.
    """
    encoded = url_encode(instrument_key)
    url     = f"{UPSTOX_BASE}/historical-candle/intraday/{encoded}/1minute"
    try:
        r    = requests.get(url, headers=headers, timeout=20)
        body = r.json()
    except Exception as exc:
        return None, str(exc)
    if body.get("status") != "success":
        return None, body
    candles = body.get("data", {}).get("candles", [])
    if not candles:
        return pd.DataFrame(), None
    candles = list(reversed(candles))
    df = pd.DataFrame(candles, columns=["ts","open","high","low","close","vol","oi"])
    df["dt"] = pd.to_datetime(df["ts"])
    df = df.set_index("dt")
    df[["open","high","low","close"]] = df[["open","high","low","close"]].round(2)
    return df[["open","high","low","close","vol"]], None


def fetch_spot_bars(from_date: str, to_date: str, headers: dict,
                    index: str = "Nifty", minutes: int = 5) -> pd.DataFrame:
    """
    Fetch Nifty / Sensex spot 1-min data and resample to `minutes` timeframe.
    Returns DataFrame with columns: datetime, open, high, low, close, volume.
    Uses the same resample_tf() pipeline as option contracts.
    """
    raw_key = "NSE_INDEX|Nifty 50" if index == "Nifty" else "BSE_INDEX|SENSEX"
    df_1min, err = fetch_1min(raw_key, from_date, to_date, headers)
    if err or df_1min is None or df_1min.empty:
        return pd.DataFrame()
    return resample_tf(df_1min, minutes)


def pivot_levels(high: float, low: float, close: float) -> dict:
    """Standard floor pivot levels from prev-day HLC."""
    pivot = (high + low + close) / 3
    r1    = (pivot * 2) - low
    s1    = (pivot * 2) - high
    r2    = pivot + (high - low)
    s2    = pivot - (high - low)
    return {"pivot": pivot, "r1": r1, "r2": r2, "s1": s1, "s2": s2}


# ── MCX Crude Oil helpers ──────────────────────────────────────────────────────

def get_mcx_active_futures(headers: dict) -> tuple:
    """
    Return (instrument_key, expiry_date_str) for the nearest MCX CrudeOil futures.
    Falls back to MCX_CRUDE_FUTURES_KEY if API search fails.
    """
    url = f"{UPSTOX_BASE}/instruments/search?query=CRUDEOIL&exchange=MCX_FO"
    try:
        r    = requests.get(url, headers=headers, timeout=10)
        body = r.json()
        if body.get("status") == "success":
            futures = [
                item for item in body.get("data", [])
                if item.get("segment") == "MCX_FO"
                and item.get("instrument_type") == "FUT"
            ]
            futures.sort(key=lambda x: x.get("expiry", "9999"))
            if futures:
                f = futures[0]
                return f["instrument_key"], f["expiry"]
    except Exception:
        pass
    return MCX_CRUDE_FUTURES_KEY, ""


def get_mcx_option_chain(futures_key: str, headers: dict) -> dict:
    """
    Build {strike: {"CE": key, "PE": key}} map for MCX CrudeOil options.
    Uses /option/contract endpoint (chain API doesn't support MCX).
    Returns dict keyed by integer strike.
    """
    enc  = url_encode(futures_key)
    url  = f"{UPSTOX_BASE}/option/contract?instrument_key={enc}"
    try:
        r    = requests.get(url, headers=headers, timeout=15)
        body = r.json()
    except Exception:
        return {}
    if body.get("status") != "success":
        return {}

    chain: dict = {}
    for item in body.get("data", []):
        sym  = item.get("trading_symbol", "")
        key  = item.get("instrument_key", "")
        # Symbol format: "CRUDEOIL 7400 CE 16 JUN 26"
        parts = sym.split()
        if len(parts) >= 3:
            try:
                strike = int(float(parts[1]))
                otype  = parts[2]           # "CE" or "PE"
                if otype in ("CE", "PE"):
                    chain.setdefault(strike, {})[otype] = key
            except (ValueError, IndexError):
                pass
    return chain


def get_mcx_instrument_key(strike: int, opt_type: str,
                            futures_key: str, headers: dict) -> tuple:
    """
    MCX-specific instrument key lookup using contract list (chain API unavailable).
    Returns (key, error_or_None).
    """
    chain = get_mcx_option_chain(futures_key, headers)
    entry = chain.get(strike, {})
    key   = entry.get(opt_type)
    if key:
        return key, None
    sample = sorted(chain.keys())[:8]
    return None, {"msg": f"MCX {strike}{opt_type} not found. Available strikes: {sample}"}


def get_mcx_option_expiries(futures_key: str, headers: dict) -> list:
    """Return sorted list of available MCX option expiry date strings."""
    chain = get_mcx_option_chain(futures_key, headers)
    # The contract list doesn't separate by expiry via API; expiry is embedded in symbol.
    # We parse it from the trading_symbol field.
    enc  = url_encode(futures_key)
    url  = f"{UPSTOX_BASE}/option/contract?instrument_key={enc}"
    try:
        r    = requests.get(url, headers=headers, timeout=15)
        body = r.json()
    except Exception:
        return []
    expiries = set()
    for item in body.get("data", []):
        exp = item.get("expiry")
        if exp:
            expiries.add(exp)
    return sorted(expiries)


def fetch_mcx_prev_day(headers: dict, futures_key: str = MCX_CRUDE_FUTURES_KEY) -> dict:
    """
    Returns previous trading day OHLC for MCX CrudeOil futures.
    Uses daily historical endpoint with the futures instrument key.
    """
    today_str = datetime.today().strftime("%Y-%m-%d")
    from_str  = (datetime.today() - timedelta(days=14)).strftime("%Y-%m-%d")
    encoded   = url_encode(futures_key)
    url = f"{UPSTOX_BASE}/historical-candle/{encoded}/day/{today_str}/{from_str}"
    r    = requests.get(url, headers=headers, timeout=15)
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"MCX daily candle error: {body}")
    candles    = list(reversed(body["data"]["candles"]))
    today_date = datetime.today().date()
    prev_row   = None
    for c in reversed(candles):
        if datetime.strptime(c[0][:10], "%Y-%m-%d").date() < today_date:
            prev_row = c
            break
    if prev_row is None and candles:
        prev_row = candles[-1]
    if prev_row is None:
        raise RuntimeError("MCX: no prev-day candle found")
    return {
        "date" : prev_row[0][:10],
        "open" : round(prev_row[1], 2),
        "high" : round(prev_row[2], 2),
        "low"  : round(prev_row[3], 2),
        "close": round(prev_row[4], 2),
    }


def fetch_mcx_today_open(headers: dict, futures_key: str = MCX_CRUDE_FUTURES_KEY) -> float | None:
    """Returns today's 09:00 open price for MCX CrudeOil futures."""
    today = datetime.today().strftime("%Y-%m-%d")
    encoded = url_encode(futures_key)
    url = f"{UPSTOX_BASE}/historical-candle/intraday/{encoded}/1minute"
    try:
        r    = requests.get(url, headers=headers, timeout=15)
        body = r.json()
        candles = body.get("data", {}).get("candles", []) if isinstance(body.get("data"), dict) else []
        if candles:
            return round(float(list(reversed(candles))[0][1]), 2)
    except Exception:
        pass
    return None
