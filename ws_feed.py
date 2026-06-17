"""
ws_feed.py — Upstox V3 WebSocket feed + real-time candle aggregator.

Uses asyncio + websockets (Upstox's recommended library for v3 feed).
Runs in a dedicated daemon thread with its own event loop.
All shared state is module-level, protected by threading.Lock.

Usage:
    import ws_feed
    ws_feed.start(token, ["NSE_FO|xxx", "BSE_FO|yyy"], hist_map={key: df1})
    ltp  = ws_feed.get_ltp("NSE_FO|xxx")
    df5  = ws_feed.get_candles("NSE_FO|xxx", minutes=5)
    ok   = ws_feed.is_connected()
"""

import asyncio
import threading
import struct
import json
import ssl
import time
import requests
import pandas as pd
from datetime import datetime

from config import UPSTOX_BASE

# ─── Shared state ──────────────────────────────────────────────────────────────
_lock        = threading.Lock()
_ltp:        dict = {}   # key → latest LTP float
_prev_close: dict = {}   # key → prev session close (from first tick's cp field)
_candles_1m: dict = {}   # key → pd.DataFrame(DatetimeIndex='dt', cols=OHLCV)

_token:    str  = ""
_keys:     list = []
_running:  bool = False
_connected:bool = False
_loop:     asyncio.AbstractEventLoop | None = None
_thread:   threading.Thread | None = None
_ws_obj    = None   # live websocket handle (set inside _ws_connect)

# Optional callback fired on every tick: fn(key: str, ltp: float)
_tick_callback = None

def set_tick_callback(fn) -> None:
    global _tick_callback
    _tick_callback = fn

WS_AUTH_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"


# ─── Public API ────────────────────────────────────────────────────────────────

def start(token: str, instrument_keys: list,
          hist_map: dict | None = None) -> None:
    """
    Start WebSocket feed in background thread.
    Safe to call multiple times — only one thread runs.
    hist_map: {instrument_key: DataFrame with DatetimeIndex (1-min OHLCV)}
    """
    global _token, _keys, _running

    with _lock:
        _token = token
        _keys  = list(instrument_keys)
        if hist_map:
            for k, df in hist_map.items():
                if df is not None and not df.empty:
                    df_copy = df.copy()
                    # Strip tz so naive tick timestamps concat cleanly
                    if df_copy.index.tz is not None:
                        df_copy.index = df_copy.index.tz_localize(None)
                    _candles_1m[k] = df_copy

    global _thread
    # If thread is alive, don't start another
    if _thread is not None and _thread.is_alive():
        return

    _thread = threading.Thread(target=_run_event_loop, daemon=True, name="ws-feed")
    _thread.start()
    _running = True


def add_keys(new_keys: list, hist_map: dict | None = None) -> None:
    """
    Append new instrument keys to an already-running WS feed without restarting.
    Seeds hist_map for new keys, then sends a fresh subscription on the live socket.
    If the socket is not yet connected the updated _keys list is used on next connect.
    """
    global _keys
    added = []
    with _lock:
        for k in new_keys:
            if k not in _keys:
                _keys.append(k)
                added.append(k)
        if hist_map:
            for k, df in hist_map.items():
                if df is not None and not df.empty and k not in _candles_1m:
                    df_copy = df.copy()
                    if df_copy.index.tz is not None:
                        df_copy.index = df_copy.index.tz_localize(None)
                    _candles_1m[k] = df_copy
    if added and _loop and _loop.is_running():
        asyncio.run_coroutine_threadsafe(_send_sub_async(), _loop)


async def _send_sub_async() -> None:
    """Re-send subscription message on the live websocket to include new keys."""
    global _ws_obj
    if _ws_obj is not None:
        try:
            await _ws_obj.send(_sub_msg())
        except Exception:
            pass  # next reconnect will subscribe with full _keys list


def stop() -> None:
    global _loop, _running, _connected
    _connected = False
    if _loop and _loop.is_running():
        _loop.call_soon_threadsafe(_loop.stop)
    # Give the loop thread time to exit cleanly
    import time as _t; _t.sleep(0.5)
    _running = False


