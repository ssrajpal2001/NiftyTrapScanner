# CLAUDE.md — NiftyTrapScanner / Live Trap Tracker
## Context Document for AI Sessions

> Read this file at the start of every session before writing any code.

---

## 1. What This Project Is

A **live options trading system** built in Python/Streamlit that:
1. Detects institutional traps (bears trapped / bulls trapped) on HTF bars
2. Cascades into 5-min LTF scan → entry signal
3. Places real options orders on **Angel One SmartAPI** (BFO for Sensex, NFO for Nifty, MCX for CrudeOil)
4. Monitors open positions via WebSocket (Upstox) for SL / T1 / trailing SL

**Currently trading: Sensex (live, Angel One). Nifty paper. CrudeOil planned.**

---

## 2. Architecture — Frozen Design

| Script | HTF Scan On | LTF Scan On | Trade In |
|--------|------------|------------|---------|
| Nifty | Option bars (75-min) | Option bars (5-min) | Option |
| Sensex | Option bars (75-min) | Option bars (5-min) | Option |
| CrudeOil | **Futures bars** (30-min) | Futures bars (5-min) | Option |

Rationale: Nifty/Sensex — institutions move via options. CrudeOil — institutions move via futures (confirmed by backtest: futures bars give 89% win rate vs options bars failing).

---

## 3. Trap Logic (Core Algorithm)

### Entry Rules
| Signal | Condition | Entry Level | SL Level |
|--------|-----------|-------------|----------|
| BEARISH entry | Current LOW < Previous LOW | Current LOW | Current HIGH |
| BULLISH entry | Current HIGH > Previous HIGH | Current HIGH | Current LOW |

### Trap / Close Rules
| Event | Condition |
|-------|-----------|
| BEARS TRAPPED | Future candle HIGH > Bear's SL (= bear's entry HIGH) |
| BULLS TRAPPED | Future candle LOW < Bull's SL (= bull's entry LOW) |
| BEARISH CLOSED | After trapped: future LOW ≤ Bear's Entry Level |
| BULLISH CLOSED | After trapped: future HIGH ≥ Bull's Entry Level |

### Trade Signal
| Trapped | You Trade | Entry | SL |
|---------|-----------|-------|-----|
| BEARS TRAPPED | BUY CE | Bear's SL (trap level = zone_high) | Bear's Entry (zone_low) |
| BULLS TRAPPED | BUY PE | Bull's SL (trap level = zone_low) | Bull's Entry (zone_high) |

---

## 4. File Structure

```
D:\AlgoSoft\TrapScanner\
├── live_tracker.py       # Main live trading app (Streamlit)
├── angel_orders.py       # Angel One SmartAPI — order placement, SL/T1/trail
├── ws_feed.py            # Upstox WebSocket — live 1-min candle aggregator
├── data.py               # Upstox REST fetch, resample, option chain
├── scanner.py            # scan_htf(), scan_ltf(), scan_htf_spot()
├── config.py             # MCX session times, constants
├── phase2_ui.py          # Backtest / analysis UI (not used for live trading)
├── trap_ui.py            # Original daily candle scanner (legacy)
├── requirements.txt
├── .env                  # Credentials — NEVER commit
├── deploy/
│   ├── hetzner_setup.sh  # One-shot Hetzner Ubuntu 24.04 server setup
│   └── update_token.sh   # Daily Upstox token rotation
└── CLAUDE.md
```

---

## 5. Trading Time Gates (implemented in live_tracker.py)

| Script | Entry Window | Entry Cutoff | SQ OFF | Notes |
|--------|-------------|-------------|--------|-------|
| Nifty | None (morning hold) | 13:45 | 14:00 | Backtest: morning hold > windows |
| Sensex | None (morning hold) | 13:45 | 14:00 | Backtest: morning hold > windows |
| CrudeOil | W2 only: 18:45–19:15 | 22:45 | 23:00 | W1 EIA (8% WR) disabled; W2 US open 100% WR |

Configured in `INDEX_CONFIG` per script via `entry_cutoff`, `entry_windows`, `sq_off_time`.

---

## 6. Key Functions

### live_tracker.py
| Function | What it does |
|----------|-------------|
| `morning_scan(strike, opt_type, expiry_api, htf_min, token, idx_name)` | HTF scan on option 1-min bars (Nifty/Sensex). Returns `(open_traps, key, df1, from_date, dbg)` |
| `morning_scan_mcx(strike, opt_type, expiry_api, token)` | HTF scan on FUTURES 1-min bars (CrudeOil). Returns same tuple |
| `live_ltf_scan(open_traps, df1, ...)` | Every 2s: scans 5-min LTF inside HTF zones, fires entry + Angel One order. Checks time window/cutoff. 1-ITM strike if toggled. |
| `live_intraday_scan(...)` | Gap-day fallback: 15-min HTF → 5-min LTF cascade |

