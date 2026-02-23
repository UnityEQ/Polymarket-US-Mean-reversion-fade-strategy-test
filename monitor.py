#!/usr/bin/env python3
"""
POLYMARKET US CLOB HUNTER - v4.0.0 (WebSocket streaming)

Migrated from Polymarket Global to Polymarket US (CFTC-regulated)
Based on: https://docs.polymarket.us/api/introduction

v4.0.0 — Major changes:
- Replaced REST BBO polling with WebSocket Markets Stream (wss://api.polymarket.us/v1/ws/markets)
- Uses SUBSCRIPTION_TYPE_MARKET_DATA_LITE for real-time BBO data
- Eliminates rate limit concerns (was ~5,000 req/10s, limit is 2,000/10s)
- Sub-second latency instead of multi-second poll loops
- Automatic reconnect with exponential backoff on disconnect
- Volume field standardized to volume24hr (per API docs)
- Market state filtering (MARKET_STATE_OPEN only)
- REST kept for discovery + balance checks only (runs every 5 min)
- All signal logic, CSV writing, and trade hints unchanged

v3.2.0 — Previous fixes kept:
- RLock for CSV (deadlock fix), fail_count reset, stale watchdog, etc.

Requirements:
    pip install websocket-client requests cryptography psutil
"""

import os
import sys
import json
import csv
import time
import signal
import threading
import logging
import gc
import base64
import re
from collections import deque, defaultdict
from statistics import mean, pstdev
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple, List, Set
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import websocket as ws_lib  # websocket-client
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# -------------------- Logging Setup --------------------
_log_file = open("monitor-console-log.txt", "w", encoding="utf-8")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.StreamHandler(_log_file),
    ]
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("websocket").setLevel(logging.WARNING)

DEBUG = os.getenv("DEBUG", "0") == "1"

# -------------------- Configuration --------------------
POLYMARKET_KEY_ID = os.getenv("POLYMARKET_KEY_ID", "")
POLYMARKET_SECRET_KEY = os.getenv("POLYMARKET_SECRET_KEY", "")

US_API_BASE = "https://api.polymarket.us"
US_WS_URL   = "wss://api.polymarket.us/v1/ws/markets"

HEARTBEAT             = 8
MARKET_REFRESH_SEC    = 300
VOLUME_REFRESH_SEC    = 60  # New: Refresh volumes more frequently
WS_PING_INTERVAL_SEC  = 30
WS_RECONNECT_BASE_SEC = 1.0
WS_RECONNECT_MAX_SEC  = 60.0

BASE_SPIKE_THRESHOLD  = 0.003
BASE_Z_SCORE_MIN      = 0.8
WATCH_Z               = 1.5
ALERT_Z               = 3.0
MIN_VOLUME_24H        = 0
TAIL_MIN, TAIL_MAX    = 0.01, 0.99
HISTORY_LEN           = 50
GLOBAL_DELTAS_MAXLEN  = 2000
TOP_PERCENTILE        = 50
WARMUP_GLOBAL_MIN     = 20
WARMUP_Z_EXTRA        = 0.1

MID_MIN, MID_MAX      = 0.12, 0.55
V24_MIN               = 10  # Liquidity gate: openInterest/sharesTraded proxy (API has no volume)
COOLDOWN_PER_SLUG     = 60

FADE_Z_MIN            = 2.0
FADE_Z_MAX            = 6.0
TREND_Z_MIN           = 3.0

REGIME_WINDOW         = 100
REGIME_UPDATE_EVERY   = 20

SPREAD_MAX_SAFE       = 0.20
SPREAD_MAX_TREND      = 0.25
POSITION_SIZE_FACTOR  = 0.01

MAX_MARKETS = 500

# Liquidity filters
MIN_BID_SIZE          = 1.0
MIN_ASK_SIZE          = 1.0
MAX_SPREAD_PCT        = 0.15

# Blacklist after repeated failures
MAX_FAILURES          = 5

SHARES_ACTIVITY_MIN   = 50  # Lowered: Min lifetime shares for "active" if volume $0

today = time.strftime("%Y-%m-%d")
OUTLIER_CSV  = f"poly_us_outliers_{today}.csv"
TRIGGERS_CSV = f"poly_us_triggers_{today}.csv"
BLOWOUT_CSV  = f"poly_us_blowout_{today}.csv"

CSV_LOCK = threading.RLock()

OUT_HEADER = [
    "ts_iso", "ts_epoch", "event", "market_slug", "event_url", "market_url",
    "price_prev", "mid_before", "mid",
    "delta", "abs_delta", "z", "abs_z", "direction_strength", "signal",
    "volume", "best_bid", "best_ask", "spread", "percentile", "severity", "burst",
    "trade_hint", "regime", "game_phase", "pm_period", "pm_score_diff"
]

TRIGGER_HEADER = [
    "ts_epoch", "market_slug", "event", "signal",
    "mid", "delta", "abs_z", "direction_strength", "volume",
    "hint_candidate", "decision", "reason", "regime", "spread",
    "pos_size_hint", "game_phase", "pm_period", "pm_score_diff"
]