def is_connected() -> bool:
    return _connected


def get_ltp(key: str) -> float | None:
    with _lock:
        return _ltp.get(key)


def get_prev_close(key: str) -> float | None:
    with _lock:
        return _prev_close.get(key)


def get_candles_1m(key: str) -> pd.DataFrame:
    """Return 1-min OHLCV DataFrame (historical + live ticks merged)."""
    with _lock:
        df = _candles_1m.get(key)
        return df.copy() if df is not None else pd.DataFrame()


def get_candles(key: str, minutes: int) -> pd.DataFrame:
    """Resample to N-min bars. Returns empty DataFrame if no data."""
    from data import resample_tf
    df1 = get_candles_1m(key)
    if df1.empty:
        return pd.DataFrame()
    return df1 if minutes == 1 else resample_tf(df1, minutes)


# ─── WebSocket helpers ─────────────────────────────────────────────────────────

def _get_ws_url() -> str | None:
    h = {"Authorization": f"Bearer {_token}", "Accept": "application/json"}
    try:
        r    = requests.get(WS_AUTH_URL, headers=h, timeout=10)
        body = r.json()
        if body.get("status") == "success":
            return body["data"].get("authorizedRedirectUri") or \
                   body["data"].get("authorized_redirect_uri")
    except Exception:
        pass
    return None


def _sub_msg() -> bytes:
    return json.dumps({
        "guid"  : "trap-scanner-ws",
        "method": "sub",
        "data"  : {"mode": "ltpc", "instrumentKeys": _keys},
    }).encode()


# ─── Minimal protobuf parser (Upstox LTPC FeedResponse v3) ────────────────────
# FeedResponse { field1(type):varint  field2(feeds):map<str,Feed> }
# Feed         { field1(ltpc):LTPC }
# LTPC         { field1(ltp):double  field2(ltt):varint  field4(cp):double }

def _varint(buf, p):
    v = sh = 0
    while p < len(buf):
        b = buf[p]; p += 1
        v |= (b & 0x7F) << sh
        if not (b & 0x80):
            break
        sh += 7
    return v, p


def _skipbytes(buf, p):
    ln, p = _varint(buf, p)
    return buf[p: p + ln], p + ln


def _parse_ltpc(buf: bytes) -> dict:
    out = {}; p = 0
    while p < len(buf):
        tag, p = _varint(buf, p)
        f = tag >> 3; w = tag & 7
        if w == 1:
            if p + 8 > len(buf): break
            val = struct.unpack_from("<d", buf, p)[0]; p += 8
            if   f == 1: out["ltp"] = round(val, 2)
            elif f == 4: out["cp"]  = round(val, 2)
        elif w == 2: _, p = _skipbytes(buf, p)
        elif w == 0: _, p = _varint(buf, p)
        elif w == 5: p += 4
        else:        break
    return out


def _parse_feed(buf: bytes) -> dict:
    """Feed → ltpc dict. Field 1 = LTPC sub-message in v3."""
    p = 0
    while p < len(buf):
        tag, p = _varint(buf, p)
        f = tag >> 3; w = tag & 7
        if w == 2:
            chunk, p = _skipbytes(buf, p)
            if f == 1:          # LTPC is field 1 inside Feed in v3
                return _parse_ltpc(chunk)
        elif w == 1: p += 8
        elif w == 0: _, p = _varint(buf, p)
        elif w == 5: p += 4
        else:        break
    return {}


def _parse_map_entry(buf: bytes) -> tuple:
    key = None; feed = {}; p = 0
    while p < len(buf):
        tag, p = _varint(buf, p)
        f = tag >> 3; w = tag & 7
        if w == 2:
            chunk, p = _skipbytes(buf, p)
            if   f == 1: key  = chunk.decode("utf-8", errors="ignore")
            elif f == 2: feed = _parse_feed(chunk)
        elif w == 1: p += 8
        elif w == 0: _, p = _varint(buf, p)
        elif w == 5: p += 4
        else:        break
    return key, feed