### angel_orders.py
| Function | What it does |
|----------|-------------|
| `login()` | Angel One SmartAPI login (uses TOTP auto-generated from secret) |
| `log_entry(side, spot_ltp, expiry, qty, sl, target, ...)` | Places MARKET BUY, tracks trade |
| `check_exits(tracked_prices)` | SL hit → SELL. T1 hit → half SELL + trail mode |
| `sensex_symbol(strike, side, expiry)` | `SENSEX{YY}{M}{DD}{STRIKE}{CE/PE}` — M has NO leading zero |
| `crudeoil_symbol(strike, side, expiry)` | `CRUDEOIL{DD}{MON}{YY}{STRIKE}{CE/PE}` |

### data.py
| Function | What it does |
|----------|-------------|
| `fetch_1min(key, from_date, to_date, headers)` | Returns `(df, error)` — 1-min bars |
| `fetch_1min_intraday(key, headers)` | Today's intraday bars only |
| `resample_tf(df, minutes, session_start, session_end)` | Resamples 1-min to any TF |
| `get_mcx_option_chain(futures_key, headers)` | Returns `{strike: {"CE": key, "PE": key}}` |

---

## 7. API Details

### Upstox V2
```
GET https://api.upstox.com/v2/historical-candle/{key}/minute/{to}/{from}
GET https://api.upstox.com/v2/historical-candle/intraday/{key}/minute
Headers: Authorization: Bearer {TOKEN}, Accept: application/json
Instrument keys: "NSE_INDEX|Nifty 50", "BSE_INDEX|SENSEX", "MCX_FO|..."
```

### Angel One SmartAPI
- Exchange: `BFO` (Sensex), `NFO` (Nifty), `MCX` (CrudeOil)
- Product: `CARRYFORWARD` (NRML) or `INTRADAY` (MIS)
- Variety: `NORMAL` (also works for AMO after market hours — returns alphanumeric order ID like `06167fa3f28dAO`)
- `searchScrip(exchange, symbol)` → returns `symboltoken` — must be fetched before `placeOrder`
- `cancelOrder(variety, order_id)` — returns empty body on success (library throws JSON parse error — ignore it, check order book to confirm)

---

## 8. Symbol Formats

### Sensex (BFO)
`SENSEX{YY}{M}{DD}{STRIKE}{CE/PE}`
- M = month number with NO leading zero (June = 6, not 06)
- Expiry = Thursday weekly
- Example: `SENSEX2661876800CE` = Jun 18, 2026 (Thursday), strike 76800

### Nifty (NFO)
`NIFTY{YY}{MON}{DD}{STRIKE}{CE/PE}`
- MON = 3-letter uppercase month name
- Expiry = Tuesday weekly
- Example: `NIFTY25JUN2423500CE`

### CrudeOil (MCX)
`CRUDEOIL{DD}{MON}{YY}{STRIKE}{CE/PE}`
- Example: `CRUDEOIL16JUL267350CE`

---

## 9. Lot Sizes
| Script | Lot Size | Default Qty | Total Units |
|--------|----------|-------------|-------------|
| Nifty | 75 | 2 lots | 150 |
| Sensex | 20 | 2 lots | 40 |
| CrudeOil | 100 | 2 lots | 200 |

---

## 10. Strike Selection Strategy

- **Prev-day Pivot levels** (S1/S2 for CE, R1/R2 for PE) — standard mode
- **Gap day** (gap > 1%): use ATM ± ITM-near/ITM-far instead
- **1-ITM toggle** in sidebar: CE order uses `strike - step`, PE uses `strike + step` for higher delta
- HTF scan still runs on the selected strike; only the live order uses 1-ITM strike

---

## 11. Credentials & Security

- All secrets in `.env` only — **never hardcode, never commit**
- `.env` entries: `UPSTOX_TOKEN`, `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PASSWORD`, `ANGEL_TOTP_SECRET`
- `ANGEL_TOTP_SECRET` = static base32 string (TOTP code is auto-generated via `pyotp`)
- Upstox token expires daily — must refresh before 09:00 AM each trading day
- `PAPER_MODE = False` in `angel_orders.py` — live orders active for Sensex
- Only last 12 chars of token shown in UI

---

## 12. Running Locally (Windows)

```powershell
cd D:\AlgoSoft\TrapScanner
# No venv — packages installed system-wide (Python 3.14)
streamlit run live_tracker.py
# Opens at http://localhost:8501
```

---

## 13. Hetzner Server Deployment

**Repo:** `https://github.com/ssrajpal2001/NiftyTrapScanner`
**Branch:** `phase2/ltf-entry-engine`

```bash
# On fresh Hetzner CX22 (Ubuntu 24.04), run once:
curl -fsSL https://raw.githubusercontent.com/ssrajpal2001/NiftyTrapScanner/phase2/ltf-entry-engine/deploy/hetzner_setup.sh | bash

# Fill credentials:
nano /opt/trapscanner/.env

# Start:
systemctl start trapscanner

# Daily token update (before 09:00 AM):
bash /opt/trapscanner/deploy/update_token.sh YOUR_NEW_TOKEN

# Logs:
journalctl -u trapscanner -f

# URL:
http://YOUR_SERVER_IP:8501
```