# -------------------- Polymarket US Client (REST only — discovery + balance) --------------------
class PolymarketUSClient:
    """REST client for Polymarket US API — used for discovery and balance only."""

    def __init__(self, key_id: str, secret_key: str):
        self.key_id = key_id
        self.secret_key = secret_key
        self._private_key = None
        self._sdk_client = None
        self._session = self._create_session()

        if key_id and secret_key:
            self._load_key()
            self._init_sdk()

    def _create_session(self) -> requests.Session:
        sess = requests.Session()
        retries = Retry(total=3, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
        sess.mount("https://", adapter)
        sess.headers.update({"User-Agent": "PolymarketUSHunter/4.0.0", "Accept": "application/json"})
        return sess

    def _load_key(self):
        try:
            from cryptography.hazmat.primitives.asymmetric import ed25519
            key_bytes = base64.b64decode(self.secret_key)
            self._private_key = ed25519.Ed25519PrivateKey.from_private_bytes(key_bytes[:32])
            logger.info("✅ Ed25519 key loaded")
        except Exception as e:
            logger.error(f"❌ Key load failed: {e}")

    def _init_sdk(self):
        try:
            from polymarket_us import PolymarketUS
            self._sdk_client = PolymarketUS(key_id=self.key_id, secret_key=self.secret_key)
            logger.info("✅ SDK initialized")
        except ImportError:
            logger.warning("⚠️ polymarket-us SDK not installed — using raw REST")
        except Exception as e:
            logger.error(f"❌ SDK init failed: {e}")

    def _sign_request(self, method: str, path: str, timestamp_ms: str) -> str:
        if not self._private_key:
            raise ValueError("Key not loaded")
        message = f"{timestamp_ms}{method}{path}"
        signature = self._private_key.sign(message.encode('utf-8'))
        return base64.b64encode(signature).decode('utf-8')

    def _get_headers(self, method: str, path: str) -> dict:
        timestamp_ms = str(int(time.time() * 1000))
        signature = self._sign_request(method, path, timestamp_ms)
        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp": timestamp_ms,
            "X-PM-Signature": signature,
            "Content-Type": "application/json",
        }

    def _raw_get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        try:
            headers = self._get_headers("GET", path)
            resp = self._session.get(f"{US_API_BASE}{path}", headers=headers, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                logger.warning(f"Rate limited on {path}")
        except Exception as e:
            logger.debug(f"API error {path}: {e}")
        return None

    def get_markets(self, limit: int = 500, active: bool = True, closed: bool = False) -> List[dict]:
        all_markets = []
        offset = 0
        page_size = min(limit, 100)

        while len(all_markets) < limit:
            page = []
            if self._sdk_client:
                try:
                    result = self._sdk_client.markets.list({
                        "limit": page_size, "offset": offset,
                        "active": active, "closed": closed,
                    })
                    if isinstance(result, dict):
                        page = result.get("markets", result.get("data", []))
                    elif isinstance(result, list):
                        page = result
                except Exception as e:
                    logger.warning(f"SDK markets page failed (offset={offset}): {e}")

            if not page:
                data = self._raw_get("/v1/markets", {
                    "limit": str(page_size), "offset": str(offset),
                    "active": str(active).lower(), "closed": str(closed).lower(),
                })
                if isinstance(data, dict):
                    page = data.get("markets", data.get("data", []))
                elif isinstance(data, list):
                    page = data

            if not page:
                break
            all_markets.extend(page)
            offset += len(page)
            if len(page) < page_size:
                break
            time.sleep(0.1)

        return all_markets[:limit]

    def get_market_details(self, slug: str) -> Optional[dict]:
        if self._sdk_client:
            try:
                result = self._sdk_client.markets.get(slug)
                # SDK may return typed objects; convert to dict if needed
                if result and not isinstance(result, dict):
                    try:
                        result = dict(result) if hasattr(result, '__iter__') else vars(result)
                    except (TypeError, ValueError):
                        pass
                # Unwrap {"market": {...}} wrapper if present
                if isinstance(result, dict) and "market" in result and isinstance(result["market"], dict):
                    result = result["market"]
                return result
            except:
                pass
        resp = self._raw_get(f"/v1/market/slug/{slug}")
        # Unwrap {"market": {...}} wrapper if present
        if isinstance(resp, dict) and "market" in resp and isinstance(resp["market"], dict):
            resp = resp["market"]
        return resp

    def get_balances(self) -> dict:
        if self._sdk_client:
            try:
                return self._sdk_client.account.balances()
            except:
                pass
        return self._raw_get("/v1/account/balances") or {"balances": []}

    def close(self):
        if self._sdk_client:
            try:
                self._sdk_client.close()
            except:
                pass

# -------------------- State --------------------
class MonitorState:
    def __init__(self):
        self.hist: Dict[str, deque] = {}
        self.meta: Dict[str, dict] = {}
        self.global_deltas: deque = deque(maxlen=GLOBAL_DELTAS_MAXLEN)
        self.last_spike: Dict[str, dict] = {}
        self.mid_cache: Dict[str, Tuple[float, float]] = {}
        self.spread_cache: Dict[str, deque] = {}
        self.cooldown: Dict[str, float] = {}
        self.spike_outcomes: deque = deque(maxlen=REGIME_WINDOW)
        self.current_regime: str = "MEAN_REVERT"
        self.regime_counter: int = 0
        self.last_msg_time: float = 0.0
        self.last_refresh: float = 0.0
        self.last_cleanup: float = 0.0
        self.recent_signals: deque = deque(maxlen=100)
        self.blacklist: Set[str] = set()
        self.fail_count: Dict[str, int] = defaultdict(int)
        # WebSocket stats
        self.ws_messages_total: int = 0
        self.ws_updates_processed: int = 0
        self.ws_connected: bool = False
        self.ws_reconnects: int = 0

    def get_stats(self) -> dict:
        return {
            'meta': len(self.meta),
            'blacklisted': len(self.blacklist),
            'global_deltas': len(self.global_deltas),
            'mid_cache': len(self.mid_cache),
            'ws_msgs': self.ws_messages_total,
            'ws_updates': self.ws_updates_processed,
            'ws_connected': self.ws_connected,
            'ws_reconnects': self.ws_reconnects,
        }

STATE = MonitorState()
STOP = threading.Event()
CLIENT: Optional[PolymarketUSClient] = None

# -------------------- Helpers --------------------
def iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def ensure_headers():
    """Truncate CSV files and write fresh headers on each run."""
    for path, header in [(OUTLIER_CSV, OUT_HEADER), (TRIGGERS_CSV, TRIGGER_HEADER)]:
        with CSV_LOCK:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(header)

def write_csv_row(path: str, header: list, row_dict: dict):
    with CSV_LOCK:
        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([row_dict.get(c, "") for c in header])
        except Exception as e:
            logger.error(f"CSV error: {e}")

def get_memory_mb() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024) if HAS_PSUTIL else 0.0

def parse_date_from_slug(slug: str) -> Optional[datetime]:
    # Match YYYY-MM-DD at end or followed by -outcome suffix (MLS three-way markets)
    match = re.search(r'(\d{4}-\d{2}-\d{2})(?:-[a-z]+)?$', slug)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except:
            pass
    return None


def parse_slug_parts(slug: str) -> Optional[dict]:
    """Parse Polymarket slug into sport, team1, team2, date_str.

    Handles: aec-cbb-duke-mich-2026-02-21, atc-mls-hou-chi-2026-02-21-draw
    """
    m = re.match(r'^(?:aec|atc)-([a-z]+)-(.+)-(\d{4}-\d{2}-\d{2})(?:-[a-z]+)?$', slug)
    if not m:
        return None
    sport, teams_part, date_str = m.groups()
    parts = teams_part.rsplit('-', 1)
    if len(parts) != 2:
        return None
    return {'sport': sport, 'team1': parts[0], 'team2': parts[1], 'date_str': date_str}


# -------------------- Polymarket Native Score Cache --------------------

PM_SCORE_REFRESH_SEC = 60
PM_SCORE_SPORTS = {'nba', 'cbb', 'nfl', 'ufc', 'mls'}


class PMScoreCache:
    """Fetches and caches live score data from Polymarket's own market detail endpoint.

    Uses GET /v1/market/slug/{slug} → events[0] for live/period/score fields.
    No external dependency — 1:1 slug match, zero mapping needed.
    """

    def __init__(self):
        self._cache: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def refresh(self, active_slugs: dict, client: 'PolymarketUSClient'):
        """Fetch market details for today's sports slugs and extract score data."""
        today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        slugs_to_fetch: List[str] = []
        for slug in active_slugs:
            sp = parse_slug_parts(slug)
            if sp and sp['sport'] in PM_SCORE_SPORTS and sp['date_str'] == today_str:
                slugs_to_fetch.append(slug)

        if not slugs_to_fetch:
            with self._lock:
                self._cache = {}
            return

        new_cache: Dict[str, dict] = {}
        errors = 0
        for slug in slugs_to_fetch:
            try:
                details = client.get_market_details(slug)
                if not details:
                    errors += 1
                    continue
                events = details.get("events") or []
                if not events:
                    # No events data — cache as pre-game (safe default)
                    new_cache[slug] = {
                        'pm_state': 'pre',
                        'period': 0,
                        'score_diff': -1,
                    }
                    continue
                ev = events[0] if isinstance(events, list) else events
                # Determine state from event fields
                is_live = ev.get("live", False)
                is_ended = ev.get("ended", False)
                if is_ended:
                    pm_state = "post"
                elif is_live:
                    pm_state = "in"
                else:
                    pm_state = "pre"

                sp = parse_slug_parts(slug)
                sport = sp['sport'] if sp else ''
                period = self._parse_period(ev.get("period", ""), sport)
                score_diff = self._parse_score_diff(ev.get("score", ""))

                new_cache[slug] = {
                    'pm_state': pm_state,
                    'period': period,
                    'score_diff': score_diff,
                    'elapsed': ev.get("elapsed") or "",
                    '_raw_score': ev.get("score") or "",
                }
                # Enrich STATE.meta with real game start time from events[0]
                # (REST listing doesn't include gameStartTime — this is the only source)
                start_time = ev.get("startTime") or ""
                if start_time and slug in active_slugs:
                    active_slugs[slug]["game_start_time"] = start_time
            except Exception:
                errors += 1
            time.sleep(0.1)  # 100ms between requests — ~5-8s for 50-80 slugs

        with self._lock:
            self._cache = new_cache

        live = sum(1 for v in new_cache.values() if v['pm_state'] == 'in')
        logger.info(f"[PM-SCORE] {len(new_cache)}/{len(slugs_to_fetch)} fetched ({live} live, {errors} errors)")

    @staticmethod
    def _parse_period(period_str: str, sport: str) -> int:
        """Convert period string to numeric. 'Q2'→2, 'H1'→1, 'OT'→5/3, ''→0."""
        if not period_str:
            return 0
        p = str(period_str).strip().upper()
        # Numeric already
        try:
            return int(p)
        except ValueError:
            pass
        # Quarter: Q1-Q4
        if p.startswith('Q') and len(p) == 2 and p[1].isdigit():
            return int(p[1])
        # Half: H1, H2
        if p.startswith('H') and len(p) == 2 and p[1].isdigit():
            return int(p[1])
        # Overtime
        if p.startswith('OT'):
            # CBB has 2 halves, so OT=3; NBA has 4 quarters, so OT=5
            if sport == 'cbb':
                return 3
            return 5
        return 0

    @staticmethod
    def _parse_score_diff(score_str: str) -> int:
        """Parse score string like '31-48' → 17. Empty → -1 (unknown)."""
        if not score_str:
            return -1
        try:
            parts = str(score_str).split('-')
            if len(parts) == 2:
                return abs(int(parts[0].strip()) - int(parts[1].strip()))
        except (ValueError, TypeError):
            pass
        return -1

    def get(self, slug: str) -> Optional[dict]:
        with self._lock:
            return self._cache.get(slug)


