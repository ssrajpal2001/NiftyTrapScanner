"""
config.py — shared constants for Nifty Trap Scanner
"""

# ── Trade sizing ───────────────────────────────────────────────────────────────
DEFAULT_QTY      = 1
DEFAULT_LOT_SIZE = 65
DEFAULT_UNITS    = DEFAULT_QTY * DEFAULT_LOT_SIZE   # 65

# ── SL buffer (points below Zone LOW) ─────────────────────────────────────────
DEFAULT_SL_BUFFER = 2.0

# ── Timeframes ─────────────────────────────────────────────────────────────────
HTF_MINUTES = 75
LTF_MINUTES = 5

# ── Session window ─────────────────────────────────────────────────────────────
SESSION_START = "09:15"
SESSION_END   = "15:29"
SQUAREOFF_TIME = "15:30"   # equity intraday hard close

# ── MCX (commodity) session ────────────────────────────────────────────────────
MCX_SESSION_START  = "09:00"
MCX_SESSION_END    = "23:29"
MCX_SQUAREOFF_TIME = "23:30"

# ── Upstox API base ────────────────────────────────────────────────────────────
UPSTOX_BASE = "https://api.upstox.com/v2"

# ── White theme colours ────────────────────────────────────────────────────────
CLR_GREEN  = "#1B5E20"
CLR_RED    = "#B71C1C"
CLR_BLUE   = "#1565C0"
CLR_ORANGE = "#E65100"
CLR_MUTED  = "#6B7280"
CLR_BORDER = "#DDE1E7"
CLR_BG     = "#FFFFFF"
CLR_CARD   = "#F5F7FA"
