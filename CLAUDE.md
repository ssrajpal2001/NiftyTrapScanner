# CLAUDE.md — Nifty Trap Scanner
## Context Document for AI Sessions

> Read this file at the start of every session before writing any code.

---

## 1. What This Project Is

**Nifty Trap Scanner** is a focused Streamlit app that:
1. Fetches Nifty 50 **daily candles** from Upstox V2 REST API
2. Scans candles to find **institutional traps** (bears trapped / bulls trapped)
3. Displays results in a table with OPEN / CLOSED status
4. Shows a **candlestick chart drilldown** for any selected trap — with Trap level, Entry, Stop Loss marked as horizontal lines

**Single file:** `trap_ui.py` — everything is in one Streamlit file.

---

## 2. Trap Logic (Core Algorithm)

### Entry Rules (who entered the market)
| Signal | Condition | Entry Level | SL Level |
|--------|-----------|-------------|----------|
| BEARISH entry | Current candle LOW < Previous candle LOW | Current candle LOW (where bears shorted) | Current candle HIGH |
| BULLISH entry | Current candle HIGH > Previous candle HIGH | Current candle HIGH (where bulls bought) | Current candle LOW |

### Trap Rules (who got squeezed)
| Trap | Condition |
|------|-----------|
| BEARS TRAPPED | Any future candle HIGH > SL Level of the bear entry |
| BULLS TRAPPED | Any future candle LOW < SL Level of the bull entry |

### Close Rules (0-loss exit for institution)
| Close | Condition |
|-------|-----------|
| BEARISH CLOSED | After TRAPPED, any future candle LOW ≤ Entry Level |
| BULLISH CLOSED | After TRAPPED, any future candle HIGH ≥ Entry Level |

### Trade Signal (what YOU trade)
| Who got trapped | Your trade | Your Entry | Your SL |
|-----------------|------------|-----------|---------|
| BEARS TRAPPED | BULLISH (BUY) | = Bear's SL Level (trap level) | = Bear's Entry Level |
| BULLS TRAPPED | BEARISH (SELL) | = Bull's SL Level (trap level) | = Bull's Entry Level |

---

## 3. File Structure

```
D:\AlgoSoft\TrapScanner\
├── trap_ui.py                      # ONLY file — complete Streamlit app
├── requirements.txt                # streamlit, pandas, plotly, requests
├── .gitignore                      # excludes .venv, __pycache__, secrets.toml
├── .streamlit/
│   └── secrets.toml.example        # template — copy to secrets.toml, add token
└── CLAUDE.md                       # this file
```

---

## 4. Key Functions in trap_ui.py

| Function | What it does |
|----------|-------------|
| `fetch_candles(from_date, to_date)` | Calls Upstox V2 API, returns DataFrame with date/open/high/low/close/candle columns. Cached 5 min. |
| `scan(df)` | Iterates candles, returns `(df_events, all_entries)`. `df_events` = table of TRAPPED/CLOSED events. `all_entries` = all trap candidates including still-ACTIVE. |
| `build_trap_chart(df_candles, trap_row, context_bars)` | Returns Plotly Figure. Candlestick + horizontal lines for Trap/Entry/SL + vertical lines for Trap Date and Close Date. |

---

## 5. API Details

**Upstox V2 Historical Candle:**
```
GET https://api.upstox.com/v2/historical-candle/NSE_INDEX%7CNifty%2050/day/{to_date}/{from_date}
Headers: Authorization: Bearer {TOKEN}, Accept: application/json
Response: body["data"]["candles"] — list of [ts, open, high, low, close, vol, oi]
Order: DESCENDING (newest first) — code reverses to get ascending
```

**Token:** Daily bearer token from Upstox. Hardcoded as fallback in `trap_ui.py` but should be set in `.streamlit/secrets.toml` as `UPSTOX_TOKEN`.

---

## 6. UI Layout (what exists)

1. **Sidebar** — Data range selector (4/8/13/26/52 weeks), Who Got Trapped filter, Status filter, context bars slider, Refresh button
2. **Metrics row** — Total Traps, Closed, Still Open, Bears Trapped, Bulls Trapped
3. **ALL TRAP EVENTS table** — colour-coded (green = bears trapped, red = bulls trapped), Status orange=OPEN / grey=CLOSED
4. **Chart Drilldown section** — dropdown to select trap → "Show Chart" button → Plotly candlestick with levels + summary card
5. **Currently Open Traps table** — only TRAPPED (not yet closed) entries
6. **Raw Candle Data** — expandable section

---

## 7. Colour Palette

| Usage | Hex |
|-------|-----|
| Canvas background | `#0D1117` |
| Card / component background | `#161B22` |
| CE / Bullish / Green | `#238636` |
| PE / Bearish / Red | `#DA3633` |
| Active zone / Blue | `#58A6FF` |
| Muted text | `#8B949E` |
| Borders | `#30363D` |
| Alert / Trap orange | `#f0a500` |

---

## 8. How to Run

```powershell
cd D:\AlgoSoft\TrapScanner
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Add your Upstox token:
copy .streamlit\secrets.toml.example .streamlit\secrets.toml
# Edit secrets.toml and paste token

streamlit run trap_ui.py
# Opens at http://localhost:8501
```

---

## 9. What Was Completed (as of Jun 2026)

- [x] Fetch Nifty 50 daily candles from Upstox V2
- [x] Scan algorithm: BEAR/BULL entry detection, trap detection, close detection
- [x] Metrics row (total/open/closed/bears/bulls)
- [x] Trap events table with colour coding and filters
- [x] Currently Open Traps table
- [x] **Candlestick chart drilldown with Trap / Entry / SL levels** ← completed this session
- [x] Status badge (OPEN 🟠 / CLOSED ⚪) on chart title
- [x] Summary card below chart (Who Trapped → Trade Signal → Entry → SL → Status)

---

## 10. Pending / Phase 2 Ideas

Things NOT yet built — add here as you decide:

- [ ] Weekly candle view (option to switch between daily / weekly)
- [ ] Option strike suggestions based on trap level (ITM distance matrix)
- [ ] Alert when price approaches an OPEN trap's Entry Level
- [ ] Export trap table to CSV
- [ ] Auto token refresh (currently must paste new token daily)
- [ ] Historical accuracy stats (% of traps that closed vs still open)

---

## 11. Git

**Repo:** `https://github.com/ssrajpal2001/NiftyTrapScanner`
**Branch:** `master`

```powershell
# Office setup (first time)
git clone https://github.com/ssrajpal2001/NiftyTrapScanner D:\AlgoSoft\TrapScanner
cd D:\AlgoSoft\TrapScanner
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run trap_ui.py
```