PM_SCORE_CACHE: Optional[PMScoreCache] = None

# -------------------- Blowout Monitoring --------------------
# Logs late-game blowouts to CSV for strategy research.
# Tracks whether the market has priced in the likely winner.

BLOWOUT_HEADER = [
    "ts_iso", "slug", "sport", "period", "score", "elapsed",
    "score_diff", "yes_mid", "implied_leader_prob"
]

# Sport-specific thresholds: (min_period, min_score_diff)
# Only log when game is deep enough that the lead is meaningful.
BLOWOUT_THRESHOLDS = {
    'nba': (3, 15),   # 3rd quarter+, 15+ points
    'cbb': (2, 12),   # 2nd half, 12+ points
    'nfl': (3, 14),   # 3rd quarter+, 2+ TDs
    'mls': (2, 2),    # 2nd half, 2+ goals
}

_blowout_header_written = False

def log_blowout_opportunities():
    """Check live games for blowout conditions and log to CSV for strategy research."""
    global _blowout_header_written
    if not PM_SCORE_CACHE:
        return
    with PM_SCORE_CACHE._lock:
        cache_snapshot = dict(PM_SCORE_CACHE._cache)

    rows = []
    now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    for slug, data in cache_snapshot.items():
        if data['pm_state'] != 'in':
            continue
        sp = parse_slug_parts(slug)
        if not sp:
            continue
        sport = sp['sport']
        thresholds = BLOWOUT_THRESHOLDS.get(sport)
        if not thresholds:
            continue
        min_period, min_diff = thresholds
        period = data['period']
        score_diff = data['score_diff']
        if period < min_period or score_diff < min_diff:
            continue
        # Get YES mid from STATE.mid_cache
        mid_entry = STATE.mid_cache.get(slug)
        if not mid_entry:
            continue
        yes_mid = mid_entry[0]
        implied_leader = max(yes_mid, 1.0 - yes_mid)
        rows.append({
            'ts_iso': now_iso,
            'slug': slug,
            'sport': sport,
            'period': period,
            'score': data.get('_raw_score', ''),
            'elapsed': data.get('elapsed', ''),
            'score_diff': score_diff,
            'yes_mid': f"{yes_mid:.4f}",
            'implied_leader_prob': f"{implied_leader:.4f}",
        })

    if not rows:
        return

    with CSV_LOCK:
        try:
            write_header = not _blowout_header_written and not os.path.exists(BLOWOUT_CSV)
            with open(BLOWOUT_CSV, 'a', newline='') as f:
                w = csv.DictWriter(f, fieldnames=BLOWOUT_HEADER)
                if write_header:
                    w.writeheader()
                    _blowout_header_written = True
                elif not _blowout_header_written:
                    _blowout_header_written = True
                for row in rows:
                    w.writerow(row)
        except Exception as e:
            logger.error(f"[BLOWOUT] CSV write error: {e}")

    for row in rows:
        logger.info(
            f"[BLOWOUT] {row['slug']} | {row['sport'].upper()} P{row['period']} "
            f"diff={row['score_diff']} elapsed={row['elapsed']} | "
            f"yes_mid={row['yes_mid']} implied_leader={row['implied_leader_prob']}"
        )


def classify_game_phase(meta: dict, slug: str) -> str:
    """Classify game phase: PRE_GAME, LIVE, POST_GAME, or UNKNOWN.

    Priority:
    1. Slug date for coarse classification (tomorrow+=PRE_GAME, yesterday-=POST_GAME)
    2. Polymarket native score data (definitive for same-day games)
    3. gameStartTime from meta (if in future → PRE_GAME)
    4. API end_date fallback
    5. UNKNOWN
    """
    from datetime import timedelta
    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    # Step 1: Slug date for coarse classification (reliable for cross-day)
    game_date = parse_date_from_slug(slug)
    if game_date:
        if game_date >= tomorrow_start:
            return "PRE_GAME"
        if game_date < today_start:
            return "POST_GAME"
        # Game date is today — check PM score data for definitive classification

    # Step 2: Polymarket native score data (definitive for same-day games)
    if PM_SCORE_CACHE:
        pm = PM_SCORE_CACHE.get(slug)
        if pm:
            pm_state = pm.get('pm_state', '')
            if pm_state == 'pre':
                return "PRE_GAME"
            elif pm_state == 'in':
                return "LIVE"
            elif pm_state == 'post':
                return "POST_GAME"

    # Step 3: gameStartTime from meta — if in future, it's pre-game
    start_str = meta.get("game_start_time", "")
    if start_str:
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if now_utc < start_dt:
                return "PRE_GAME"
        except (ValueError, TypeError):
            pass

    # Step 4: API end_date fallback for same-day
    end_str = meta.get("end_date", "")
    if end_str:
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            if now_utc > end_dt:
                return "POST_GAME"
        except (ValueError, TypeError):
            pass

    return "UNKNOWN"


def extract_amount_value(obj) -> Optional[float]:
    """Extract numeric value from an Amount object ({value, currency}) or plain number/string."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        v = obj.get("value") or obj.get("price")
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                return None
        return None
    if isinstance(obj, str):
        # Handle formatted strings like "$1,234.56"
        cleaned = obj.replace(",", "").replace("$", "").strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except (ValueError, TypeError):
            return None
    try:
        return float(obj)
    except (ValueError, TypeError):
        return None


def _safe_get(obj, key, default=None):
    """Get a field from a dict or SDK object (supports both .get() and getattr)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    # SDK objects may use attribute access
    return getattr(obj, key, default)


VOLUME_KEYS = ["volume24hr", "volume24h", "last24HrVolume", "volume24Hr", "volumeNum", "volume"]


def _extract_volume(obj, slug: str = "", label: str = "") -> float:
    """Extract volume from a market object, trying multiple field names."""
    for vkey in VOLUME_KEYS:
        raw = _safe_get(obj, vkey)
        if raw is not None:
            v = extract_amount_value(raw)
            if DEBUG:
                logger.info(f"[VOL] {slug} {label}: {vkey}={raw!r} -> {v}")
            if v is not None and v > 0:
                return v
    if DEBUG:
        logger.info(f"[VOL] {slug} {label}: no REST volume (expected for sports — WS openInterest used as fallback)")
    return 0.0

