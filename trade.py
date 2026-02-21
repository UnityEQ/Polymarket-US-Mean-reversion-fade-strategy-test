#!/usr/bin/env python3
"""
trade.py - v15.0 - POLYMARKET US API - FADE + TREND STRATEGIES
CHANGES FROM v14.5:
  - Entry orders now use IOC (Immediate-Or-Cancel) instead of GTC. GTC
    orders were sitting unfilled on the book because limit prices didn't
    cross the spread. IOC fills against available liquidity or cancels
    immediately — correct for a FADE strategy that needs instant entry.
  - Fixed portfolio endpoint: /v1/portfolio/positions does NOT support
    ?market= query param. Now fetches all positions and looks up by slug.
  - Both entry and close fallback orders now use IOC.
"""

import csv
import os
import sys
import time
import math
import signal
import json
import requests
import traceback
import gc
import threading
import atexit
import logging
import base64
import hashlib
from collections import deque
from functools import wraps
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    HAS_ED25519 = True
except ImportError:
    HAS_ED25519 = False

_original_dumps = json.dumps

def patched_dumps(obj, **kwargs):
    effective_kwargs = {
        'sort_keys': True,
        'ensure_ascii': False,
        **kwargs
    }
    if 'separators' not in kwargs:
        effective_kwargs['separators'] = (',', ':')
    return _original_dumps(obj, **effective_kwargs)

json.dumps = patched_dumps

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import Dict, Optional, List, Tuple, Any, Set

try:
    from web3 import Web3
    HAS_WEB3 = True
except ImportError:
    HAS_WEB3 = False

HAS_CLOB = False

# -------------------- Console Log Tee --------------------
class _Tee:
    """Write to both original stream and a file."""
    def __init__(self, original, log_file):
        self._original = original
        self._log_file = log_file
    def write(self, data):
        self._original.write(data)
        self._log_file.write(data)
        self._log_file.flush()
    def flush(self):
        self._original.flush()
        self._log_file.flush()
    def fileno(self):
        return self._original.fileno()

