"""
data.py — all Upstox API fetch + resample helpers
No Streamlit imports — pure data layer, safe to import from any module.
"""

import requests
import pandas as pd
from datetime import datetime, timedelta, date
from config import UPSTOX_BASE, SESSION_START, SESSION_END


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


def resample_tf(df_1min: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Resample 1-min bars to any N-min timeframe, session-aware (09:15–15:29)."""
    if df_1min.empty:
        return pd.DataFrame()
    bars = []
    for day, day_df in df_1min.groupby(df_1min.index.date):
        d = day_df.between_time(SESSION_START, SESSION_END)
        if d.empty:
            continue
        tz     = d.index.tz
        origin = pd.Timestamp(f"{day} {SESSION_START}:00", tz=tz)
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


def pivot_levels(high: float, low: float, close: float) -> dict:
    """Standard floor pivot levels from prev-day HLC."""
    pivot = (high + low + close) / 3
    r1    = (pivot * 2) - low
    s1    = (pivot * 2) - high
    r2    = pivot + (high - low)
    s2    = pivot - (high - low)
    return {"pivot": pivot, "r1": r1, "r2": r2, "s1": s1, "s2": s2}