# -------------------- Discovery (REST — runs once at startup + every 5 min) --------------------
def discover(refresh: bool = False):
    now = time.time()
    if not refresh and now - STATE.last_refresh < MARKET_REFRESH_SEC:
        return
    if not CLIENT:
        return

    logger.info("🔍 Discovering active markets...")
    markets = CLIENT.get_markets(limit=MAX_MARKETS, active=True, closed=False)
    if not markets:
        logger.warning("⚠️ No markets returned")
        return

    logger.info(f"📊 API returned {len(markets)} active, open markets")

    # One-time diagnostic: log raw field names from first market for volume debugging
    if markets and not refresh and DEBUG:
        sample = markets[0]
        logger.info(f"[DIAG] Market object type: {type(sample).__name__}")
        vol_keys = [k for k in (sample.keys() if hasattr(sample, 'keys') else dir(sample)) if 'vol' in k.lower() or 'trade' in k.lower() or 'share' in k.lower()]
        logger.info(f"[DIAG] Sample market keys (vol-related): {vol_keys}")
        for vk in vol_keys:
            raw = sample.get(vk) if hasattr(sample, 'get') else getattr(sample, vk, None)
            logger.info(f"[DIAG]   {vk} = {raw!r} (type={type(raw).__name__})")
        try:
            listing_json = json.dumps(sample, default=str, indent=2)
            with open("monitor_listing_debug.json", "w") as f:
                f.write(listing_json)
            logger.info(f"[DIAG] Full listing market written to monitor_listing_debug.json ({len(listing_json)} bytes)")
        except Exception as e:
            logger.info(f"[DIAG] Listing market dump failed: {e}")

    # One-time diagnostic: discover date/timing fields in API response (not gated by DEBUG)
    if markets and not refresh:
        sample = markets[0]
        timing_keywords = ("date", "time", "game", "start", "end", "schedule")
        if hasattr(sample, 'keys'):
            all_keys = list(sample.keys())
        else:
            all_keys = [k for k in dir(sample) if not k.startswith('_')]
        timing_keys = [k for k in all_keys if any(kw in k.lower() for kw in timing_keywords)]
        if timing_keys:
            logger.info(f"[DIAG] Date/timing fields found: {timing_keys}")
            for tk in timing_keys:
                raw = sample.get(tk) if hasattr(sample, 'get') else getattr(sample, tk, None)
                logger.info(f"[DIAG]   {tk} = {raw!r} (type={type(raw).__name__})")
        else:
            logger.info(f"[DIAG] No date/timing fields found in market response. Keys: {all_keys[:20]}")

    today_dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    fresh_markets = []
    stale_count = 0
    for m in markets:
        slug = m.get("slug", "")
        if not slug:
            continue
        game_date = parse_date_from_slug(slug)
        if game_date and game_date < today_dt:
            stale_count += 1
            continue
        if m.get("closed") or m.get("ep3Status") == "EXPIRED":
            stale_count += 1
            continue
        # Filter by market state if available
        state_val = m.get("state", "").upper()
        if state_val and state_val not in ("", "MARKET_STATE_OPEN"):
            stale_count += 1
            continue
        fresh_markets.append(m)

    if stale_count > 0:
        logger.info(f"📅 Filtered out {stale_count} stale/closed/suspended markets")

    markets_to_check = fresh_markets[:MAX_MARKETS]
    total_vol = 0.0
    loaded = 0
    skipped = 0

    for m in markets_to_check:
        slug = m.get("slug", "")
        if slug in STATE.blacklist:
            skipped += 1
            continue

        # Volume — use the canonical field name first (volume24hr per docs)
        vol = _extract_volume(m, slug, "listing")
        if vol == 0:
            # Fallback: Fetch single market details (listing may omit volume)
            details = CLIENT.get_market_details(slug)
            if details:
                vol = _extract_volume(details, slug, "details")
                if vol == 0 and loaded == 0 and DEBUG:
                    # Write first market's full detail response to file for debugging
                    try:
                        diag_json = json.dumps(details, default=str, indent=2)
                        diag_path = "monitor_vol_debug.json"
                        with open(diag_path, "w") as f:
                            f.write(diag_json)
                        logger.info(f"[DIAG] Full details for {slug} written to {diag_path} ({len(diag_json)} bytes)")
                        logger.info(f"[DIAG] Details keys: {list(details.keys()) if hasattr(details, 'keys') else 'N/A'}")
                    except Exception as e:
                        logger.info(f"[DIAG] Details response dump failed: {e}")
        total_vol += vol

        # Quick spread sanity check from listing data (WebSocket handles real-time BBO)
        bid_price, ask_price = None, None
        op = m.get("outcomePrices")
        if op:
            try:
                prices = json.loads(op) if isinstance(op, str) else op
                if prices and len(prices) >= 1:
                    p = float(prices[0])
                    if 0 < p < 1:
                        bid_price = p - 0.005
                        ask_price = p + 0.005
            except:
                pass

        bb = extract_amount_value(m.get("bestBid"))
        ba = extract_amount_value(m.get("bestAsk"))
        if bb and ba and 0 < bb < 1 and 0 < ba < 1:
            bid_price, ask_price = bb, ba

        if bid_price and ask_price and bid_price > 0:
            spread_pct = (ask_price - bid_price) / bid_price
            if spread_pct > MAX_SPREAD_PCT:
                skipped += 1
                continue

        STATE.meta[slug] = {
            "question": m.get("question") or m.get("title") or "",
            "event_slug": (m.get("events", [{}])[0].get("slug", "")
                           if m.get("events") else m.get("eventSlug", "")),
            "volume24h": vol,
            "category": m.get("category", ""),
            "market_type": m.get("marketType", ""),
            "shares_traded": extract_amount_value(m.get("sharesTraded", 0)),  # New: Store initial
            "start_date": m.get("startDate") or m.get("start_date") or "",
            "end_date": m.get("endDate") or m.get("end_date") or "",
            "game_id": m.get("gameId") or m.get("game_id") or "",
            "game_start_time": m.get("gameStartTime") or "",
        }
        loaded += 1

        if loaded % 100 == 0:
            logger.info(f"  ... catalogued {loaded} markets")

    if skipped > 0:
        logger.info(f"⏸️ Skipped {skipped} blacklisted/wide-spread markets")

    STATE.last_refresh = now
    top_vol = max((m.get("volume24h", 0) for m in STATE.meta.values()), default=0)
    vol_nonzero = sum(1 for m in STATE.meta.values() if m.get("volume24h", 0) > 0)
    oi_nonzero = sum(1 for m in STATE.meta.values() if m.get("open_interest", 0) > 0)
    logger.info(f"✅ Loaded {len(STATE.meta)} markets | Total 24h vol: ${total_vol:,.0f} | Top: ${top_vol:,.0f} | "
                f"vol>0: {vol_nonzero}/{len(STATE.meta)} | OI>0: {oi_nonzero}/{len(STATE.meta)} (from WS)")

# -------------------- Math & Signal Logic --------------------
def zscore(h: deque, mid: float) -> float:
    if len(h) < 10:
        return 0.0
    mu, sigma = mean(h), pstdev(h)
    return (mid - mu) / sigma if sigma > 1e-9 else 0.0

def adaptive_z() -> float:
    if len(STATE.global_deltas) < 50:
        return BASE_Z_SCORE_MIN
    recent = list(STATE.global_deltas)[-50:]
    baseline = pstdev(STATE.global_deltas) if len(STATE.global_deltas) > 100 else 1.0
    if baseline == 0:
        return BASE_Z_SCORE_MIN
    ratio = (pstdev(recent) if len(recent) >= 2 else 1.0) / baseline
    if ratio > 1.3:
        return max(1.1, BASE_Z_SCORE_MIN - 0.3)
    elif ratio < 0.7:
        return BASE_Z_SCORE_MIN + 0.45
    return BASE_Z_SCORE_MIN

def percentile(val: float, dq: deque) -> float:
    if len(dq) < 50:
        return 0.0
    return round(100 * sum(1 for x in sorted(dq) if x <= val) / len(dq), 2)

def severity(z: float) -> str:
    if z >= ALERT_Z:
        return "ALERT"
    if z >= WATCH_Z:
        return "WATCH"
    return "INFO"

def trade_hint(abs_z: float, spread: Optional[float], vol: float) -> Tuple[str, float]:
    if spread is None:
        return "", 0.0
    
    # FADE (mean reversion)
    if FADE_Z_MIN <= abs_z <= FADE_Z_MAX:
        if spread <= SPREAD_MAX_SAFE:
            conf = (abs_z - FADE_Z_MIN) / (FADE_Z_MAX - FADE_Z_MIN)
            return "FADE", min(1.0, conf)
    
    # TREND (momentum)
    if abs_z >= TREND_Z_MIN:
        if spread <= SPREAD_MAX_TREND:
            conf = (abs_z - TREND_Z_MIN) / (FADE_Z_MAX - TREND_Z_MIN)
            return "TREND", min(1.0, conf)
    
    return "", 0.0

def detect_burst(slug: str, ts: int, sig: str, z: float) -> str:
    prev = STATE.last_spike.get(slug)
    STATE.last_spike[slug] = {"ts": ts, "sig": sig, "z": z}
    if prev and sig != prev["sig"] and abs(z) >= 4.5 and (ts - prev["ts"]) <= 300:
        return "MEAN_REVERSION"
    return ""

def update_regime():
    if len(STATE.spike_outcomes) < 30:
        return
    cont = sum(1 for o in STATE.spike_outcomes if o == "CONTINUED")
    STATE.current_regime = "TRENDING" if cont / len(STATE.spike_outcomes) > 0.65 else "MEAN_REVERT"