_log_file = open("trade-console-log.txt", "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _log_file)
sys.stderr = _Tee(sys.__stderr__, _log_file)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# =========================
# Configuration
# =========================

LIVE = os.getenv("LIVE", "false").lower() == "true"
PAPER = not LIVE

# Risk / exits
TP_PCT = 0.10
SL_PCT = 0.04
TIME_EXIT_SEC_PRIMARY = 720
BREAKEVEN_EXIT_SEC = 480
BREAKEVEN_TOLERANCE = 0.015
TRAILING_ACTIVATE_PCT = 0.04
TRAILING_STOP_PCT = 0.025

# Opening filters
Z_OPEN = 3.5
Z_OPEN_OUTLIER = 4.5
MAX_CONCURRENT_POS = 2
MIN_REARM_SEC = 300
MIN_CASH_PER_TRADE = 1.0

# Signal quality filters (anti-warmup-noise)
MIN_DELTA_PCT = 0.015
MAX_DELTA_PCT = 0.15
MIN_OPEN_INTERVAL_SEC = 30
MAX_SIGNAL_AGE_SEC = 15

# Quality filters
MIN_MID_PRICE = 0.25
MAX_MID_PRICE = 0.55
MAX_SPREAD_BASE = 0.10
MAX_SPREAD_MID = 0.13
MAX_SPREAD_HIGH = 0.16
MIN_VOLUME = 10  # Match monitor V24_MIN — volume is now openInterest proxy, not dollar volume

MAX_LOSSES_PER_MARKET = 2
FADE_ONLY_TRIGGERS = True
ENABLE_TRAILING_STOP = True
SELL_BIAS = False
BLOCK_PRE_GAME = True         # Block all pre-game entries (consistent losers)
ALLOW_UNKNOWN_PHASE = True    # Allow entries when phase can't be determined

# TREND strategy (momentum following) — trades WITH directional moves during live games
ENABLE_TREND = True
TREND_TP_PCT = 0.12           # Wider — let momentum run
TREND_SL_PCT = 0.05           # Moderate — room for brief pullbacks
TREND_TIME_EXIT_SEC = 480     # 8 min — momentum fades fast
TREND_BREAKEVEN_EXIT_SEC = 240  # 4 min
TREND_BREAKEVEN_TOLERANCE = 0.01
TREND_TRAILING_ACTIVATE_PCT = 0.035
TREND_TRAILING_STOP_PCT = 0.02
TREND_Z_OPEN = 3.5
TREND_Z_OPEN_OUTLIER = 4.5

MARKET_BLOCKLIST = {
    "106290179211046540747289269936667551104628138202727334337396394027502415762364",
}

PAPER_FEE_RATE = 0.005
LIVE_FEE_RATE = 0.005

SIZED_CASH_FRAC = 0.10
SIZED_CASH_MIN = 1.0
SIZED_CASH_MAX = 10.0

SLIPPAGE_TOLERANCE_PCT = 3.0  # Must exceed half-spread to cross for IOC fills (0.5% was too tight)
CROSS_BUFFER = 0.005  # $0.005 beyond best price to ensure crossing the book
ORDER_TIMEOUT_SEC = 15.0
ORDER_POLL_INTERVAL_SEC = 0.5
FILL_POLL_ATTEMPTS = 10
FILL_POLL_DELAY_SEC = 1.0
BALANCE_SYNC_INTERVAL_SEC = 60.0
CLOSE_RETRY_ATTEMPTS = 3
CLOSE_RETRY_DELAY_SEC = 2.0

RATE_LIMIT_CALLS = 40
RATE_LIMIT_WINDOW_SEC = 1.0

PM_US_API_KEY_ID = os.getenv("POLYMARKET_KEY_ID")
PM_US_API_SECRET = os.getenv("POLYMARKET_SECRET_KEY")
PM_US_BASE_URL = os.getenv("PM_US_BASE_URL", "https://api.polymarket.us")

class PMUSEnums:
    BUY_LONG = "ORDER_INTENT_BUY_LONG"
    SELL_LONG = "ORDER_INTENT_SELL_LONG"
    BUY_SHORT = "ORDER_INTENT_BUY_SHORT"
    SELL_SHORT = "ORDER_INTENT_SELL_SHORT"
    LIMIT = "ORDER_TYPE_LIMIT"
    MARKET = "ORDER_TYPE_MARKET"
    GTC = "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    IOC = "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    FOK = "TIME_IN_FORCE_FILL_OR_KILL"
    SIDE_BUY = "ORDER_SIDE_BUY"
    SIDE_SELL = "ORDER_SIDE_SELL"
    AUTOMATIC = "MANUAL_ORDER_INDICATOR_AUTOMATIC"
    STATE_FILLED = "ORDER_STATE_FILLED"
    STATE_PARTIALLY_FILLED = "ORDER_STATE_PARTIALLY_FILLED"
    STATE_CANCELED = "ORDER_STATE_CANCELED"
    STATE_REJECTED = "ORDER_STATE_REJECTED"
    STATE_EXPIRED = "ORDER_STATE_EXPIRED"
    STATE_PENDING_NEW = "ORDER_STATE_PENDING_NEW"
    EXEC_FILL = "EXECUTION_TYPE_FILL"
    EXEC_PARTIAL_FILL = "EXECUTION_TYPE_PARTIAL_FILL"

MIDS_JSON_PATH = "poly_mids_latest.json"
MIDS_MAX_AGE_SEC = 30.0
CSV_MIDS_MAX_AGE_SEC = 1200.0

STATUS_EVERY_SEC = 5.0
SUMMARY_EVERY_SEC = 10.0
CLEANUP_EVERY_SEC = 60

MAX_REARM_ENTRIES = 1000
REARM_TTL_SEC = 3600
MAX_LATEST_MIDS = 500
LATEST_MIDS_TTL_SEC = 120
MAX_TAILER_LINES = 100

TRIGGERS_CSV = os.getenv("TRIGGERS_CSV", f"poly_us_triggers_{date.today().isoformat()}.csv")
OUTLIERS_CSV = os.getenv("OUTLIERS_CSV", f"poly_us_outliers_{date.today().isoformat()}.csv")
TRADES_CSV = os.getenv("TRADES_CSV", f"poly_us_trades_{date.today().isoformat()}.csv")

DEBUG_REJECTIONS = os.getenv("DEBUG_REJECTIONS", "false").lower() == "true"

# =========================
# Rate Limiter
# =========================

class RateLimiter:
    def __init__(self, max_calls: int = RATE_LIMIT_CALLS, window_sec: float = RATE_LIMIT_WINDOW_SEC):
        self._lock = threading.Lock()
        self._calls: deque = deque()
        self._max_calls = max_calls
        self._window = window_sec
    
    def wait(self):
        with self._lock:
            now = time.time()
            while self._calls and self._calls[0] < now - self._window:
                self._calls.popleft()
            if len(self._calls) >= self._max_calls:
                sleep_time = self._calls[0] + self._window - now + 0.01
                if sleep_time > 0:
                    time.sleep(sleep_time)
                return self.wait()
            self._calls.append(now)

rate_limiter = RateLimiter()

def rate_limited(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        rate_limiter.wait()
        return func(*args, **kwargs)
    return wrapper

# =========================
# Global Session
# =========================

def create_session() -> requests.Session:
    sess = requests.Session()
    retries = Retry(total=3, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET", "POST", "DELETE"])
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess

http_session = create_session()

# =========================
# Helpers
# =========================

def now() -> float:
    return time.time()

def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def get_memory_mb() -> float:
    if HAS_PSUTIL:
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    return 0.0

def to_float(x, default: float = float('nan')) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if s in ("", "nan", "NaN"):
            return default
        return float(s)
    except Exception:
        return default

def to_upper(s) -> str:
    return str(s or "").strip().upper()

def ensure_file_exists(path: str):
    if not os.path.exists(path):
        with open(path, "a", newline="", encoding="utf-8") as _:
            pass

def safe_open(path: str, mode: str):
    return open(path, mode, newline="", encoding="utf-8", errors="replace")

def load_latest_mids(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            j = json.load(f)
            if isinstance(j, dict):
                if len(j) > MAX_LATEST_MIDS:
                    sorted_items = sorted(j.items(), key=lambda x: x[1].get('ts', 0) if isinstance(x[1], dict) else 0, reverse=True)
                    return dict(sorted_items[:MAX_LATEST_MIDS])
                return j
    except Exception:
        pass
    return {}

# =========================
# Trades CSV
# =========================

TRADE_FIELDS = ["ts", "event", "slug", "side", "qty", "entry_mid", "exit_mid", "pnl", "cash_after", "reason", "fee", "z_score", "strategy"]

_trades_header_written = False

def ensure_trades_header():
    """Truncate trades CSV and write fresh header (once per run)."""
    global _trades_header_written
    if _trades_header_written:
        return
    with safe_open(TRADES_CSV, "w") as f:
        csv.writer(f).writerow(TRADE_FIELDS)
    _trades_header_written = True

def append_trade(row: dict):
    ensure_trades_header()
    with safe_open(TRADES_CSV, "a") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        complete = {k: row.get(k, "") for k in TRADE_FIELDS}
        w.writerow(complete)

# =========================
# Rearm tracking
# =========================

class RearmTracker:
    def __init__(self, max_entries: int = MAX_REARM_ENTRIES, ttl_sec: float = REARM_TTL_SEC):
        self._lock = threading.Lock()
        self._data: Dict[str, float] = {}
        self._max_entries = max_entries
        self._ttl_sec = ttl_sec
        self._last_cleanup = time.time()
    
    def can_rearm(self, tid: str) -> bool:
        with self._lock:
            self._maybe_cleanup()
            return (now() - self._data.get(tid, 0.0)) >= MIN_REARM_SEC
    
    def touch(self, tid: str):
        with self._lock:
            self._data[tid] = now()
    
    def _maybe_cleanup(self):
        current = time.time()
        if current - self._last_cleanup < 60:
            return
        self._last_cleanup = current
        cutoff = current - self._ttl_sec
        expired = [tid for tid, ts in self._data.items() if ts < cutoff]
        for tid in expired:
            del self._data[tid]
        if len(self._data) > self._max_entries:
            sorted_items = sorted(self._data.items(), key=lambda x: x[1])
            for tid, _ in sorted_items[:len(self._data) - self._max_entries]:
                del self._data[tid]
    
    def force_cleanup(self):
        with self._lock:
            self._last_cleanup = 0
            self._maybe_cleanup()
    
    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

rearm_tracker = RearmTracker()

# =========================
# Per-Market Loss Tracker
# =========================

class MarketLossTracker:
    def __init__(self, max_losses: int = MAX_LOSSES_PER_MARKET):
        self._lock = threading.Lock()
        self._losses: Dict[str, int] = {}
        self._max_losses = max_losses
    
    def record_loss(self, slug: str):
        with self._lock:
            self._losses[slug] = self._losses.get(slug, 0) + 1
    
    def is_blocked(self, slug: str) -> bool:
        with self._lock:
            return self._losses.get(slug, 0) >= self._max_losses
    
    def get_losses(self, slug: str) -> int:
        with self._lock:
            return self._losses.get(slug, 0)

market_loss_tracker = MarketLossTracker()

# =========================
# Position
# =========================

@dataclass
class Position:
    tid: str
    side: str
    qty: float
    entry_mid: float
    entry_ts: float
    fee_open: float
    cost_basis: float
    order_id: str = ""
    fill_price: float = 0.0
    z_score: float = 0.0
    peak_profit_pct: float = 0.0
    trailing_active: bool = False
    peak_last_updated: float = 0.0
    consecutive_profit_mids: int = 0
    strategy: str = "FADE"

    @property
    def slug(self) -> str:
        return self.tid

# =========================
# Profit/Loss helpers
# =========================

def _calc_profit_pct(side: str, entry: float, current: float) -> float:
    if not (0 < current < 1 and 0 < entry < 1):
        return 0.0
    if side in ("BUY", "BUY_LONG"):
        return (current - entry) / entry
    # BUY_NO/BUY_SHORT: profit is on the NO side, so measure relative to NO cost (1-entry)
    return (entry - current) / (1.0 - entry)

def hit_take_profit(side: str, entry: float, current: float, threshold: float = TP_PCT) -> bool:
    return _calc_profit_pct(side, entry, current) >= threshold

def hit_stop_loss(side: str, entry: float, current: float, threshold: float = SL_PCT) -> bool:
    return _calc_profit_pct(side, entry, current) <= -threshold

def hit_breakeven_exit(side: str, entry: float, current: float, age_sec: float,
                       be_sec: float = BREAKEVEN_EXIT_SEC, tolerance: float = BREAKEVEN_TOLERANCE) -> bool:
    if age_sec < be_sec:
        return False
    return abs(_calc_profit_pct(side, entry, current)) < tolerance

TRAILING_PEAK_DECAY_SEC = 60.0
TRAILING_PEAK_DECAY_RATE = 0.25
TRAILING_MIN_CONSECUTIVE = 2

def check_trailing_stop(pos: Position, current: float, is_stale: bool = False,
                        activate_pct: float = TRAILING_ACTIVATE_PCT,
                        stop_pct: float = TRAILING_STOP_PCT) -> Tuple[bool, float]:
    if not ENABLE_TRAILING_STOP:
        return False, pos.peak_profit_pct

    profit_pct = _calc_profit_pct(pos.side, pos.entry_mid, current)
    current_time = time.time()
    new_peak = pos.peak_profit_pct

    if pos.peak_last_updated > 0 and (current_time - pos.peak_last_updated) > TRAILING_PEAK_DECAY_SEC:
        new_peak = new_peak * (1.0 - TRAILING_PEAK_DECAY_RATE)
        pos.peak_last_updated = current_time

    if not is_stale and profit_pct > new_peak:
        new_peak = profit_pct
        pos.peak_last_updated = current_time

    if not is_stale and profit_pct >= activate_pct:
        pos.consecutive_profit_mids += 1
    elif not is_stale:
        pos.consecutive_profit_mids = 0

    if new_peak >= activate_pct and pos.consecutive_profit_mids >= TRAILING_MIN_CONSECUTIVE:
        if profit_pct <= new_peak - stop_pct:
            return True, new_peak

    return False, new_peak

def get_exit_params(strategy: str) -> dict:
    """Return exit thresholds for the given strategy."""
    if strategy == "TREND":
        return {
            "tp": TREND_TP_PCT, "sl": TREND_SL_PCT,
            "time": TREND_TIME_EXIT_SEC, "be_sec": TREND_BREAKEVEN_EXIT_SEC,
            "be_tol": TREND_BREAKEVEN_TOLERANCE,
            "trail_activate": TREND_TRAILING_ACTIVATE_PCT,
            "trail_stop": TREND_TRAILING_STOP_PCT,
        }
    return {
        "tp": TP_PCT, "sl": SL_PCT,
        "time": TIME_EXIT_SEC_PRIMARY, "be_sec": BREAKEVEN_EXIT_SEC,
        "be_tol": BREAKEVEN_TOLERANCE,
        "trail_activate": TRAILING_ACTIVATE_PCT,
        "trail_stop": TRAILING_STOP_PCT,
    }

# =========================
# PaperBroker
# =========================

@dataclass
class PaperBroker:
    starting_cash: float = 10.0
    cash: float = field(init=False)
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    latest_mids: Dict[str, dict] = field(default_factory=dict)
    csv_mids: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    _last_mids_cleanup: float = field(default=0.0, repr=False)
    _trade_count: int = field(default=0, repr=False)
    _wins: int = field(default=0, repr=False)
    _losses: int = field(default=0, repr=False)
    _tp_count: int = field(default=0, repr=False)
    _sl_count: int = field(default=0, repr=False)
    _time_count: int = field(default=0, repr=False)
    _be_count: int = field(default=0, repr=False)
    _trail_count: int = field(default=0, repr=False)

    def __post_init__(self):
        self.cash = self.starting_cash

    def sized_cash(self) -> float:
        base = max(SIZED_CASH_MIN, min(self.cash * SIZED_CASH_FRAC, SIZED_CASH_MAX))
        return min(base, self.cash)

    def can_afford_trade(self) -> bool:
        return self.sized_cash() >= MIN_CASH_PER_TRADE

    def fee_for_notional(self, notional: float) -> float:
        return abs(notional) * PAPER_FEE_RATE

    def _is_long_yes(self, side: str) -> bool:
        return side in ("BUY", "BUY_LONG")

    def get_unrealized_pnl(self) -> float:
        total = 0.0
        for pos in self.positions.values():
            current_mid = self.get_current_yes_mid(pos)
            if self._is_long_yes(pos.side):
                total += pos.qty * (current_mid - pos.entry_mid)
            else:
                total += pos.qty * (pos.entry_mid - current_mid)
        return total

    def get_locked_capital(self) -> float:
        return sum(pos.cost_basis for pos in self.positions.values())

    def get_equity(self) -> float:
        return self.cash + self.get_locked_capital() + self.get_unrealized_pnl()

    def is_blocked(self, tid: str) -> bool:
        return tid in MARKET_BLOCKLIST or market_loss_tracker.is_blocked(tid)

    def should_open(self, mid: float, z_score: float, z_min: float = Z_OPEN) -> Tuple[bool, str]:
        if mid < MIN_MID_PRICE or mid > MAX_MID_PRICE:
            return False, "price_range"
        if z_score < z_min:
            return False, "z_too_low"
        return True, "ok"

    def open(self, tid: str, side: str, mid: float, z_score: float = 0.0, strategy: str = "FADE") -> Optional[Position]:
        if tid in self.positions or self.is_blocked(tid):
            return None
        z_min = TREND_Z_OPEN if strategy == "TREND" else Z_OPEN
        allowed, _ = self.should_open(mid, z_score, z_min=z_min)
        if not allowed:
            return None
        cash_to_use = self.sized_cash()
        if cash_to_use < MIN_CASH_PER_TRADE or not (0 < mid < 1):
            return None
        qty = cash_to_use / max(mid, 1e-9)
        notional = qty * mid
        fee_open = self.fee_for_notional(notional)
        total_cost = notional + fee_open
        if total_cost > self.cash:
            return None
        self.cash -= total_cost
        pos = Position(
            tid=tid, side=side, qty=qty, entry_mid=mid,
            entry_ts=now(), fee_open=fee_open, cost_basis=total_cost,
            fill_price=mid, z_score=z_score, strategy=strategy
        )
        self.positions[tid] = pos
        self._trade_count += 1
        append_trade({
            "ts": utc_ts(), "event": "OPEN", "slug": tid, "side": side,
            "qty": f"{qty:.4f}", "entry_mid": f"{mid:.6f}", "exit_mid": "",
            "pnl": "", "cash_after": f"{self.cash:.2f}",
            "reason": "", "fee": f"{fee_open:.4f}", "z_score": f"{z_score:.2f}",
            "strategy": strategy
        })
        return pos

    def get_current_yes_mid(self, pos: Position) -> float:
        try:
            rec = self.latest_mids.get(pos.tid)
            if rec:
                mid = float(rec.get("mid"))
                ts = float(rec.get("ts"))
                if 0.0 < mid < 1.0 and time.time() - ts <= MIDS_MAX_AGE_SEC:
                    return mid
        except Exception:
            pass
        try:
            csv_rec = self.csv_mids.get(pos.tid)
            if csv_rec:
                mid, ts = csv_rec
                if 0.0 < mid < 1.0 and time.time() - ts <= CSV_MIDS_MAX_AGE_SEC:
                    return mid
        except Exception:
            pass
        return pos.entry_mid

    def get_executable_exit_price(self, pos: Position) -> float:
        """Paper broker has no bid/ask, fall back to mid."""
        return self.get_current_yes_mid(pos)

    def _is_stale_mid(self, pos: Position, current: float) -> bool:
        if abs(current - pos.entry_mid) < 1e-6:
            csv_rec = self.csv_mids.get(pos.tid)
            if csv_rec and (time.time() - csv_rec[1]) > CSV_MIDS_MAX_AGE_SEC:
                return True
            json_rec = self.latest_mids.get(pos.tid)
            if json_rec and (time.time() - float(json_rec.get("ts", 0))) > MIDS_MAX_AGE_SEC:
                return True
            return True
        return False

    def close(self, tid: str, exit_mid: float, reason: str) -> Optional[Tuple[Position, float]]:
        pos = self.positions.get(tid)
        if not pos or not (0 < exit_mid < 1):
            return None
        exit_notional = pos.qty * exit_mid
        fee_close = self.fee_for_notional(exit_notional)
        if self._is_long_yes(pos.side):
            proceeds = exit_notional - fee_close
            self.cash += proceeds
            pnl_gross = pos.qty * (exit_mid - pos.entry_mid)
            pnl_after_fee = pnl_gross - (pos.fee_open + fee_close)
        else:
            pnl_gross = pos.qty * (pos.entry_mid - exit_mid)
            pnl_after_fee = pnl_gross - (pos.fee_open + fee_close)
            self.cash += pos.cost_basis + pnl_after_fee
        if pnl_after_fee > 0:
            self._wins += 1
        else:
            self._losses += 1
            market_loss_tracker.record_loss(tid)
        if reason == "tp": self._tp_count += 1
        elif reason == "sl": self._sl_count += 1
        elif reason == "time_exit": self._time_count += 1
        elif reason == "breakeven": self._be_count += 1
        elif reason == "trailing_stop": self._trail_count += 1
        self.realized_pnl += pnl_after_fee
        del self.positions[tid]
        self._trade_count += 1
        append_trade({
            "ts": utc_ts(), "event": "CLOSE", "slug": tid, "side": pos.side,
            "qty": f"{pos.qty:.4f}", "entry_mid": f"{pos.entry_mid:.6f}",
            "exit_mid": f"{exit_mid:.6f}", "pnl": f"{pnl_after_fee:.4f}",
            "cash_after": f"{self.cash:.2f}",
            "reason": reason, "fee": f"{(pos.fee_open + fee_close):.4f}",
            "z_score": f"{pos.z_score:.2f}", "strategy": pos.strategy
        })
        return pos, pnl_after_fee

    def cleanup_latest_mids(self):
        current_time = time.time()
        if current_time - self._last_mids_cleanup < 30:
            return
        self._last_mids_cleanup = current_time
        active_tids = set(self.positions.keys())
        cutoff = current_time - LATEST_MIDS_TTL_SEC
        to_remove = [tid for tid, data in self.latest_mids.items() 
                     if tid not in active_tids or (isinstance(data, dict) and data.get('ts', 0) < cutoff)]
        for tid in to_remove:
            del self.latest_mids[tid]

    def cleanup_all(self):
        self.cleanup_latest_mids()
        active_tids = set(self.positions.keys())
        cutoff = time.time() - CSV_MIDS_MAX_AGE_SEC * 2
        to_remove = [tid for tid, (_, ts) in self.csv_mids.items()
                     if tid not in active_tids and ts < cutoff]
        for tid in to_remove:
            del self.csv_mids[tid]

    def get_status_dict(self) -> dict:
        locked = self.get_locked_capital()
        unrealized = self.get_unrealized_pnl()
        equity = self.cash + locked + unrealized
        total = self._wins + self._losses
        win_rate = self._wins / max(1, total) * 100
        return {
            "open": len(self.positions), "cash": self.cash,
            "locked": locked, "unrealized_pnl": unrealized,
            "realized_pnl": self.realized_pnl, "equity": equity,
            "trades": self._trade_count, "wins": self._wins,
            "losses": self._losses, "win_rate": win_rate,
            "tp": self._tp_count, "sl": self._sl_count,
            "time": self._time_count, "be": self._be_count,
            "trail": self._trail_count,
        }

# =========================
# Polymarket US Auth (FIXED)
# =========================

class PolymarketUSAuth:
    def __init__(self, api_key_id: str, api_secret_b64: str):
        if not HAS_ED25519:
            raise ImportError("cryptography package required: pip install cryptography")
        self.api_key_id = api_key_id
        secret_bytes = base64.b64decode(api_secret_b64)
        self._private_key = Ed25519PrivateKey.from_private_bytes(secret_bytes[:32])
    
    def sign_request(self, method: str, path: str, body: str = "") -> dict:
        """Polymarket US signing: timestamp + METHOD + path (body is NOT part of signature)"""
        timestamp = str(int(time.time() * 1000))
        message = timestamp + method.upper() + path
        signature = self._private_key.sign(message.encode("utf-8"))
        return {
            "X-PM-Access-Key": self.api_key_id,
            "X-PM-Timestamp": timestamp,
            "X-PM-Signature": base64.b64encode(signature).decode("utf-8"),
            "Content-Type": "application/json",
        }

# =========================
# LiveBroker (with cancel fix + order ID extraction)
# =========================

@dataclass 
class LiveBroker:
    auth: Any = field(init=False, default=None)
    starting_cash: float = field(init=False, default=0.0)
    cash: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    _price_cache: Dict[str, Tuple[float, float, float, float]] = field(default_factory=dict, repr=False)  # (mid, bid, ask, ts)
    _trade_count: int = field(default=0, repr=False)
    _wins: int = field(default=0, repr=False)
    _losses: int = field(default=0, repr=False)
    _tp_count: int = field(default=0, repr=False)
    _sl_count: int = field(default=0, repr=False)
    _time_count: int = field(default=0, repr=False)
    _be_count: int = field(default=0, repr=False)
    _trail_count: int = field(default=0, repr=False)
    _last_balance_sync: float = field(default=0.0, repr=False)

    def __post_init__(self):
        if not PM_US_API_KEY_ID or not PM_US_API_SECRET:
            raise ValueError("POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY env vars required")
        if not HAS_ED25519:
            raise ImportError("pip install cryptography")
        self.auth = PolymarketUSAuth(PM_US_API_KEY_ID, PM_US_API_SECRET)
        logger.info(f"[LIVE] Polymarket US API initialized (key: {PM_US_API_KEY_ID[:8]}...)")
        self.cash = self._get_account_balance()
        self.starting_cash = self.cash
        logger.info(f"[LIVE] Balance: ${self.cash:.2f}")

    def _extract_order_id(self, resp: Optional[dict]) -> str:
        """Robust order ID extraction - handles Polymarket US response formats."""
        if not resp or not isinstance(resp, dict):
            return ""
        for key in ("orderId", "orderID", "order_id", "id"):
            val = resp.get(key)
            if val:
                return str(val)
        return ""

    def _parse_fill_from_response(self, resp: Optional[dict]) -> Tuple[bool, float]:
        """Parse fill info from order response — checks executions array AND root-level state."""
        if not resp or not isinstance(resp, dict):
            return False, 0.0
        # Check executions array first (synchronousExecution responses)
        executions = resp.get("executions")
        if executions and isinstance(executions, list):
            for ex in executions:
                if not isinstance(ex, dict):
                    continue
                ex_type = str(ex.get("type", "")).upper()
                if ex_type in (PMUSEnums.EXEC_FILL, PMUSEnums.EXEC_PARTIAL_FILL):
                    last_px = ex.get("lastPx")
                    if isinstance(last_px, dict):
                        fill_price = to_float(last_px.get("value"), 0.0)
                    else:
                        fill_price = to_float(last_px, 0.0)
                    if fill_price <= 0:
                        order_obj = ex.get("order", {})
                        if isinstance(order_obj, dict):
                            fill_price = self._extract_avg_price(order_obj)
                    return True, fill_price
        # Check root-level state (order may have state directly in response)
        _FILL_STATES = (PMUSEnums.STATE_FILLED, PMUSEnums.STATE_PARTIALLY_FILLED)
        state = str(resp.get("state") or resp.get("status") or "").upper()
        if state in _FILL_STATES:
            return True, self._extract_avg_price(resp)
        # Check nested order object if present
        order_obj = resp.get("order")
        if isinstance(order_obj, dict):
            state = str(order_obj.get("state") or order_obj.get("status") or "").upper()
            if state in _FILL_STATES:
                return True, self._extract_avg_price(order_obj)
        return False, 0.0

    def _api_get(self, path: str) -> Optional[dict]:
        rate_limiter.wait()
        url = PM_US_BASE_URL + path
        # Sign only the path portion (no query string) per API docs
        sign_path = path.split("?")[0]
        headers = self.auth.sign_request("GET", sign_path)
        try:
            resp = http_session.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"API GET {path} failed: {e}")
            return None

    def _api_post(self, path: str, body: dict) -> Optional[dict]:
        rate_limiter.wait()
        url = PM_US_BASE_URL + path
        body_str = json.dumps(body, separators=(',', ':'), sort_keys=True)
        headers = self.auth.sign_request("POST", path)
        try:
            resp = http_session.post(url, headers=headers, data=body_str, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logger.error(f"API POST {path} HTTP error: {e} | Response: {getattr(e.response, 'text', 'N/A')}")
            return None
        except Exception as e:
            logger.error(f"API POST {path} failed: {e}")
            return None

    def _get_account_balance(self) -> float:
        data = self._api_get("/v1/account/balances")
        if data:
            try:
                balances = data.get("balances", [])
                if balances:
                    bal = balances[0]
                    return to_float(bal.get("buyingPower") or bal.get("currentBalance"), 0.0)
            except Exception as e:
                logger.error(f"Balance parse error: {e}")
        return self.cash

    def sync_balance(self, force: bool = False):
        if not force and time.time() - self._last_balance_sync < BALANCE_SYNC_INTERVAL_SEC:
            return
        self._last_balance_sync = time.time()
        new_cash = self._get_account_balance()
        if new_cash > 0 and abs(new_cash - self.cash) > 0.01:
            logger.info(f"[BALANCE] Synced: ${self.cash:.2f} -> ${new_cash:.2f}")
            self.cash = new_cash

    def sized_cash(self) -> float:
        base = max(SIZED_CASH_MIN, min(self.cash * SIZED_CASH_FRAC, SIZED_CASH_MAX))
        return min(base, self.cash)

    def can_afford_trade(self) -> bool:
        return self.sized_cash() >= MIN_CASH_PER_TRADE

    def fee_for_notional(self, notional: float) -> float:
        return abs(notional) * LIVE_FEE_RATE

    def get_locked_capital(self) -> float:
        return sum(pos.cost_basis for pos in self.positions.values())

    def get_unrealized_pnl(self) -> float:
        total = 0.0
        for pos in self.positions.values():
            current_mid = self.get_current_yes_mid(pos)
            if self._is_long_yes(pos.side):
                total += pos.qty * (current_mid - pos.entry_mid)
            else:
                total += pos.qty * (pos.entry_mid - current_mid)
        return total

    def get_equity(self) -> float:
        return self.cash + self.get_locked_capital() + self.get_unrealized_pnl()

    def is_blocked(self, tid: str) -> bool:
        return tid in MARKET_BLOCKLIST or market_loss_tracker.is_blocked(tid)

    def should_open(self, mid: float, z_score: float, z_min: float = Z_OPEN) -> Tuple[bool, str]:
        if mid < MIN_MID_PRICE or mid > MAX_MID_PRICE:
            return False, "price_range"
        if z_score < z_min:
            return False, "z_too_low"
        return True, "ok"

    def _get_bbo(self, market_slug: str) -> Optional[dict]:
        return self._api_get(f"/v1/markets/{market_slug}/bbo")

    def _get_order_book(self, market_slug: str) -> Optional[dict]:
        return self._api_get(f"/v1/markets/{market_slug}/book")

    def _debug_liquidity(self, market_slug: str, intent: str, order_price: float, qty: float, book: Optional[dict] = None):
        """Log full order book state for debugging IOC failures."""
        if not book:
            book = self._get_order_book(market_slug)
        if not book:
            logger.warning(f"[DEBUG] Could not fetch order book for {market_slug}")
            return
        data = book.get("marketData", book)
        state = data.get("state", "UNKNOWN")
        bids = data.get("bids", [])
        offers = data.get("offers", [])
        logger.info(f"[DEBUG] {market_slug} state={state} book={len(bids)} bids, {len(offers)} offers")
        for i, bid in enumerate(bids[:3]):
            px = bid.get("px", {})
            price = px.get("value", px) if isinstance(px, dict) else px
            logger.info(f"[DEBUG]   BID {i+1}: {price} x {bid.get('qty', '?')}")
        for i, offer in enumerate(offers[:3]):
            px = offer.get("px", {})
            price = px.get("value", px) if isinstance(px, dict) else px
            logger.info(f"[DEBUG]   ASK {i+1}: {price} x {offer.get('qty', '?')}")
        # BUY_LONG/SELL_SHORT cross the ask; BUY_SHORT/SELL_LONG cross the bid
        crosses_ask = intent in (PMUSEnums.BUY_LONG, PMUSEnums.SELL_SHORT)
        crosses_bid = intent in (PMUSEnums.BUY_SHORT, PMUSEnums.SELL_LONG)
        if crosses_ask and offers:
            best_ask_px = offers[0].get("px", {})
            best_ask = to_float(best_ask_px.get("value", best_ask_px) if isinstance(best_ask_px, dict) else best_ask_px, 0.0)
            crosses = order_price >= best_ask
            logger.info(f"[DEBUG] {intent} price={order_price:.4f} vs best_ask {best_ask:.4f} -> {'CROSSES' if crosses else 'NO CROSS'}")
        elif crosses_bid and bids:
            best_bid_px = bids[0].get("px", {})
            best_bid = to_float(best_bid_px.get("value", best_bid_px) if isinstance(best_bid_px, dict) else best_bid_px, 0.0)
            crosses = order_price <= best_bid
            logger.info(f"[DEBUG] {intent} price={order_price:.4f} vs best_bid {best_bid:.4f} -> {'CROSSES' if crosses else 'NO CROSS'}")

    def _extract_book_bbo(self, market_slug: str) -> Tuple[float, float, Optional[dict]]:
        """Fetch order book and extract best bid/ask. Returns (bid, ask, raw_book) or (0, 0, None)."""
        book = self._get_order_book(market_slug)
        if not book:
            # Also try raw BBO endpoint as fallback
            bbo = self._get_bbo(market_slug)
            if bbo:
                try:
                    data = bbo.get("marketDataLite", bbo)
                    bid = to_float(data.get("bestBid") or data.get("best_bid") or data.get("bid"), 0.0)
                    ask = to_float(data.get("bestAsk") or data.get("best_ask") or data.get("ask"), 0.0)
                    if bid > 0 or ask > 0:
                        logger.info(f"[BBO] {market_slug} via /bbo fallback: bid={bid:.4f} ask={ask:.4f}")
                        return bid, ask, None
                except Exception:
                    pass
            return 0.0, 0.0, None
        data = book.get("marketData", book)
        bids = data.get("bids", [])
        offers = data.get("offers", [])
        bid, ask = 0.0, 0.0
        if bids:
            px = bids[0].get("px", {})
            bid = to_float(px.get("value", px) if isinstance(px, dict) else px, 0.0)
        if offers:
            px = offers[0].get("px", {})
            ask = to_float(px.get("value", px) if isinstance(px, dict) else px, 0.0)
        logger.info(f"[BBO] {market_slug} via /book: bid={bid:.4f} ask={ask:.4f} depth={len(bids)}b/{len(offers)}a")
        return bid, ask, book

    def _refresh_price_cache(self, tid: str) -> Tuple[float, float, float]:
        """Fetch book BBO and cache (mid, bid, ask) for 5 seconds. Returns (mid, bid, ask)."""
        cached = self._price_cache.get(tid)
        if cached and time.time() - cached[3] < 5:
            return cached[0], cached[1], cached[2]
        bid, ask, _ = self._extract_book_bbo(tid)
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            if 0 < mid < 1:
                self._price_cache[tid] = (mid, bid, ask, time.time())
                return mid, bid, ask
        if cached:
            return cached[0], cached[1], cached[2]
        return 0.0, 0.0, 0.0

    def get_current_yes_mid(self, pos: Position) -> float:
        mid, _, _ = self._refresh_price_cache(pos.tid)
        return mid if mid > 0 else pos.entry_mid

    def get_executable_exit_price(self, pos: Position) -> float:
        """Return the executable exit price (bid for SELL_LONG, ask for SELL_SHORT).
        Prevents TP from firing on inflated mid when spread is wide/asymmetric."""
        mid, bid, ask = self._refresh_price_cache(pos.tid)
        if self._is_long_yes(pos.side):
            # SELL_LONG crosses the bid
            if bid > 0:
                return bid
        else:
            # SELL_SHORT: YES ask determines NO exit value (higher ask = worse for us)
            if ask > 0:
                return ask
        return mid if mid > 0 else pos.entry_mid

    def _is_stale_mid(self, pos: Position, current: float) -> bool:
        cached = self._price_cache.get(pos.tid)
        if cached and time.time() - cached[3] < 10:
            return False
        return abs(current - pos.entry_mid) < 1e-6

    def _get_order_status(self, order_id: str) -> Optional[dict]:
        resp = self._api_get(f"/v1/order/{order_id}")
        if resp and isinstance(resp, dict):
            # API wraps response in {"order": {...}} — unwrap if present
            return resp.get("order", resp)
        return resp

    def _cancel_order(self, order_id: str, market_slug: str) -> bool:
        """Cancel order with required marketSlug in body."""
        body = {"marketSlug": market_slug}
        resp = self._api_post(f"/v1/order/{order_id}/cancel", body)
        if resp is not None:
            logger.debug(f"Cancel request sent for order {order_id} on {market_slug}")
            return True
        return False

    def _extract_avg_price(self, order: dict) -> float:
        """Extract average fill price from order object. avgPx can be Amount object or scalar."""
        avg_px = order.get("avgPx") or order.get("averagePrice") or order.get("average_price")
        if isinstance(avg_px, dict):
            return to_float(avg_px.get("value"), 0.0)
        return to_float(avg_px, 0.0)

    def _check_position_exists(self, market_slug: str) -> Tuple[bool, float]:
        """Check portfolio for position in this market — confirms fill even when order polling fails."""
        data = self._api_get("/v1/portfolio/positions")
        if not data or not isinstance(data, dict):
            return False, 0.0
        positions = data.get("positions", {})
        # Response is a dict keyed by market slug
        if isinstance(positions, dict):
            pos_data = positions.get(market_slug)
            if pos_data and isinstance(pos_data, dict):
                net_pos = to_float(pos_data.get("netPosition"), 0.0)
                if abs(net_pos) > 0:
                    cost = pos_data.get("cost", {})
                    cost_val = to_float(cost.get("value") if isinstance(cost, dict) else cost, 0.0)
                    avg_price = abs(cost_val / net_pos) if abs(net_pos) > 0 else 0.0
                    logger.info(f"Position confirmed via portfolio: {market_slug} net={net_pos:.4f} avgCost={avg_price:.4f}")
                    return True, avg_price
        # Also handle list format (pagination)
        if isinstance(positions, list):
            for p in positions:
                if not isinstance(p, dict):
                    continue
                meta = p.get("marketMetadata", {})
                slug = meta.get("slug", "") if isinstance(meta, dict) else ""
                if slug == market_slug:
                    net_pos = to_float(p.get("netPosition"), 0.0)
                    if abs(net_pos) > 0:
                        cost = p.get("cost", {})
                        cost_val = to_float(cost.get("value") if isinstance(cost, dict) else cost, 0.0)
                        avg_price = abs(cost_val / net_pos) if abs(net_pos) > 0 else 0.0
                        logger.info(f"Position confirmed via portfolio: {market_slug} net={net_pos:.4f}")
                        return True, avg_price
        return False, 0.0

    def _wait_for_fill(self, order_id: str, market_slug: str, timeout_sec: float = ORDER_TIMEOUT_SEC, is_close: bool = False) -> Tuple[bool, float]:
        start = time.time()
        attempts = 0
        time.sleep(FILL_POLL_DELAY_SEC)  # Initial delay for order propagation
        while time.time() - start < timeout_sec and attempts < FILL_POLL_ATTEMPTS:
            attempts += 1
            # Try order status polling first
            order = self._get_order_status(order_id)
            if order and isinstance(order, dict):
                state = (order.get("state") or order.get("status") or "").upper()
                logger.info(f"Order {order_id[:12]}... poll #{attempts} state={state}")
                if state in (PMUSEnums.STATE_FILLED, PMUSEnums.STATE_PARTIALLY_FILLED):
                    return True, self._extract_avg_price(order)
                elif state in (PMUSEnums.STATE_CANCELED, PMUSEnums.STATE_REJECTED, PMUSEnums.STATE_EXPIRED):
                    logger.info(f"Order {order_id[:12]}... exchange says {state}, no fill")
                    return False, 0.0
            else:
                logger.info(f"Order {order_id[:12]}... poll #{attempts} status=404/None")
            # Every 3rd attempt, also check portfolio as fallback
            if attempts % 3 == 0:
                has_pos, avg_price = self._check_position_exists(market_slug)
                if is_close:
                    # For CLOSE orders: position GONE = close succeeded, position EXISTS = close failed
                    if not has_pos:
                        logger.info(f"Order {order_id[:12]}... close confirmed via portfolio (position gone)")
                        return True, 0.0
                    else:
                        logger.info(f"Order {order_id[:12]}... close NOT confirmed — position still exists")
                else:
                    # For ENTRY orders: position EXISTS = entry filled
                    if has_pos:
                        logger.info(f"Order {order_id[:12]}... fill confirmed via portfolio")
                        return True, avg_price
            time.sleep(FILL_POLL_DELAY_SEC)
        # Final portfolio check before cancelling
        logger.info(f"Order {order_id[:12]}... poll timeout, final portfolio check...")
        has_pos, avg_price = self._check_position_exists(market_slug)
        if is_close:
            if not has_pos:
                logger.info(f"Order {order_id[:12]}... close confirmed via portfolio (position gone) for {market_slug}")
                return True, 0.0
            logger.warning(f"Order {order_id[:12]}... close NOT filled — position still open for {market_slug}")
        else:
            if has_pos:
                logger.info(f"Order {order_id[:12]}... fill confirmed via portfolio for {market_slug}")
                return True, avg_price
        logger.warning(f"Order {order_id[:12]}... no fill detected, cancelling...")
        self._cancel_order(order_id, market_slug)
        return False, 0.0

    def _build_order_body(self, market_slug: str, intent: str, price: float, quantity: float, tif: str = None) -> dict:
        return {
            "marketSlug": market_slug,
            "type": PMUSEnums.LIMIT,
            "intent": intent,
            "price": {"value": f"{price:.3f}", "currency": "USD"},
            "quantity": quantity,
            "tif": tif or PMUSEnums.GTC,
            "manualOrderIndicator": PMUSEnums.AUTOMATIC,
        }

    def _validate_market(self, slug: str, known_mid: float = 0.0) -> bool:
        if 0 < known_mid < 1:
            return True
        bbo = self._get_bbo(slug)
        return bbo is not None

    def _side_to_open_intent(self, side: str) -> str:
        return PMUSEnums.BUY_LONG if side in ("BUY", "BUY_LONG") else PMUSEnums.BUY_SHORT

    def _side_to_close_intent(self, side: str) -> str:
        return PMUSEnums.SELL_LONG if side in ("BUY", "BUY_LONG") else PMUSEnums.SELL_SHORT

    def _is_long_yes(self, side: str) -> bool:
        return side in ("BUY", "BUY_LONG")

    def open(self, tid: str, side: str, mid: float, z_score: float = 0.0, strategy: str = "FADE") -> Optional[Position]:
        if tid in self.positions or self.is_blocked(tid):
            return None
        z_min = TREND_Z_OPEN if strategy == "TREND" else Z_OPEN
        allowed, _ = self.should_open(mid, z_score, z_min=z_min)
        if not allowed:
            return None
        cash_to_use = self.sized_cash()
        if cash_to_use < MIN_CASH_PER_TRADE or not (0 < mid < 1):
            return None
        if not self._validate_market(tid, known_mid=mid):
            return None
        # Fetch real BBO from order book for accurate crossing price
        bbo_bid, bbo_ask, _book_data = self._extract_book_bbo(tid)
        slippage_mult = 1.0 + (SLIPPAGE_TOLERANCE_PCT / 100.0)
        if self._is_long_yes(side):
            # BUY_LONG: bid at/above YES ask to fill
            if bbo_ask > 0:
                order_price = min(bbo_ask + CROSS_BUFFER, 0.999)
            else:
                order_price = min(mid * slippage_mult, 0.999)
            cost_per_share = order_price  # YES cost = price.value
        else:
            # BUY_SHORT: to buy NO, set price.value to YES price
            # price.value <= YES bid means we cross the NO ask
            if bbo_bid > 0:
                order_price = max(bbo_bid - CROSS_BUFFER, 0.001)
            else:
                order_price = max(mid * (1.0 - SLIPPAGE_TOLERANCE_PCT / 100.0), 0.001)
            cost_per_share = 1.0 - order_price  # NO cost = 1 - YES price
        order_price = max(0.001, min(0.999, order_price))
        # Reject if crossing price eats more than half the TP target
        # Compare actual vs ideal cost on the same side
        ideal_cost = mid if self._is_long_yes(side) else (1.0 - mid)
        entry_slippage = abs(cost_per_share - ideal_cost) / max(ideal_cost, 1e-9)
        max_entry_slip = min(TP_PCT / 2.0, 0.03)  # Cap at 3% regardless of TP
        if entry_slippage > max_entry_slip:
            logger.info(f"[SKIP] {tid} entry slippage {entry_slippage:.1%} > {max_entry_slip:.1%}, spread too wide")
            return None
        qty = cash_to_use / max(cost_per_share, 1e-9)
        estimated_notional = qty * cost_per_share
        estimated_fee = self.fee_for_notional(estimated_notional)
        if estimated_notional + estimated_fee > self.cash:
            return None
        intent = self._side_to_open_intent(side)
        order_body = self._build_order_body(tid, intent, order_price, qty, tif=PMUSEnums.IOC)
        bbo_tag = f"bbo={bbo_bid:.3f}/{bbo_ask:.3f}" if (bbo_bid > 0 or bbo_ask > 0) else "bbo=NONE"
        logger.info(f"[ORDER] {intent} {tid} | mid={mid:.4f} {bbo_tag} price.value={order_price:.3f} qty={qty:.2f} cost={cost_per_share:.4f}/sh ${cash_to_use:.2f} IOC")
        # Debug: log full order book state before placing
        self._debug_liquidity(tid, intent, order_price, qty, book=_book_data)
        resp = self._api_post("/v1/orders", order_body)
        if not resp:
            logger.error(f"Order POST returned no response for {tid}")
            return None
        order_id = self._extract_order_id(resp)
        if not order_id:
            logger.error(f"No order ID returned: {resp}")
            return None
        logger.debug(f"Order response for {tid}: {json.dumps(resp, default=str)[:500]}")
        filled, fill_price = self._parse_fill_from_response(resp)
        if filled:
            logger.info(f"Order {order_id[:12]}... filled immediately at {fill_price:.4f}")
        else:
            logger.info(f"Order {order_id[:12]}... placed IOC (keys={list(resp.keys())}), waiting for fill...")
            filled, fill_price = self._wait_for_fill(order_id, tid)
        if not filled:
            logger.warning(f"Order {order_id[:12]}... not filled for {tid}")
            return None
        actual_fill_price = fill_price if fill_price > 0 else mid  # YES-side price
        if self._is_long_yes(side):
            actual_cost_per_share = actual_fill_price
        else:
            actual_cost_per_share = 1.0 - actual_fill_price  # NO cost = 1 - YES price
        actual_notional = qty * actual_cost_per_share
        actual_fee = self.fee_for_notional(actual_notional)
        actual_total_cost = actual_notional + actual_fee
        self.cash -= actual_total_cost
        pos = Position(
            tid=tid, side=side, qty=qty, entry_mid=mid,
            entry_ts=now(), fee_open=actual_fee, cost_basis=actual_total_cost,
            order_id=order_id, fill_price=actual_fill_price, z_score=z_score,
            strategy=strategy
        )
        self.positions[tid] = pos
        self._trade_count += 1
        append_trade({
            "ts": utc_ts(), "event": "OPEN", "slug": tid, "side": side,
            "qty": f"{qty:.4f}", "entry_mid": f"{mid:.6f}", "exit_mid": "",
            "pnl": "", "cash_after": f"{self.cash:.2f}",
            "reason": "", "fee": f"{actual_fee:.4f}", "z_score": f"{z_score:.2f}",
            "strategy": strategy
        })
        self.sync_balance(force=True)
        logger.info(f"[OPEN] {strategy} {side} on {tid} at {actual_fill_price:.4f} (order {order_id[:12]}...)")
        return pos

    def _attempt_close(self, pos: Position, exit_mid: float) -> Tuple[bool, str, float]:
        slug = pos.tid
        slippage_mult = 1.0 + (SLIPPAGE_TOLERANCE_PCT / 100.0)
        intent = self._side_to_close_intent(pos.side)
        # Book-based close pricing (same approach as entry)
        bbo_bid, bbo_ask, _ = self._extract_book_bbo(slug)
        if self._is_long_yes(pos.side):
            # SELL_LONG: must cross the bid → price at best_bid - buffer
            if bbo_bid > 0:
                order_price = max(bbo_bid - CROSS_BUFFER, 0.001)
            else:
                order_price = max(exit_mid / slippage_mult, 0.001)
        else:
            # SELL_SHORT: must cross the ask → price at best_ask + buffer
            if bbo_ask > 0:
                order_price = min(bbo_ask + CROSS_BUFFER, 0.999)
            else:
                no_price = 1.0 - exit_mid
                order_price = min(1.0 - (no_price / slippage_mult), 0.999)
        order_price = max(0.001, min(0.999, order_price))
        # close-position endpoint only supports marketSlug + slippageTolerance (object, not float)
        close_body = {
            "marketSlug": slug,
            "slippageTolerance": {
                "currentPrice": {"value": f"{exit_mid:.4f}", "currency": "USD"},
                "bips": int(SLIPPAGE_TOLERANCE_PCT * 100),  # 3.0% → 300 bips
            },
        }
        resp = self._api_post("/v1/order/close-position", close_body)
        if resp:
            order_id = self._extract_order_id(resp)
            filled, fill_price = self._parse_fill_from_response(resp)
            if filled:
                return True, order_id, fill_price if fill_price > 0 else exit_mid
            if order_id:
                filled, fill_price = self._wait_for_fill(order_id, slug, is_close=True)
                if filled:
                    return True, order_id, fill_price
        # Fallback: explicit limit order with IOC priced to cross the book
        order_body = self._build_order_body(slug, intent, order_price, pos.qty, tif=PMUSEnums.IOC)
        resp = self._api_post("/v1/orders", order_body)
        if not resp:
            return False, "", 0.0
        order_id = self._extract_order_id(resp)
        if not order_id:
            return False, "", 0.0
        filled, fill_price = self._parse_fill_from_response(resp)
        if filled:
            return True, order_id, fill_price if fill_price > 0 else exit_mid
        filled, fill_price = self._wait_for_fill(order_id, slug, is_close=True)
        return filled, order_id, fill_price

    def close(self, tid: str, exit_mid: float, reason: str) -> Optional[Tuple[Position, float]]:
        pos = self.positions.get(tid)
        if not pos or not (0 < exit_mid < 1):
            return None
        filled = False
        order_id = ""
        fill_price = 0.0
        for attempt in range(CLOSE_RETRY_ATTEMPTS):
            filled, order_id, fill_price = self._attempt_close(pos, exit_mid)
            if filled:
                break
            if attempt < CLOSE_RETRY_ATTEMPTS - 1:
                time.sleep(CLOSE_RETRY_DELAY_SEC)
                exit_mid = self.get_current_yes_mid(pos)
        if not filled:
            logger.error(f"All close attempts failed for {tid}")
            return None
        actual_exit_price = fill_price if fill_price > 0 else exit_mid  # YES-side price
        if self._is_long_yes(pos.side):
            exit_notional = pos.qty * actual_exit_price
            fee_close = self.fee_for_notional(exit_notional)
            proceeds = exit_notional - fee_close
            self.cash += proceeds
            pnl = proceeds - pos.cost_basis
        else:
            exit_notional = pos.qty * (1.0 - actual_exit_price)  # NO proceeds
            fee_close = self.fee_for_notional(exit_notional)
            pnl = pos.qty * (pos.fill_price - actual_exit_price) - pos.fee_open - fee_close
            self.cash += pos.cost_basis + pnl
        if pnl > 0:
            self._wins += 1
        else:
            self._losses += 1
            market_loss_tracker.record_loss(tid)
        if reason == "tp": self._tp_count += 1
        elif reason == "sl": self._sl_count += 1
        elif reason == "time_exit": self._time_count += 1
        elif reason == "breakeven": self._be_count += 1
        elif reason == "trailing_stop": self._trail_count += 1
        self.realized_pnl += pnl
        del self.positions[tid]
        self._trade_count += 1
        append_trade({
            "ts": utc_ts(), "event": "CLOSE", "slug": tid, "side": pos.side,
            "qty": f"{pos.qty:.4f}", "entry_mid": f"{pos.entry_mid:.6f}",
            "exit_mid": f"{actual_exit_price:.6f}", "pnl": f"{pnl:.4f}",
            "cash_after": f"{self.cash:.2f}",
            "reason": reason, "fee": f"{(pos.fee_open + fee_close):.4f}",
            "z_score": f"{pos.z_score:.2f}", "strategy": pos.strategy
        })
        self.sync_balance(force=True)
        return pos, pnl

    def cleanup_all(self):
        active = set(self.positions.keys())
        for tid in list(self._price_cache.keys()):
            if tid not in active:
                del self._price_cache[tid]
        self.sync_balance()

    def get_status_dict(self) -> dict:
        total = self._wins + self._losses
        return {
            "open": len(self.positions), "cash": self.cash,
            "locked": self.get_locked_capital(),
            "unrealized_pnl": self.get_unrealized_pnl(),
            "realized_pnl": self.realized_pnl,
            "equity": self.get_equity(),
            "trades": self._trade_count, "wins": self._wins,
            "losses": self._losses,
            "win_rate": self._wins / max(1, total) * 100,
            "tp": self._tp_count, "sl": self._sl_count,
            "time": self._time_count, "be": self._be_count,
            "trail": self._trail_count,
        }

# =========================
# Tailer, parsing, main loop, etc.
# =========================

class TailState:
    def __init__(self, path: str):
        self.path = path
        self._fh = None
        self._offset = 0
        self._lock = threading.Lock()
        ensure_file_exists(path)
        self._open()
    
    def _open(self):
        if self._fh:
            try: self._fh.close()
            except: pass
        self._fh = safe_open(self.path, "r")
        self._fh.seek(0, os.SEEK_END)
        self._offset = self._fh.tell()
    
    def read_new_lines(self, max_lines: int = MAX_TAILER_LINES) -> List[str]:
        with self._lock:
            try:
                if os.path.getsize(self.path) < self._offset:
                    self._open()
                    return []
                self._fh.seek(self._offset)
                content = self._fh.read(65536)
                if not content:
                    return []
                last_nl = content.rfind('\n')
                if last_nl == -1:
                    return []
                self._offset += last_nl + 1
                return content[:last_nl].splitlines()[-max_lines:]
            except Exception:
                try: self._open()
                except: pass
                return []
    
    def close(self):
        with self._lock:
            if self._fh:
                try: self._fh.close()
                except: pass
                self._fh = None

@dataclass
class SkipCounters:
    z_out_of_band: int = 0
    rearm_gate: int = 0
    already_open: int = 0
    max_concurrent: int = 0
    bad_row: int = 0
    low_cash: int = 0
    price_filter: int = 0
    spread_filter: int = 0
    volume_filter: int = 0
    blocked_market: int = 0
    market_loss_limit: int = 0
    trend_rejected: int = 0
    token_invalid: int = 0
    delta_too_small: int = 0
    signal_stale: int = 0
    open_cooldown: int = 0
    game_phase_blocked: int = 0
    signals_processed: int = 0
    def clear(self):
        for k in self.__dict__:
            setattr(self, k, 0)
    def has_any(self) -> bool:
        return any(v > 0 for v in self.__dict__.values())

def row_to_signal_from_triggers(row: Dict[str, str]) -> Optional[Tuple[str, str, float, float]]:
    if to_upper(row.get("decision")) != "ACCEPT":
        return None
    hint = to_upper(row.get("hint_candidate"))
    if FADE_ONLY_TRIGGERS and hint != "FADE":
        return None
    slug = (row.get("market_slug") or row.get("tid") or "").strip()
    if not slug:
        return None
    mid = to_float(row.get("mid"))
    if not (0 < mid < 1):
        return None
    delta = to_float(row.get("delta"), 0.0)
    delta_pct = abs(delta) / mid if mid > 0 else 0.0
    if delta_pct < MIN_DELTA_PCT or delta_pct > MAX_DELTA_PCT:
        return None
    abs_z = abs(to_float(row.get("abs_z") or row.get("z"), 0.0))
    spread = to_float(row.get("spread"), 0.0)
    max_spread = MAX_SPREAD_HIGH if abs_z >= 5.0 else MAX_SPREAD_MID if abs_z >= 4.0 else MAX_SPREAD_BASE
    if spread > max_spread:
        return None
    volume = to_float(row.get("volume"), 0.0)
    if volume < MIN_VOLUME:
        return None
    regime = to_upper(row.get("regime"))
    if regime and regime not in ("MEAN_REVERT", ""):
        return None
    sig = to_upper(row.get("signal"))
    if sig == "SPIKE":
        side = "BUY_NO"
    elif sig == "DIP":
        side = "BUY"
    else:
        ds = to_float(row.get("direction_strength"))
        if math.isfinite(ds) and ds != 0:
            side = "BUY_NO" if ds > 0 else "BUY"
        elif math.isfinite(delta) and delta != 0:
            side = "BUY_NO" if delta > 0 else "BUY"
        else:
            side = "BUY" if mid < 0.5 else "BUY_NO"
    return slug, side, mid, abs_z

def row_to_signal_from_outliers(row: Dict[str, str]) -> Optional[Tuple[str, str, float, float]]:
    if to_upper(row.get("trade_hint")) != "FADE":
        return None
    z = to_float(row.get("z"))
    abs_z = abs(z) if math.isfinite(z) else to_float(row.get("abs_z"), 0.0)
    if abs_z < Z_OPEN_OUTLIER:
        return None
    slug = (row.get("market_slug") or row.get("tid") or "").strip()
    if not slug:
        return None
    mid = to_float(row.get("mid"))
    if not (0 < mid < 1):
        return None
    delta = to_float(row.get("delta"), 0.0)
    delta_pct = abs(delta) / mid if mid > 0 else 0.0
    if delta_pct < MIN_DELTA_PCT or delta_pct > MAX_DELTA_PCT:
        return None
    spread = to_float(row.get("spread"), 0.0)
    max_spread = MAX_SPREAD_HIGH if abs_z >= 5.0 else MAX_SPREAD_MID if abs_z >= 4.0 else MAX_SPREAD_BASE
    if spread > max_spread:
        return None
    volume = to_float(row.get("volume"), 0.0)
    if volume < MIN_VOLUME:
        return None
    regime = to_upper(row.get("regime"))
    if regime and regime not in ("MEAN_REVERT", ""):
        return None
    side = "BUY_NO" if (math.isfinite(z) and z > 0) else "BUY"
    return slug, side, mid, abs_z

# ---------- TREND signal parsers (momentum following — enter WITH the move) ----------

def row_to_trend_from_triggers(row: Dict[str, str]) -> Optional[Tuple[str, str, float, float]]:
    """Parse TREND signal from triggers CSV — same quality gates but enters WITH the move."""
    if not ENABLE_TREND:
        return None
    if to_upper(row.get("decision")) != "ACCEPT":
        return None
    # Accept both FADE and TREND hints — during live games we follow momentum regardless
    hint = to_upper(row.get("hint_candidate"))
    if hint not in ("FADE", "TREND"):
        return None
    slug = (row.get("market_slug") or row.get("tid") or "").strip()
    if not slug:
        return None
    mid = to_float(row.get("mid"))
    if not (0 < mid < 1):
        return None
    delta = to_float(row.get("delta"), 0.0)
    delta_pct = abs(delta) / mid if mid > 0 else 0.0
    if delta_pct < MIN_DELTA_PCT or delta_pct > MAX_DELTA_PCT:
        return None
    abs_z = abs(to_float(row.get("abs_z") or row.get("z"), 0.0))
    spread = to_float(row.get("spread"), 0.0)
    max_spread = MAX_SPREAD_HIGH if abs_z >= 5.0 else MAX_SPREAD_MID if abs_z >= 4.0 else MAX_SPREAD_BASE
    if spread > max_spread:
        return None
    volume = to_float(row.get("volume"), 0.0)
    if volume < MIN_VOLUME:
        return None
    # No regime filter for TREND — works in any regime
    # TREND: enter WITH the move (opposite of FADE)
    sig = to_upper(row.get("signal"))
    if sig == "SPIKE":
        side = "BUY"       # Price going up → buy YES (follow momentum)
    elif sig == "DIP":
        side = "BUY_NO"    # Price going down → buy NO (follow momentum)
    else:
        ds = to_float(row.get("direction_strength"))
        if math.isfinite(ds) and ds != 0:
            side = "BUY" if ds > 0 else "BUY_NO"
        elif math.isfinite(delta) and delta != 0:
            side = "BUY" if delta > 0 else "BUY_NO"
        else:
            return None  # TREND needs clear direction
    return slug, side, mid, abs_z

def row_to_trend_from_outliers(row: Dict[str, str]) -> Optional[Tuple[str, str, float, float]]:
    """Parse TREND signal from outliers CSV — follows momentum on strong moves."""
    if not ENABLE_TREND:
        return None
    hint = to_upper(row.get("trade_hint"))
    if hint not in ("FADE", "TREND"):
        return None
    z = to_float(row.get("z"))
    abs_z = abs(z) if math.isfinite(z) else to_float(row.get("abs_z"), 0.0)
    if abs_z < TREND_Z_OPEN_OUTLIER:
        return None
    slug = (row.get("market_slug") or row.get("tid") or "").strip()
    if not slug:
        return None
    mid = to_float(row.get("mid"))
    if not (0 < mid < 1):
        return None
    delta = to_float(row.get("delta"), 0.0)
    delta_pct = abs(delta) / mid if mid > 0 else 0.0
    if delta_pct < MIN_DELTA_PCT or delta_pct > MAX_DELTA_PCT:
        return None
    spread = to_float(row.get("spread"), 0.0)
    max_spread = MAX_SPREAD_HIGH if abs_z >= 5.0 else MAX_SPREAD_MID if abs_z >= 4.0 else MAX_SPREAD_BASE
    if spread > max_spread:
        return None
    volume = to_float(row.get("volume"), 0.0)
    if volume < MIN_VOLUME:
        return None
    # TREND: enter WITH the move (z > 0 means spike up → BUY YES)
    side = "BUY" if (math.isfinite(z) and z > 0) else "BUY_NO"
    return slug, side, mid, abs_z

def extract_mid_from_row(row: Dict[str, str]) -> Optional[Tuple[str, float, float]]:
    slug = (row.get("market_slug") or row.get("tid") or "").strip()
    if not slug:
        return None
    mid = to_float(row.get("mid"))
    if not (0 < mid < 1):
        return None
    ts = to_float(row.get("ts_epoch"), time.time())
    return slug, mid, ts

def parse_csv_lines(header: List[str], lines: List[str]) -> List[Dict[str, str]]:
    out = []
    for ln in lines[-100:]:
        if ln and "," in ln:
            try:
                parts = list(csv.reader([ln]))[0]
                out.append({h: parts[i] if i < len(parts) else "" for i, h in enumerate(header)})
            except Exception:
                pass
    return out

def read_header(path: str) -> List[str]:
    ensure_file_exists(path)
    with safe_open(path, "r") as fh:
        first = fh.readline().strip()
        return [h.strip() for h in first.split(",")] if first and "," in first else []

_cleanup_done = False
_tailers: List[TailState] = []

def register_tailer(t: TailState):
    _tailers.append(t)

def cleanup_on_exit():
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    for t in _tailers:
        try: t.close()
        except: pass
    _tailers.clear()
    try: http_session.close()
    except: pass
    gc.collect()

atexit.register(cleanup_on_exit)

def main():
    mode_str = "LIVE" if LIVE else "PAPER"
    print("=" * 60)
    print(f"TRADE BOT v15.0 - POLYMARKET US API (FADE + TREND STRATEGIES)")
    print("=" * 60)
    print(f"Mode: {mode_str}")
    print(f"FADE  TP: {TP_PCT*100:.1f}%  SL: {SL_PCT*100:.1f}%  Time: {TIME_EXIT_SEC_PRIMARY}s  BE: {BREAKEVEN_EXIT_SEC}s")
    print(f"TREND TP: {TREND_TP_PCT*100:.1f}%  SL: {TREND_SL_PCT*100:.1f}%  Time: {TREND_TIME_EXIT_SEC}s  BE: {TREND_BREAKEVEN_EXIT_SEC}s  (enabled={ENABLE_TREND})")
    print(f"FADE  Trail: activate={TRAILING_ACTIVATE_PCT*100:.1f}% trail={TRAILING_STOP_PCT*100:.1f}%")
    print(f"TREND Trail: activate={TREND_TRAILING_ACTIVATE_PCT*100:.1f}% trail={TREND_TRAILING_STOP_PCT*100:.1f}%")
    print(f"Z threshold: FADE >= {Z_OPEN} / TREND >= {TREND_Z_OPEN}")
    print(f"Delta: {MIN_DELTA_PCT*100:.1f}%-{MAX_DELTA_PCT*100:.0f}%  Mid: {MIN_MID_PRICE}-{MAX_MID_PRICE}  Age: {MAX_SIGNAL_AGE_SEC}s  Cooldown: {MIN_OPEN_INTERVAL_SEC}s")
    print(f"Max concurrent: {MAX_CONCURRENT_POS}  TREND during live games: {ENABLE_TREND}")
    print(f"API Base: {PM_US_BASE_URL}")
    print("=" * 60)

    broker = PaperBroker() if PAPER else LiveBroker()

    ensure_file_exists(TRIGGERS_CSV)
    ensure_file_exists(OUTLIERS_CSV)
    ensure_trades_header()

    t_trig = TailState(TRIGGERS_CSV)
    t_outl = TailState(OUTLIERS_CSV)
    register_tailer(t_trig)
    register_tailer(t_outl)

    hdr_trig = read_header(TRIGGERS_CSV)
    hdr_outl = read_header(OUTLIERS_CSV)

    skips = SkipCounters()
    last_status = last_summary = last_cleanup = now()
    last_open_ts = 0.0
    stop = False

    def handle_sig(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    while not stop:
        try:
            if PAPER:
                broker.latest_mids = load_latest_mids(MIDS_JSON_PATH)
            else:
                broker.sync_balance()

            new_trig = t_trig.read_new_lines()
            new_outl = t_outl.read_new_lines()
            trig_rows = parse_csv_lines(hdr_trig, new_trig) if hdr_trig else []
            outl_rows = parse_csv_lines(hdr_outl, new_outl) if hdr_outl else []

            if PAPER:
                for row in trig_rows + outl_rows:
                    parsed = extract_mid_from_row(row)
                    if parsed:
                        slug, mid, ts = parsed
                        broker.csv_mids[slug] = (mid, ts)

            for tid in list(broker.positions):
                pos = broker.positions.get(tid)
                if not pos:
                    continue
                current = broker.get_current_yes_mid(pos)
                if not (0 < current < 1):
                    continue
                age = now() - pos.entry_ts
                is_stale = getattr(broker, '_is_stale_mid', lambda p, c: False)(pos, current)
                # Use executable price (bid/ask) for profit-taking to avoid TP on inflated mid
                exec_price = broker.get_executable_exit_price(pos)
                ep = get_exit_params(pos.strategy)
                stag = f"[{pos.strategy}] " if pos.strategy != "FADE" else ""

                if hit_take_profit(pos.side, pos.entry_mid, exec_price, ep["tp"]):
                    res = broker.close(tid, current, "tp")
                    if res:
                        _, pnl = res
                        print(f"✅ [TP] {stag}{tid[:16]}... {pos.side} pnl=${pnl:.4f}")
                        rearm_tracker.touch(tid)
                    continue

                if hit_stop_loss(pos.side, pos.entry_mid, current, ep["sl"]):
                    res = broker.close(tid, current, "sl")
                    if res:
                        _, pnl = res
                        print(f"🛑 [SL] {stag}{tid[:16]}... {pos.side} pnl=${pnl:.4f}")
                        rearm_tracker.touch(tid)
                    continue

                if ENABLE_TRAILING_STOP and not is_stale:
                    should_trail, new_peak = check_trailing_stop(
                        pos, exec_price, is_stale=is_stale,
                        activate_pct=ep["trail_activate"], stop_pct=ep["trail_stop"])
                    pos.peak_profit_pct = new_peak
                    if new_peak >= ep["trail_activate"] and not pos.trailing_active:
                        pos.trailing_active = True
                    if should_trail:
                        res = broker.close(tid, current, "trailing_stop")
                        if res:
                            _, pnl = res
                            print(f"✅ [TRAIL] {stag}{tid[:16]}... {pos.side} pnl=${pnl:.4f} peak={new_peak*100:.1f}%")
                            rearm_tracker.touch(tid)
                        continue

                if not is_stale and hit_breakeven_exit(pos.side, pos.entry_mid, current, age, ep["be_sec"], ep["be_tol"]):
                    res = broker.close(tid, current, "breakeven")
                    if res:
                        _, pnl = res
                        print(f"↔️ [BE] {stag}{tid[:16]}... {pos.side} pnl=${pnl:.4f}")
                        rearm_tracker.touch(tid)
                    continue

                if age >= ep["time"]:
                    res = broker.close(tid, current, "time_exit")
                    if res:
                        _, pnl = res
                        print(f"⏰ [TIME] {stag}{tid[:16]}... {pos.side} pnl=${pnl:.4f}")
                        rearm_tracker.touch(tid)

            def try_open(rows, src):
                nonlocal last_open_ts
                for r in rows:
                    skips.signals_processed += 1
                    try:
                        ts_epoch = to_float(r.get("ts_epoch"), 0)
                        if ts_epoch > 0 and abs(time.time() - ts_epoch) > MAX_SIGNAL_AGE_SEC:
                            skips.signal_stale += 1
                            continue

                        game_phase = (r.get("game_phase") or "").strip().upper()

                        # Strategy selection: TREND for live games, FADE otherwise
                        if ENABLE_TREND and game_phase == "UNKNOWN":
                            # Live game — prefer TREND (follow momentum)
                            sig = row_to_trend_from_triggers(r) if src == "TRIG" else row_to_trend_from_outliers(r)
                            strategy = "TREND"
                            z_min = TREND_Z_OPEN
                            if not sig:
                                # Fallback to FADE if TREND parser rejects
                                sig = row_to_signal_from_triggers(r) if src == "TRIG" else row_to_signal_from_outliers(r)
                                strategy = "FADE"
                                z_min = Z_OPEN
                        else:
                            # Non-live — FADE first, TREND fallback
                            sig = row_to_signal_from_triggers(r) if src == "TRIG" else row_to_signal_from_outliers(r)
                            strategy = "FADE"
                            z_min = Z_OPEN
                            if not sig and ENABLE_TREND:
                                sig = row_to_trend_from_triggers(r) if src == "TRIG" else row_to_trend_from_outliers(r)
                                strategy = "TREND"
                                z_min = TREND_Z_OPEN

                        if not sig:
                            continue
                        tid, side, mid, z = sig
                        if BLOCK_PRE_GAME and game_phase == "PRE_GAME":
                            skips.game_phase_blocked += 1
                            continue
                        if not ALLOW_UNKNOWN_PHASE and game_phase == "UNKNOWN":
                            skips.game_phase_blocked += 1
                            continue
                        if broker.is_blocked(tid):
                            if market_loss_tracker.is_blocked(tid):
                                skips.market_loss_limit += 1
                            else:
                                skips.blocked_market += 1
                            continue
                        if len(broker.positions) >= MAX_CONCURRENT_POS:
                            skips.max_concurrent += 1
                            continue
                        if tid in broker.positions:
                            skips.already_open += 1
                            continue
                        if not rearm_tracker.can_rearm(tid):
                            skips.rearm_gate += 1
                            continue
                        if time.time() - last_open_ts < MIN_OPEN_INTERVAL_SEC:
                            skips.open_cooldown += 1
                            continue
                        if not broker.can_afford_trade():
                            skips.low_cash += 1
                            continue
                        if z < z_min:
                            skips.z_out_of_band += 1
                            continue
                        if mid < MIN_MID_PRICE or mid > MAX_MID_PRICE:
                            skips.price_filter += 1
                            continue
                        pos = broker.open(tid, side, mid, z, strategy=strategy)
                        if pos:
                            last_open_ts = time.time()
                            rearm_tracker.touch(tid)
                            stag = f"[{strategy}] " if strategy != "FADE" else ""
                            print(f"🔵 [OPEN] {stag}{tid[:16]}... {side} mid={mid:.4f} z={z:.1f} delta_pct={abs(to_float(r.get('delta'),0))/max(mid,1e-9)*100:.1f}%")
                    except Exception as e:
                        logger.warning(f"Error processing {src} row: {e}")
                        skips.bad_row += 1

            try_open(trig_rows, "TRIG")
            try_open(outl_rows, "OUTL")

            t = now()
            if t - last_cleanup >= CLEANUP_EVERY_SEC:
                if hasattr(broker, 'cleanup_all'):
                    broker.cleanup_all()
                rearm_tracker.force_cleanup()
                gc.collect()
                last_cleanup = t

            if t - last_status >= STATUS_EVERY_SEC:
                s = broker.get_status_dict()
                mem = f" RAM {get_memory_mb():.0f}MB" if HAS_PSUTIL else ""
                print(f"[STATUS] pos={s['open']} eq=${s['equity']:.2f} pnl=${s['realized_pnl']:.4f} W:{s['wins']}/L:{s['losses']} ({s['win_rate']:.0f}%) TP:{s['tp']} SL:{s['sl']} T:{s['time']} BE:{s['be']} TR:{s['trail']}{mem}")
                last_status = t

            if t - last_summary >= SUMMARY_EVERY_SEC and skips.has_any():
                bits = [f"{k}:{v}" for k, v in skips.__dict__.items() if v and k != "signals_processed"]
                sig_count = skips.signals_processed
                summary_parts = [f"signals:{sig_count}"] if sig_count else []
                summary_parts.extend(bits)
                if summary_parts:
                    print(f"[SKIPS] {' '.join(summary_parts)}")
                skips.clear()
                last_summary = t

            time.sleep(0.25)

        except Exception as e:
            logger.error(f"Main loop error: {e}")
            traceback.print_exc()
            time.sleep(1)

    print("[SHUTDOWN] Closing positions...")
    for tid in list(broker.positions):
        try:
            broker.close(tid, broker.get_current_yes_mid(broker.positions[tid]), "shutdown")
        except Exception as e:
            logger.error(f"Error closing {tid}: {e}")
    
    final = broker.get_status_dict()
    print("=" * 60)
    print(f"FINAL: PnL=${final['realized_pnl']:.4f} Equity=${final['equity']:.4f}")
    print(f"W:{final['wins']} L:{final['losses']} ({final['win_rate']:.1f}%) TP:{final['tp']} SL:{final['sl']} T:{final['time']} BE:{final['be']} TR:{final['trail']}")
    print("=" * 60)
    cleanup_on_exit()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[SHUTDOWN]")
    except Exception as e:
        logger.critical(f"FATAL: {e}")
        traceback.print_exc()
    finally:
        cleanup_on_exit()