---

## 14. Backtest Results (Jun 2026, 7 trading days)

| Script | Mode | Win Rate | Real Option P&L |
|--------|------|----------|----------------|
| Nifty | Morning hold 09:15–13:45 | 71% | ~₹48,000 |
| Sensex | Morning hold 09:15–13:45 | 80% | ~₹34,000 |
| CrudeOil | W2 window 18:45–19:15 | 100% | ₹1,31,740 |

Lot sizes used: Nifty 75×2=150, Sensex 20×2=40, CrudeOil 100×2=200.
1-ITM CE consistently outperforms ATM CE for CrudeOil.

---

## 15. Git

**Repo:** `https://github.com/ssrajpal2001/NiftyTrapScanner`
**Active branch:** `phase2/ltf-entry-engine`
**Main branch:** `master`

```powershell
git checkout phase2/ltf-entry-engine
git pull origin phase2/ltf-entry-engine
streamlit run live_tracker.py
```

---

## 16. Bugs Fixed (Jun 17 2026 live session) — CRITICAL for integration

These were discovered during live Sensex trading. Any integration of this logic MUST apply these fixes:

### A. tracked_sym must be Upstox instrument key, not Angel One symbol
`log_entry(tracked_sym=...)` must receive the **Upstox BSE_FO|numeric_token** key, NOT the Angel One trading symbol (e.g. `SENSEX18JUN2676600CE`). `ws_feed.get_ltp()` only knows Upstox keys — passing the AO symbol gives LTP=0, breaking all SL/T1 monitoring.

### B. 1-ITM strike = spot ATM ± 1 step (not pivot ± 1 step)
When 1-ITM is enabled, compute from **current live spot LTP**:
```python
atm = round(spot_ltp / strike_step) * strike_step
exec_strike = atm - strike_step  # CE: 1 step ITM below ATM
exec_strike = atm + strike_step  # PE: 1 step ITM above ATM
```
Previous incorrect behaviour: `exec_strike = pivot_strike - step` → put orders 600+ pts ITM.

### C. LTF CLOSED entries must be today's date only
In `live_ltf_scan`, filter `closed_ltf` to entries where `closed_on.date() == today`. Without this filter, a June 16 LTF entry (e.g. entry=58.85 when CE was at ₹58) reappears as a live buy signal on June 17 when CE is at ₹540.

### D. option_daily_atr() was always returning 0
`resample_tf()` returns `reset_index()` — datetime is a **column** named `"datetime"`, not the index. Reading `df.index.date` gives RangeIndex dates, producing 0 ATR. Fix:
```python
df_htf["_date"] = pd.to_datetime(df_htf["datetime"]).dt.date
```
ATR=0 made `_zone_far_threshold=0` (falsy) → zone_too_far check never ran → cascade never triggered.

### E. Stale deep-history zones block cascade
Old HTF zones from weeks ago (e.g. CE at ₹30–100 when current LTP is ₹540) kept `has_fresh_75m=True`, preventing cascade even when today's zones were unreachable. Fix: filter `today_traps` to zones where `zone_trigger >= current_ltp * 0.4`.

### F. In-memory stale trades persist across day boundary
`_open_trades` in `angel_orders.py` is in-memory. On a new trading day without app restart, previous day's trades remain. Fix: `get_open_trades()` now auto-purges entries where `entry_time.date() != today`.

### G. SQ OFF time for Sensex is 15:25, NOT 14:00
Sensex (BSE) closes at 15:30. SQ OFF must be 15:25. The old value of 14:00 in `entry_cutoff` was cutting off trades 1.75 hours early.

### H. Simulation vs real orders — two separate sections
`"Today's Phase 3 Trades"` = scenario C backtest simulation on today's bars. NOT real orders.
`"Angel One Trades"` = real broker orders only. Never confuse the two.

---

## 17. Pending Work

- [ ] **Hetzner server setup** — CX22, curl setup script, fill .env, start service
- [ ] **Regenerate Angel One credentials** — API key + TOTP secret were shared in chat; must regenerate before live trading
- [ ] **CrudeOil live trading** — currently paper only; enable after Sensex is stable
- [ ] **Nifty live trading** — currently paper only
- [ ] **Auto Upstox token refresh** — currently manual daily
- [ ] **SQ OFF auto-trigger** — `square_off_all()` exists but not wired to scheduled call at 15:25 Sensex / 23:00 MCX
- [ ] **Angel One order cancel helper** — `cancelOrder` returns empty body (success) but SmartAPI lib throws JSON parse error; handle gracefully
- [ ] **Test Nifty symbol format** — confirmed Sensex BFO works; Nifty NFO not yet tested live
- [ ] **Entry cutoff in live_ltf_scan** — check `entry_cutoff` time before firing (currently only checked in time-window gate, not for SENSEX 15:20)