# -------------------- Process Update (core — called by WebSocket handler) --------------------
def process_bbo_update(slug: str, best_bid_raw, best_ask_raw, market_state: str = "", shares_traded_raw=None, open_interest_raw=None):
    """Core BBO processing — called by WebSocket on_message."""
    meta = STATE.meta.get(slug)
    if not meta:
        return

    # Filter suspended/expired markets from the stream
    if market_state and market_state not in ("", "MARKET_STATE_OPEN"):
        return

    best_bid = extract_amount_value(best_bid_raw)
    best_ask = extract_amount_value(best_ask_raw)

    if not (best_bid and best_ask and 0 < best_bid < 1 and 0 < best_ask < 1):
        if DEBUG:
            logger.debug(f"[BBO] {slug} — unparseable bid={best_bid_raw} ask={best_ask_raw}")
        return

    mid = (best_bid + best_ask) / 2
    spread = best_ask - best_bid

    if not (TAIL_MIN <= mid <= TAIL_MAX):
        return

    ts_epoch = int(time.time())
    STATE.mid_cache[slug] = (mid, time.time())
    STATE.last_msg_time = time.time()
    STATE.ws_updates_processed += 1

    if spread > 0:
        if slug not in STATE.spread_cache:
            STATE.spread_cache[slug] = deque(maxlen=10)
        STATE.spread_cache[slug].append(spread)

    # Gate: skip wide-spread markets from signal pipeline entirely.
    # They still get mid_cache updates (for price tracking) but don't
    # contaminate global_deltas or generate signals/CSV rows.
    if spread > MAX_SPREAD_PCT:
        return

    shares_traded = extract_amount_value(shares_traded_raw) if shares_traded_raw is not None else None
    if shares_traded is not None:
        prev_shares = STATE.meta.get(slug, {}).get("shares_traded", 0)
        STATE.meta[slug]["shares_traded"] = shares_traded
        shares_delta = shares_traded - prev_shares
        if DEBUG:
            logger.debug(f"[BBO] {slug} shares_traded={shares_traded} delta={shares_delta}")

    oi = extract_amount_value(open_interest_raw) if open_interest_raw is not None else None
    if oi is not None:
        prev_oi = STATE.meta[slug].get("open_interest")
        STATE.meta[slug]["open_interest"] = oi
        if prev_oi is None and oi > 0:
            logger.info(f"[OI] {slug} openInterest={oi:.0f} (first capture from WS)")

    if slug not in STATE.hist:
        STATE.hist[slug] = deque(maxlen=HISTORY_LEN)
    h = STATE.hist[slug]
    prev_mid = h[-2] if len(h) >= 2 else None
    mid_before = h[-1] if h else mid
    delta = mid - mid_before
    h.append(mid)
    STATE.global_deltas.append(abs(delta))

    z = zscore(h, mid)
    az = adaptive_z()
    curr_spread = (STATE.spread_cache[slug][-1]
                   if slug in STATE.spread_cache and STATE.spread_cache[slug]
                   else spread)

    if DEBUG and abs(delta) > 0 and abs(z) > 0:
        logger.debug(f"  {slug[:30]} mid={mid:.4f} Δ={delta:+.5f} z={z:+.3f} az={az:.2f} "
                     f"hist={len(h)} gate={'PASS' if abs(delta) >= BASE_SPIKE_THRESHOLD and abs(z) >= az else 'FAIL'}")

    if abs(delta) < BASE_SPIKE_THRESHOLD or abs(z) < az:
        return

    pctl = percentile(abs(delta), STATE.global_deltas)
    if len(STATE.global_deltas) < WARMUP_GLOBAL_MIN:
        if abs(z) < (az + WARMUP_Z_EXTRA):
            return
        pctl = max(pctl, float(TOP_PERCENTILE))
    elif pctl < TOP_PERCENTILE:
        return

    sig = "SPIKE" if delta > 0 else "DIP"
    sev = severity(abs(z))
    burst = detect_burst(slug, ts_epoch, sig, z)
    dirZ = delta * abs(z)

    STATE.recent_signals.append((ts_epoch, slug, sig, z))
    if burst:
        STATE.spike_outcomes.append("REVERTED" if burst == "MEAN_REVERSION" else "CONTINUED")  # Add outcome

    STATE.regime_counter += 1
    if STATE.regime_counter >= REGIME_UPDATE_EVERY:
        update_regime()
        STATE.regime_counter = 0

    vol = meta.get("volume24h", 0)
    shares = meta.get("shares_traded", 0) or 0
    oi = meta.get("open_interest", 0) or 0
    # Liquidity proxy: REST volume > WS sharesTraded > WS openInterest
    effective_vol = vol if vol > 0 else (shares if shares > SHARES_ACTIVITY_MIN else oi)
    hint, conf = trade_hint(abs(z), curr_spread, effective_vol)

    decision, reason = "REJECT", ""
    if hint:
        if effective_vol < V24_MIN:
            reason = "REJECT_VOLUME"
        elif not (MID_MIN <= mid <= MID_MAX):
            reason = "REJECT_BOUNDS"
        elif STATE.cooldown.get(slug, 0) > time.time():
            reason = "REJECT_COOLDOWN"
        else:
            decision, reason = "ACCEPT", f"ACCEPT_{hint}"
            STATE.cooldown[slug] = time.time() + COOLDOWN_PER_SLUG
    else:
        reason = "WEAK_SIGNAL"

    if DEBUG:
        logger.info(f"[DECISION] {slug} effective_vol={effective_vol} (vol={vol} shares={shares} oi={oi}) hint={hint} decision={decision} reason={reason}")

    pos_size = POSITION_SIZE_FACTOR * conf if decision == "ACCEPT" else 0.0
    game_phase = classify_game_phase(meta, slug)
    pm_data = PM_SCORE_CACHE.get(slug) if PM_SCORE_CACHE else None
    pm_period = pm_data.get('period', 0) if pm_data else 0
    pm_score_diff = pm_data.get('score_diff', '') if pm_data else ''

    with CSV_LOCK:
        write_csv_row(TRIGGERS_CSV, TRIGGER_HEADER, {
            "ts_epoch": ts_epoch, "market_slug": slug, "event": meta["question"], "signal": sig,
            "mid": f"{mid:.5f}", "delta": f"{delta:.5f}", "abs_z": f"{abs(z):.5f}",
            "direction_strength": f"{dirZ:.6f}", "volume": f"{effective_vol:.0f}",
            "hint_candidate": hint, "decision": decision, "reason": reason,
            "regime": STATE.current_regime, "spread": f"{curr_spread:.5f}" if curr_spread else "",
            "pos_size_hint": f"{pos_size:.4f}", "game_phase": game_phase,
            "pm_period": str(pm_period) if pm_period else "",
            "pm_score_diff": str(pm_score_diff) if pm_score_diff != '' else "",
        })

        write_csv_row(OUTLIER_CSV, OUT_HEADER, {
            "ts_iso": iso(ts_epoch), "ts_epoch": str(ts_epoch), "event": meta["question"],
            "market_slug": slug,
            "event_url": f"https://polymarket.us/event/{meta.get('event_slug', '')}",
            "market_url": f"https://polymarket.us/market/{slug}",
            "price_prev": f"{prev_mid:.5f}" if prev_mid else "", "mid_before": f"{mid_before:.5f}",
            "mid": f"{mid:.5f}", "delta": f"{delta:.5f}", "abs_delta": f"{abs(delta):.5f}",
            "z": f"{z:.2f}", "abs_z": f"{abs(z):.2f}", "direction_strength": f"{dirZ:.6f}",
            "signal": sig, "volume": f"{effective_vol:.0f}",
            "best_bid": f"{best_bid:.4f}" if best_bid else "",
            "best_ask": f"{best_ask:.4f}" if best_ask else "",
            "spread": f"{curr_spread:.5f}" if curr_spread else "",
            "percentile": f"{pctl:.0f}%", "severity": sev, "burst": burst,
            "trade_hint": hint, "regime": STATE.current_regime,
            "game_phase": game_phase,
            "pm_period": str(pm_period) if pm_period else "",
            "pm_score_diff": str(pm_score_diff) if pm_score_diff != '' else "",
        })

    arr = "↑" if sig == "SPIKE" else "↓"
    icon = "📈" if STATE.current_regime == "TRENDING" else "📉"
    logger.info(f"{sev} {sig} {arr} {icon} {meta['question'][:50]} | z={z:+.2f} p{pctl:.0f}% "
                f"hint={hint or '-'} -> {decision}")