def _parse_response(data: bytes) -> dict:
    """FeedResponse → {instrument_key: {ltp, cp}}."""
    result = {}; p = 0; n = len(data)
    while p < n:
        tag, p = _varint(data, p)
        f = tag >> 3; w = tag & 7
        if w == 2:
            chunk, p = _skipbytes(data, p)
            if f == 2:          # feeds map entry
                k, feed = _parse_map_entry(chunk)
                if k and feed:
                    result[k] = feed
        elif w == 1: p += 8
        elif w == 0: _, p = _varint(data, p)
        elif w == 5: p += 4
        else:        break
    return result


# ─── Candle aggregation ────────────────────────────────────────────────────────

def _update_candle(key: str, ts: datetime, price: float) -> None:
    """Upsert current 1-min candle. Caller must hold _lock."""
    bar_ts = pd.Timestamp(ts.replace(second=0, microsecond=0))

    if key not in _candles_1m or _candles_1m[key].empty:
        _candles_1m[key] = pd.DataFrame(
            [{"open": price, "high": price, "low": price,
              "close": price, "vol": 1}],
            index=pd.DatetimeIndex([bar_ts], name="dt"),
        )
        return

    df  = _candles_1m[key]
    idx = df.index[-1]
    if idx == bar_ts:
        df.at[idx, "high"]  = max(df.at[idx, "high"], price)
        df.at[idx, "low"]   = min(df.at[idx, "low"],  price)
        df.at[idx, "close"] = price
        df.at[idx, "vol"]  += 1
    else:
        new = pd.DataFrame(
            [{"open": price, "high": price, "low": price,
              "close": price, "vol": 1}],
            index=pd.DatetimeIndex([bar_ts], name="dt"),
        )
        _candles_1m[key] = pd.concat([df, new])


def _process_tick(raw: bytes) -> None:
    """Parse protobuf tick, update _ltp and candle aggregator."""
    feeds = {}
    try:
        feeds = _parse_response(raw)
    except Exception:
        pass
    if not feeds:
        return
    now = datetime.now()
    with _lock:
        for key, d in feeds.items():
            ltp = d.get("ltp")
            cp  = d.get("cp")
            if ltp:
                _ltp[key] = ltp
                _update_candle(key, now, ltp)
                if _tick_callback:
                    try:
                        _tick_callback(key, ltp)
                    except Exception:
                        pass
            if cp and cp > 0 and key not in _prev_close:
                _prev_close[key] = cp


# ─── Async WebSocket loop ──────────────────────────────────────────────────────

async def _ws_connect() -> None:
    """Single WebSocket session. Raises on disconnect — caller retries."""
    global _connected, _ws_obj
    import websockets

    url = _get_ws_url()
    if not url:
        raise ConnectionError("Could not get WS auth URL")

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode    = ssl.CERT_NONE

    async with websockets.connect(url, ssl=ssl_ctx, ping_interval=20,
                                   ping_timeout=10) as ws:
        _ws_obj    = ws
        _connected = True
        await asyncio.sleep(0.3)
        await ws.send(_sub_msg())

        async for raw in ws:
            data = raw if isinstance(raw, bytes) else raw.encode()
            _process_tick(data)

    _ws_obj    = None
    _connected = False


async def _ws_loop_async() -> None:
    """Reconnect loop with exponential backoff."""
    delay = 3
    while True:
        try:
            await _ws_connect()
            delay = 3          # reset backoff on clean disconnect
        except Exception:
            pass
        finally:
            global _connected
            _connected = False
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)   # backoff: 3 → 6 → 12 → 24 → 60s max


def _run_event_loop() -> None:
    """Entry point for the daemon thread — creates and runs the event loop."""
    global _loop, _running, _connected
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_ws_loop_async())
    finally:
        _loop.close()
        _running   = False
        _connected = False