# -------------------- WebSocket Stream --------------------
class MarketWebSocket:
    """WebSocket client for real-time BBO via SUBSCRIPTION_TYPE_MARKET_DATA_LITE."""

    def __init__(self, key_id: str, secret_key: str):
        self.key_id = key_id
        self.secret_key = secret_key
        self._private_key = None
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._reconnect_delay = WS_RECONNECT_BASE_SEC
        self._subscribed_slugs: Set[str] = set()
        self._wildcard_subscribed: bool = False
        self._req_counter: int = 0

        if key_id and secret_key:
            try:
                from cryptography.hazmat.primitives.asymmetric import ed25519
                key_bytes = base64.b64decode(secret_key)
                self._private_key = ed25519.Ed25519PrivateKey.from_private_bytes(key_bytes[:32])
            except Exception as e:
                logger.error(f"WS key load failed: {e}")

    def _sign(self, method: str, path: str, timestamp_ms: str) -> str:
        message = f"{timestamp_ms}{method}{path}"
        sig = self._private_key.sign(message.encode('utf-8'))
        return base64.b64encode(sig).decode('utf-8')

    def _get_ws_headers(self) -> list:
        """Build auth headers for the WebSocket handshake (same format as REST)."""
        ts = str(int(time.time() * 1000))
        sig = self._sign("GET", "/v1/ws/markets", ts)
        return [
            f"X-PM-Access-Key: {self.key_id}",
            f"X-PM-Timestamp: {ts}",
            f"X-PM-Signature: {sig}",
        ]

    def _on_open(self, ws):
        STATE.ws_connected = True
        self._reconnect_delay = WS_RECONNECT_BASE_SEC
        logger.info("🔌 WebSocket connected")
        self._subscribe_all()

    def _next_req_id(self) -> str:
        self._req_counter += 1
        return f"sub_{self._req_counter}"

    def _subscribe_all(self):
        """Subscribe to all market data via wildcard (empty market_slugs=[])."""
        msg = {
            "subscribe": {
                "request_id": self._next_req_id(),
                "subscription_type": 2,
                "market_slugs": [],
            }
        }
        try:
            self._ws.send(json.dumps(msg))
            self._wildcard_subscribed = True
            logger.info("📡 Subscribed to ALL markets via wildcard (market_slugs: [])")
        except Exception as e:
            logger.warning(f"WS wildcard subscribe failed: {e}, falling back to batched")
            self._wildcard_subscribed = False
            self._subscribe_batched()

    def _subscribe_batched(self):
        """Fallback: subscribe in batches of 100 if wildcard fails."""
        slugs = list(STATE.meta.keys())
        if not slugs:
            return
        batch_size = 100
        for i in range(0, len(slugs), batch_size):
            batch = slugs[i:i + batch_size]
            msg = {
                "subscribe": {
                    "request_id": self._next_req_id(),
                    "subscription_type": 2,
                    "market_slugs": batch,
                }
            }
            try:
                self._ws.send(json.dumps(msg))
                self._subscribed_slugs.update(batch)
            except Exception as e:
                logger.warning(f"WS subscribe batch failed: {e}")
            time.sleep(0.05)
        logger.info(f"📡 Subscribed to {len(self._subscribed_slugs)} markets via batched fallback")

    def subscribe_new(self, slugs: List[str]):
        """No-op when wildcard is active (already receiving all markets)."""
        if getattr(self, '_wildcard_subscribed', False):
            return
        new_slugs = [s for s in slugs if s not in self._subscribed_slugs]
        if not new_slugs or not self._ws:
            return
        batch_size = 100
        for i in range(0, len(new_slugs), batch_size):
            batch = new_slugs[i:i + batch_size]
            msg = {
                "subscribe": {
                    "request_id": self._next_req_id(),
                    "subscription_type": 2,
                    "market_slugs": batch,
                }
            }
            try:
                self._ws.send(json.dumps(msg))
                self._subscribed_slugs.update(batch)
            except Exception as e:
                logger.warning(f"WS subscribe-new failed: {e}")
            time.sleep(0.05)
        if new_slugs:
            logger.info(f"📡 Subscribed to {len(new_slugs)} new markets")

    def _on_message(self, ws, raw_msg: str):
        STATE.ws_messages_total += 1
        try:
            msg = json.loads(raw_msg)
        except json.JSONDecodeError:
            return

        if not isinstance(msg, dict):
            return

        # --- Debug: log first N messages to diagnose format issues ---
        # Debug: log first 10 raw WS messages when DEBUG=1
        if DEBUG and STATE.ws_messages_total <= 10:
            truncated = json.dumps(msg)[:600]
            logger.info(f"[WS_DEBUG] msg #{STATE.ws_messages_total}: {truncated}")

        # --- Heartbeat ---
        if "heartbeat" in msg:
            return

        # --- Subscription confirmation / snapshot (has request_id) ---
        if "request_id" in msg:
            if "error" in msg:
                logger.warning(f"WS subscription error: {msg['error']}")
            return

        # ---- MARKET_DATA_LITE subscription update ----
        # Expected format (snake_case):
        #   {"market_data_lite_subscription_update": {"market_slug": "...", "best_bid": ..., "best_ask": ..., ...}}
        # OR batch:
        #   {"market_data_lite_subscription_update": [{"market_slug": ..., ...}, ...]}
        # Also handle camelCase variants just in case:
        #   {"marketDataLiteSubscriptionUpdate": ...}
        update_payload = (
            msg.get("market_data_lite_subscription_update")
            or msg.get("marketDataLiteSubscriptionUpdate")
            or msg.get("market_data_subscription_update")
            or msg.get("marketDataSubscriptionUpdate")
            or msg.get("market_data_lite")
            or msg.get("marketDataLite")
            or msg.get("market_data")
            or msg.get("marketData")
        )

        if update_payload is not None:
            if isinstance(update_payload, list):
                for item in update_payload:
                    if isinstance(item, dict):
                        self._handle_single_update(item)
            elif isinstance(update_payload, dict):
                self._handle_single_update(update_payload)
            return

        # --- Batch updates / data array ---
        updates = msg.get("updates") or msg.get("data")
        if isinstance(updates, list):
            for u in updates:
                if isinstance(u, dict):
                    self._handle_single_update(u)
            return

        # --- Flat single-market message (fallback) ---
        # Check for any key that indicates this is market data
        if any(k in msg for k in (
            "market_slug", "marketSlug", "slug",
            "best_bid", "bestBid", "best_ask", "bestAsk",
            "bids", "asks"
        )):
            self._handle_single_update(msg)
            return

        # --- Trade messages ---
        if "trade" in msg or msg.get("subscription_type") == 3:
            return  # Ignore trade messages for BBO processing

        if DEBUG:
            logger.debug(f"[WS] Unhandled message type: {list(msg.keys())[:5]}")

    def _handle_single_update(self, data: dict):
        # Extract slug — try snake_case first (API standard), then camelCase fallback
        slug = (
            data.get("market_slug")
            or data.get("marketSlug")
            or data.get("slug")
        )
        if not slug:
            return

        # Unwrap nested data if present (both naming conventions)
        inner = (
            data.get("market_data_lite")
            or data.get("marketDataLite")
            or data.get("market_data")
            or data.get("marketData")
            or data
        )
        market_state = (
            inner.get("state")
            or inner.get("market_state")
            or data.get("state")
            or data.get("market_state")
            or ""
        )

        # Extract BBO — try snake_case first, then camelCase
        best_bid = inner.get("best_bid") or inner.get("bestBid")
        best_ask = inner.get("best_ask") or inner.get("bestAsk")

        # Some responses embed BBO inside an "orderbook" or "bbo" field
        if best_bid is None and best_ask is None:
            bbo = inner.get("bbo") or inner.get("best_bid_ask")
            if isinstance(bbo, dict):
                best_bid = bbo.get("best_bid") or bbo.get("bestBid") or bbo.get("bid")
                best_ask = bbo.get("best_ask") or bbo.get("bestAsk") or bbo.get("ask")

        # Try to extract from bids/asks arrays (orderbook format)
        if best_bid is None and "bids" in inner:
            bids = inner["bids"]
            if isinstance(bids, list) and bids:
                top_bid = bids[0]
                if isinstance(top_bid, dict):
                    best_bid = top_bid.get("price") or top_bid.get("p")
                elif isinstance(top_bid, (list, tuple)) and top_bid:
                    best_bid = top_bid[0]
        if best_ask is None and "asks" in inner:
            asks = inner["asks"]
            if isinstance(asks, list) and asks:
                top_ask = asks[0]
                if isinstance(top_ask, dict):
                    best_ask = top_ask.get("price") or top_ask.get("p")
                elif isinstance(top_ask, (list, tuple)) and top_ask:
                    best_ask = top_ask[0]

        # Try price/mid fields as fallback for MARKET_DATA_LITE
        if best_bid is None and best_ask is None:
            price = inner.get("price") or inner.get("last_price") or inner.get("lastPrice")
            mid_val = inner.get("mid") or inner.get("midpoint")
            p = price or mid_val
            if p is not None:
                try:
                    pf = float(p)
                    if 0 < pf < 1:
                        # Synthesize a tight spread around the price
                        best_bid = pf - 0.005
                        best_ask = pf + 0.005
                except (ValueError, TypeError):
                    pass

        shares_traded = inner.get("shares_traded") or inner.get("sharesTraded")
        open_interest = inner.get("open_interest") or inner.get("openInterest")

        if best_bid is not None or best_ask is not None:
            process_bbo_update(slug, best_bid, best_ask, market_state, shares_traded, open_interest)

    def _on_error(self, ws, error):
        logger.warning(f"WS error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        STATE.ws_connected = False
        logger.warning(f"WS closed (code={close_status_code}): {close_msg}")

    def _run_forever(self):
        """Connect and reconnect loop with exponential backoff."""
        while not STOP.is_set():
            try:
                # Auth headers must be generated fresh for each connection attempt
                headers = self._get_ws_headers()
                self._ws = ws_lib.WebSocketApp(
                    US_WS_URL,
                    header=headers,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(
                    ping_interval=WS_PING_INTERVAL_SEC,
                    ping_timeout=10,
                )
            except Exception as e:
                logger.error(f"WS run error: {e}")

            if STOP.is_set():
                break

            STATE.ws_reconnects += 1
            self._subscribed_slugs.clear()
            self._wildcard_subscribed = False
            logger.info(f"🔄 WS reconnecting in {self._reconnect_delay:.0f}s (attempt #{STATE.ws_reconnects})")
            time.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, WS_RECONNECT_MAX_SEC)

    def start(self):
        self._thread = threading.Thread(target=self._run_forever, daemon=True, name="ws-stream")
        self._thread.start()

    def stop(self):
        if self._ws:
            try:
                self._ws.close()
            except:
                pass

# -------------------- Heartbeat --------------------
def heartbeat_thread():
    _hb_count = 0
    while not STOP.is_set():
        try:
            stats = STATE.get_stats()
            icon = "📈" if STATE.current_regime == "TRENDING" else "📉"
            ws_tag = "🟢" if stats['ws_connected'] else "🔴"
            logger.info(f"[LIVE] {icon} {STATE.current_regime} | mkts {stats['meta']} | "
                        f"mids {stats['mid_cache']} | Δs {stats['global_deltas']} | "
                        f"WS {ws_tag} msgs={stats['ws_msgs']} updates={stats['ws_updates']}")

            # Stale data warning
            if STATE.last_msg_time > 0 and (time.time() - STATE.last_msg_time) > 60:
                logger.warning(f"⚠️ No price data for {time.time() - STATE.last_msg_time:.0f}s")

            # Pipeline health every 5th heartbeat
            _hb_count += 1
            if _hb_count % 5 == 0 and STATE.hist:
                hist_lens = [len(h) for h in STATE.hist.values()]
                avg_hist = sum(hist_lens) / len(hist_lens) if hist_lens else 0
                ready_count = sum(1 for h in hist_lens if h >= 10)
                nonzero_deltas = sum(1 for d in STATE.global_deltas if d > 0)
                signals_count = len(STATE.recent_signals)
                oi_count = sum(1 for m in STATE.meta.values() if m.get("open_interest", 0) > 0)
                logger.info(f"[PIPE] avg_hist={avg_hist:.0f}/{HISTORY_LEN} | "
                            f"ready={ready_count}/{len(hist_lens)} | "
                            f"nonzero_Δ={nonzero_deltas}/{len(STATE.global_deltas)} | "
                            f"signals={signals_count} | OI>0={oi_count}/{len(STATE.meta)}")
        except Exception as e:
            logger.error(f"HB error: {e}")
        time.sleep(HEARTBEAT)

# -------------------- Background Refresh --------------------
def refresh_thread(ws_client: Optional['MarketWebSocket'] = None):
    """Periodically re-discover markets and subscribe new ones on the WebSocket."""
    while not STOP.is_set():
        try:
            time.sleep(MARKET_REFRESH_SEC)
            if STOP.is_set():
                break
            old_slugs = set(STATE.meta.keys())
            discover(refresh=True)
            new_slugs = set(STATE.meta.keys()) - old_slugs
            if ws_client and new_slugs:
                ws_client.subscribe_new(list(new_slugs))
        except Exception as e:
            logger.error(f"Refresh error: {e}")

# -------------------- Volume Refresh --------------------
def volume_refresh_thread():
    while not STOP.is_set():
        try:
            time.sleep(VOLUME_REFRESH_SEC)
            if STOP.is_set():
                break
            markets = CLIENT.get_markets(limit=MAX_MARKETS, active=True, closed=False)
            updated = 0
            nonzero = 0
            for m in markets:
                slug = _safe_get(m, "slug", "")
                if slug in STATE.meta:
                    vol = _extract_volume(m, slug, "refresh")
                    if vol == 0:
                        details = CLIENT.get_market_details(slug)
                        if details:
                            vol = _extract_volume(details, slug, "refresh_details")
                    STATE.meta[slug]["volume24h"] = vol
                    updated += 1
                    if vol > 0:
                        nonzero += 1
            oi_count = sum(1 for m in STATE.meta.values() if m.get("open_interest", 0) > 0)
            logger.info(f"🔄 Volume refresh: {nonzero}/{updated} REST vol>0 | {oi_count}/{len(STATE.meta)} WS OI>0")
        except Exception as e:
            logger.error(f"Volume refresh error: {e}")

# -------------------- Polymarket Score Refresh --------------------
def pm_score_refresh_thread():
    """Background thread: refresh Polymarket native score data every PM_SCORE_REFRESH_SEC."""
    # Initial fetch is done synchronously in main() before WebSocket starts.
    # This thread handles subsequent periodic refreshes only.
    while not STOP.is_set():
        try:
            if STATE.meta and CLIENT:
                PM_SCORE_CACHE.refresh(STATE.meta, CLIENT)
                log_blowout_opportunities()
        except Exception as e:
            logger.error(f"[PM-SCORE] Refresh error: {e}")
        for _ in range(PM_SCORE_REFRESH_SEC):
            if STOP.is_set():
                break
            time.sleep(1)

# -------------------- Cleanup --------------------
def cleanup_thread():
    while not STOP.is_set():
        try:
            time.sleep(300)
            now = time.time()
            for slug in [s for s, exp in STATE.cooldown.items() if exp < now]:
                del STATE.cooldown[slug]
            cutoff = now - 1800
            for slug in [s for s, (_, ts) in STATE.mid_cache.items() if ts < cutoff]:
                del STATE.mid_cache[slug]
            active = set(STATE.meta.keys())
            for d in [STATE.hist, STATE.spread_cache, STATE.last_spike]:
                for slug in [s for s in d.keys() if s not in active]:
                    del d[slug]
            gc.collect()
            STATE.last_cleanup = now
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

# -------------------- Main --------------------
def ws_wildcard_test(duration: int = 30):
    """Quick test: subscribe with empty market_slugs[] to see if server sends all markets."""
    import threading as _thr

    if not POLYMARKET_KEY_ID or not POLYMARKET_SECRET_KEY:
        logger.error("❌ Set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY")
        return

    if not HAS_WEBSOCKET:
        logger.error("❌ websocket-client not installed")
        return

    # First discover markets so we know the total count
    global CLIENT
    CLIENT = PolymarketUSClient(POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY)
    ensure_headers()
    discover(refresh=True)
    total_known = len(STATE.meta)
    logger.info(f"[WS-TEST] {total_known} markets discovered via REST")

    seen_slugs: set = set()
    msg_count = [0]
    errors = []
    sub_response = []

    def _sign(method, path, ts):
        key_bytes = base64.b64decode(POLYMARKET_SECRET_KEY)
        from cryptography.hazmat.primitives.asymmetric import ed25519
        pk = ed25519.Ed25519PrivateKey.from_private_bytes(key_bytes[:32])
        sig = pk.sign(f"{ts}{method}{path}".encode())
        return base64.b64encode(sig).decode()

    def on_open(ws):
        logger.info("[WS-TEST] Connected — sending wildcard subscribe (market_slugs: [])")
        msg = json.dumps({
            "subscribe": {
                "request_id": "wildcard_test",
                "subscription_type": 2,
                "market_slugs": [],
            }
        })
        ws.send(msg)
        logger.info(f"[WS-TEST] Sent: {msg}")

    def on_message(ws, raw):
        msg_count[0] += 1
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        if "heartbeat" in data:
            return

        # Extract slug from any known message format
        # Server may use camelCase (marketDataLite, requestId) or snake_case
        found = False
        for key in ("market_data_lite_subscription_update", "marketDataLiteSubscriptionUpdate",
                     "market_data_subscription_update", "marketDataSubscriptionUpdate",
                     "market_data_lite", "marketDataLite",
                     "market_data", "marketData",
                     "updates", "data"):
            payload = data.get(key)
            if payload is not None:
                items = payload if isinstance(payload, list) else [payload]
                for item in items:
                    if isinstance(item, dict):
                        s = item.get("market_slug") or item.get("marketSlug") or item.get("slug")
                        if s:
                            seen_slugs.add(s)
                            found = True
                break
        # Flat message fallback
        if not found:
            s = data.get("market_slug") or data.get("marketSlug") or data.get("slug")
            if s:
                seen_slugs.add(s)

        # Log first few BBO messages
        if msg_count[0] <= 5:
            logger.info(f"[WS-TEST] msg #{msg_count[0]}: {json.dumps(data)[:400]}")

    def on_error(ws, err):
        errors.append(str(err))
        logger.error(f"[WS-TEST] Error: {err}")

    def on_close(ws, code, msg):
        logger.info(f"[WS-TEST] Closed (code={code}): {msg}")

    ts = str(int(time.time() * 1000))
    sig = _sign("GET", "/v1/ws/markets", ts)
    headers = [
        f"X-PM-Access-Key: {POLYMARKET_KEY_ID}",
        f"X-PM-Timestamp: {ts}",
        f"X-PM-Signature: {sig}",
    ]

    ws = ws_lib.WebSocketApp(
        US_WS_URL, header=headers,
        on_open=on_open, on_message=on_message,
        on_error=on_error, on_close=on_close,
    )

    ws_thread = _thr.Thread(target=lambda: ws.run_forever(ping_interval=30, ping_timeout=10), daemon=True)
    ws_thread.start()

    logger.info(f"[WS-TEST] Listening for {duration}s...")
    for elapsed in range(duration):
        time.sleep(1)
        if elapsed > 0 and elapsed % 5 == 0:
            logger.info(f"[WS-TEST] {elapsed}s — {len(seen_slugs)} unique slugs, {msg_count[0]} messages")

    ws.close()
    time.sleep(0.5)

    # ---- Results ----
    logger.info("=" * 60)
    logger.info("[WS-TEST] WILDCARD SUBSCRIBE RESULTS")
    logger.info("=" * 60)
    logger.info(f"  Duration:         {duration}s")
    logger.info(f"  Total messages:   {msg_count[0]}")
    logger.info(f"  Unique slugs:     {len(seen_slugs)}")
    logger.info(f"  Known markets:    {total_known}")
    logger.info(f"  Errors:           {len(errors)}")

    if seen_slugs:
        pct = len(seen_slugs) / total_known * 100 if total_known else 0
        logger.info(f"  Coverage:         {pct:.1f}%")
        if len(seen_slugs) >= total_known * 0.8:
            logger.info("  VERDICT:          ✅ WILDCARD WORKS — empty slugs subscribes to all markets")
        elif len(seen_slugs) > 0:
            logger.info(f"  VERDICT:          ⚠️ PARTIAL — got {len(seen_slugs)}/{total_known}, may need longer listen or batched subs")
        # Write seen slugs to file for inspection
        with open("ws_test_slugs.txt", "w") as f:
            for s in sorted(seen_slugs):
                known_tag = "KNOWN" if s in STATE.meta else "NEW"
                f.write(f"{s}  [{known_tag}]\n")
        logger.info("  Slugs written to: ws_test_slugs.txt")
    else:
        logger.info("  VERDICT:          ❌ NO DATA — wildcard subscribe did not produce BBO updates")
        if sub_response:
            logger.info(f"  Sub response was: {json.dumps(sub_response[0])[:300]}")

    if CLIENT:
        CLIENT.close()


def run():
    global CLIENT

    if not POLYMARKET_KEY_ID or not POLYMARKET_SECRET_KEY:
        logger.error("❌ Set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY")
        logger.error("Generate at: https://polymarket.us/developer")
        return

    if not HAS_WEBSOCKET:
        logger.error("❌ websocket-client not installed. Run: pip install websocket-client")
        return

    CLIENT = PolymarketUSClient(POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY)

    logger.info("🔗 Testing connection...")
    try:
        b = CLIENT.get_balances()
        if b.get("balances"):
            logger.info(f"✅ Connected! Balance: ${b['balances'][0].get('currentBalance', 0):.2f}")
        else:
            logger.info("✅ Connected!")
    except Exception as e:
        logger.error(f"❌ Connection failed: {e}")
        return

    ensure_headers()
    discover(refresh=True)

    if not STATE.meta:
        logger.error("❌ No markets found!")
        return

    # Synchronous initial PM score fetch — must complete before WebSocket
    # starts firing signals, otherwise game_phase will be UNKNOWN
    global PM_SCORE_CACHE
    PM_SCORE_CACHE = PMScoreCache()
    if CLIENT:
        PM_SCORE_CACHE.refresh(STATE.meta, CLIENT)

    # Start WebSocket stream
    ws_client = MarketWebSocket(POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY)
    ws_client.start()

    # Start background threads
    threads = [
        threading.Thread(target=heartbeat_thread, daemon=True, name="heartbeat"),
        threading.Thread(target=refresh_thread, args=(ws_client,), daemon=True, name="refresh"),
        threading.Thread(target=volume_refresh_thread, daemon=True, name="volume_refresh"),
        threading.Thread(target=pm_score_refresh_thread, daemon=True, name="pm_score_refresh"),
        threading.Thread(target=cleanup_thread, daemon=True, name="cleanup"),
    ]
    for t in threads:
        t.start()

    import atexit
    atexit.register(lambda: (STOP.set(), ws_client.stop(), CLIENT.close() if CLIENT else None, gc.collect()))

    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())

    logger.info("=" * 60)
    logger.info("🇺🇸 POLYMARKET US CLOB HUNTER v4.0.0 STARTED")
    logger.info("=" * 60)
    logger.info(f"Monitoring {len(STATE.meta)} markets via WebSocket stream")

    while not STOP.is_set():
        time.sleep(1)

    ws_client.stop()
    logger.info("👋 Bye!")

if __name__ == "__main__":
    if "--ws-test" in sys.argv:
        # Quick test: does subscribing with empty market_slugs[] get all markets?
        dur = 30
        for arg in sys.argv:
            if arg.startswith("--duration="):
                dur = int(arg.split("=")[1])
        ws_wildcard_test(duration=dur)
    else:
        run()