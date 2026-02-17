#!/usr/bin/env python3
"""
POLYMARKET US ACTIVITY SCANNER v3.0

Monitors whether the FADE strategy would actually PROFIT right now — not
just whether signals exist, but whether spreads are tight enough to enter
AND whether price spikes are actually reverting (the core of FADE).

Three improvements over v2:
  1. Tighter spread threshold (4% WS spread, matching trade.py's 3% entry slippage)
  2. TREND filter (|z| >= 6 excluded — monitor marks these TREND, trade.py skips)
  3. Reversion tracking — after every z>=3.5 spike, checks 3min later if price
     reverted >50% toward pre-spike mean. Only alerts when reversion rate is decent.

Usage:
    . .\creds.ps1
    python scanner.py
"""

import os
import sys
import json
import time
import signal
import threading
import base64
import re
from collections import deque
from statistics import mean, pstdev
from datetime import datetime, timezone
from typing import Dict, Optional, List, Set, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import websocket as ws_lib
except ImportError:
    print("FATAL: pip install websocket-client")
    exit(1)

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

from cryptography.hazmat.primitives.asymmetric import ed25519

# -------------------- Console + Log File --------------------
_log_file = open("scanner-console-log.txt", "w", encoding="utf-8")
_print_lock = threading.Lock()

def tee_print(*args, **kwargs):
    """Print to both console and log file."""
    with _print_lock:
        print(*args, **kwargs)
        kwargs.pop("file", None)
        print(*args, file=_log_file, **kwargs)
        _log_file.flush()

# -------------------- Configuration --------------------
POLYMARKET_KEY_ID = os.getenv("POLYMARKET_KEY_ID", "")
POLYMARKET_SECRET_KEY = os.getenv("POLYMARKET_SECRET_KEY", "")

US_API_BASE = "https://api.polymarket.us"
US_WS_URL = "wss://api.polymarket.us/v1/ws/markets"

# Dashboard
DASHBOARD_INTERVAL_SEC = 30
TOP_SPIKES_SHOWN = 5

# ---------- Thresholds (match trade.py exactly) ----------
Z_TRADEABLE = 3.5          # trade.py Z_OPEN
Z_MAX_FADE = 6.0           # above this, monitor marks TREND -> trade.py FADE_ONLY rejects
Z_WATCH = 1.5              # monitor.py WATCH_Z
MIN_MID = 0.20             # trade.py MIN_MID_PRICE
MAX_MID = 0.55             # trade.py MAX_MID_PRICE
MAX_SPREAD_FADE = 0.04     # WS spread proxy for trade.py entry_slippage < 3% (TP_PCT/2)
MAX_SPREAD_BASE = 0.10     # trade.py MAX_SPREAD_BASE (for general "ready" metric)
HISTORY_LEN = 50           # monitor.py HISTORY_LEN for z-score window
MIN_WARMUP = 20            # min history samples before z-scores are reliable

# ---------- Reversion tracking ----------
REVERSION_CHECK_SEC = 180  # check spike outcome after 3 minutes
REVERSION_THRESHOLD = 0.50 # spike must revert >50% of its magnitude to count
REVERSION_WINDOW_SEC = 600 # rolling 10-min window for reversion rate

# ---------- Alert thresholds ----------
SCORE_HOT = 65
SCORE_FIRE = 85
ALERT_COOLDOWN_SEC = 300
# Reversion rate gate: don't alert unless enough spikes have reverted
MIN_REVERSION_RATE = 0.30  # at least 30% of tracked spikes must revert
MIN_CHECKED_SPIKES = 3     # need at least 3 checked spikes for reversion rate to matter

# Beep settings (Windows)
BEEP_FREQ_HOT = 1000
BEEP_DUR_HOT = 500
BEEP_FREQ_FIRE = 1500
BEEP_DUR_FIRE = 1000

# Metric weights (sum to 1.0)
WEIGHT_FADE_READY = 0.35   # FADE-eligible: z in 3.5-6, spread < 4%, mid in range
WEIGHT_REVERSION = 0.30    # are spikes actually reverting?
WEIGHT_VOLATILE = 0.15     # markets with z >= 1.5 (pipeline health)
WEIGHT_TIGHT = 0.20        # markets with spread < 4% (entry-slippage-safe)

# Infrastructure
MAX_MARKETS = 500
MARKET_REFRESH_SEC = 300
WS_PING_INTERVAL_SEC = 30
WS_RECONNECT_BASE_SEC = 1.0
WS_RECONNECT_MAX_SEC = 60.0

STOP = threading.Event()

# Module-level market metadata (timing info from REST discovery)
MARKET_META: Dict[str, dict] = {}

# -------------------- Helpers --------------------

def extract_amount_value(obj) -> Optional[float]:
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


def parse_date_from_slug(slug: str) -> Optional[datetime]:
    match = re.search(r'(\d{4}-\d{2}-\d{2})$', slug)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def classify_game_phase(meta: dict, slug: str) -> str:
    """Classify game phase: PRE_GAME, LIVE, POST_GAME, or UNKNOWN.

    Slug date takes priority for cross-day checks (API startDate is market
    creation time, not game time). API end_date used for same-day refinement.
    """
    from datetime import timedelta
    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    # Step 1: Slug date for coarse classification
    game_date = parse_date_from_slug(slug)
    if game_date:
        if game_date >= tomorrow_start:
            return "PRE_GAME"
        if game_date < today_start:
            return "POST_GAME"

    # Step 2: API end_date for same-day refinement
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

    if game_date:
        return "UNKNOWN"

    return "UNKNOWN"


def score_linear(value: float, brackets: List[Tuple[float, float]]) -> float:
    if value <= brackets[0][0]:
        return brackets[0][1]
    if value >= brackets[-1][0]:
        return brackets[-1][1]
    for i in range(len(brackets) - 1):
        lo_val, lo_score = brackets[i]
        hi_val, hi_score = brackets[i + 1]
        if lo_val <= value <= hi_val:
            t = (value - lo_val) / (hi_val - lo_val) if hi_val != lo_val else 0
            return lo_score + t * (hi_score - lo_score)
    return brackets[-1][1]


def score_bar(score: float, width: int = 24) -> str:
    filled = int(score / 100 * width)
    return "[" + "|" * filled + "." * (width - filled) + "]"


# -------------------- REST Client --------------------

class RestClient:
    def __init__(self, key_id: str, secret_key: str):
        self.key_id = key_id
        key_bytes = base64.b64decode(secret_key)
        self._private_key = ed25519.Ed25519PrivateKey.from_private_bytes(key_bytes[:32])
        self._session = requests.Session()
        retries = Retry(total=3, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries, pool_connections=5, pool_maxsize=5)
        self._session.mount("https://", adapter)
        self._session.headers.update({"User-Agent": "PolymarketScanner/3.0", "Accept": "application/json"})

    def _sign(self, method: str, path: str, timestamp_ms: str) -> str:
        message = f"{timestamp_ms}{method}{path}"
        sig = self._private_key.sign(message.encode("utf-8"))
        return base64.b64encode(sig).decode("utf-8")

    def _get_headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp": ts,
            "X-PM-Signature": self._sign(method, path, ts),
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        try:
            headers = self._get_headers("GET", path)
            resp = self._session.get(f"{US_API_BASE}{path}", headers=headers, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            tee_print(f"  API error {path}: {e}")
        return None

    def discover_markets(self) -> List[str]:
        all_markets = []
        offset = 0
        page_size = 100
        while len(all_markets) < MAX_MARKETS:
            data = self._get("/v1/markets", {
                "limit": str(page_size), "offset": str(offset),
                "active": "true", "closed": "false",
            })
            page = []
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

        today_dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        slugs = []
        for m in all_markets[:MAX_MARKETS]:
            slug = m.get("slug", "")
            if not slug:
                continue
            game_date = parse_date_from_slug(slug)
            if game_date and game_date < today_dt:
                continue
            if m.get("closed") or m.get("ep3Status") == "EXPIRED":
                continue
            state_val = m.get("state", "").upper()
            if state_val and state_val not in ("", "MARKET_STATE_OPEN"):
                continue
            slugs.append(slug)
            MARKET_META[slug] = {
                "start_date": m.get("startDate") or m.get("start_date") or "",
                "end_date": m.get("endDate") or m.get("end_date") or "",
                "game_id": m.get("gameId") or m.get("game_id") or "",
            }
        return slugs


# -------------------- Spike Reversion Tracker --------------------

class SpikeRecord:
    """Tracks a z-score spike and whether the price reverted."""
    __slots__ = ('time', 'slug', 'spike_mid', 'pre_mean', 'z_score',
                 'spread', 'checked', 'reverted', 'check_mid')

    def __init__(self, t: float, slug: str, spike_mid: float,
                 pre_mean: float, z_score: float, spread: float):
        self.time = t
        self.slug = slug
        self.spike_mid = spike_mid
        self.pre_mean = pre_mean
        self.z_score = z_score
        self.spread = spread
        self.checked = False
        self.reverted = False
        self.check_mid = 0.0


# -------------------- Market Z-Score Tracker --------------------

class MarketState:
    __slots__ = ('history', 'last_mid', 'last_bid', 'last_ask', 'last_spread',
                 'last_oi', 'peak_z', 'peak_z_time', 'last_update')

    def __init__(self):
        self.history: deque = deque(maxlen=HISTORY_LEN)
        self.last_mid: float = 0.0
        self.last_bid: float = 0.0
        self.last_ask: float = 0.0
        self.last_spread: float = 999.0
        self.last_oi: float = 0.0
        self.peak_z: float = 0.0
        self.peak_z_time: float = 0.0
        self.last_update: float = 0.0


class ActivityTracker:
    """Runs a mini z-score pipeline + reversion tracking."""

    def __init__(self):
        self._lock = threading.Lock()
        self._markets: Dict[str, MarketState] = {}
        # Recent FADE-eligible spikes (for dashboard display)
        self._recent_spikes: deque = deque(maxlen=50)
        # Spike reversion tracking
        self._spike_records: deque = deque(maxlen=200)
        self._update_count = 0

    def record_update(self, slug: str, bid: float, ask: float, oi: float):
        now = time.time()
        mid = (bid + ask) / 2.0
        if not (0 < mid < 1):
            return

        spread_pct = (ask - bid) / bid if bid > 0 else 999.0

        with self._lock:
            self._update_count += 1
            ms = self._markets.get(slug)
            if ms is None:
                ms = MarketState()
                self._markets[slug] = ms

            ms.last_bid = bid
            ms.last_ask = ask
            ms.last_spread = spread_pct
            ms.last_oi = oi
            ms.last_update = now

            prev_mid = ms.last_mid
            ms.last_mid = mid

            if prev_mid > 0:
                ms.history.append(mid)

                if len(ms.history) >= MIN_WARMUP:
                    h = list(ms.history)
                    m_val = mean(h)
                    s_val = pstdev(h, m_val)
                    if s_val > 1e-9:
                        z = (mid - m_val) / s_val
                        abs_z = abs(z)

                        # Track peak z for this market (decays after 60s)
                        if abs_z > abs(ms.peak_z) or (now - ms.peak_z_time > 60):
                            ms.peak_z = z
                            ms.peak_z_time = now

                        # Log FADE-eligible spikes (z in FADE range, not TREND)
                        is_fade_z = Z_TRADEABLE <= abs_z < Z_MAX_FADE
                        mid_ok = MIN_MID <= mid <= MAX_MID
                        spread_ok = spread_pct < MAX_SPREAD_FADE

                        if is_fade_z and mid_ok and spread_ok:
                            self._recent_spikes.append((now, slug, z, mid, spread_pct))
                            # Create reversion record to track outcome
                            self._spike_records.append(SpikeRecord(
                                t=now, slug=slug, spike_mid=mid,
                                pre_mean=m_val, z_score=z, spread=spread_pct,
                            ))

                        # Also log wider spikes for display (but mark them)
                        elif is_fade_z and mid_ok and spread_pct < MAX_SPREAD_BASE:
                            # Track for display only — spread too wide for real entry
                            self._recent_spikes.append((now, slug, z, mid, spread_pct))
            else:
                ms.history.append(mid)

    def _check_reversions(self, now: float):
        """Check old spike records to see if price reverted. Called under lock."""
        for rec in self._spike_records:
            if rec.checked:
                continue
            age = now - rec.time
            if age < REVERSION_CHECK_SEC:
                continue  # too soon to check
            # Check current mid for this market
            ms = self._markets.get(rec.slug)
            if ms is None or ms.last_mid <= 0:
                rec.checked = True
                rec.reverted = False
                continue

            rec.checked = True
            rec.check_mid = ms.last_mid

            # How far did the spike deviate from the mean?
            spike_deviation = rec.spike_mid - rec.pre_mean
            if abs(spike_deviation) < 1e-9:
                rec.reverted = False
                continue

            # How much has it reverted? (current mid closer to pre_mean than spike)
            current_deviation = rec.check_mid - rec.pre_mean
            # Reversion = moving back toward pre_mean
            # If spike went UP (deviation > 0), reversion means current < spike
            # If spike went DOWN (deviation < 0), reversion means current > spike
            reversion_amount = spike_deviation - current_deviation
            reversion_pct = reversion_amount / spike_deviation if abs(spike_deviation) > 1e-9 else 0

            rec.reverted = reversion_pct >= REVERSION_THRESHOLD

    def get_metrics(self) -> dict:
        now = time.time()
        spike_window = 300  # 5-min window for recent spikes

        with self._lock:
            # Check any pending reversion records
            self._check_reversions(now)

            total_markets = len(self._markets)
            warmed_up = 0
            ready = 0
            volatile = 0
            fade_ready = 0     # FADE-eligible: z 3.5-6, spread < 4%, mid in range
            tight_entry = 0
            total_oi = 0.0
            # Game phase counts for FADE-ready markets
            fade_phase_live = 0
            fade_phase_pre = 0
            fade_phase_unknown = 0

            for slug, ms in self._markets.items():
                total_oi += ms.last_oi
                n_hist = len(ms.history)

                if n_hist >= MIN_WARMUP:
                    warmed_up += 1
                    if ms.last_spread < MAX_SPREAD_BASE:
                        ready += 1

                if ms.last_spread < MAX_SPREAD_FADE:
                    tight_entry += 1

                # Check peak z within the last 60s
                if now - ms.peak_z_time < 60 and n_hist >= MIN_WARMUP:
                    abs_z = abs(ms.peak_z)
                    if abs_z >= Z_WATCH:
                        volatile += 1
                    # FADE-ready: z in FADE range, NOT trend, tight spread, mid ok
                    if (Z_TRADEABLE <= abs_z < Z_MAX_FADE
                            and MIN_MID <= ms.last_mid <= MAX_MID
                            and ms.last_spread < MAX_SPREAD_FADE):
                        fade_ready += 1
                        # Track game phase of FADE-ready markets
                        meta = MARKET_META.get(slug, {})
                        phase = classify_game_phase(meta, slug)
                        if phase == "LIVE":
                            fade_phase_live += 1
                        elif phase == "PRE_GAME":
                            fade_phase_pre += 1
                        else:
                            fade_phase_unknown += 1

            # Reversion rate from recent checked spikes
            recent_records = [r for r in self._spike_records if now - r.time < REVERSION_WINDOW_SEC]
            checked_records = [r for r in recent_records if r.checked]
            reverted_records = [r for r in checked_records if r.reverted]
            total_checked = len(checked_records)
            total_reverted = len(reverted_records)
            reversion_rate = total_reverted / total_checked if total_checked > 0 else 0.0
            pending_checks = len(recent_records) - total_checked

            # Recent FADE-eligible spikes for display (last 5 min, most recent first)
            recent_list = [(t, s, z, m, sp) for t, s, z, m, sp in self._recent_spikes if now - t < spike_window]
            recent_list.sort(key=lambda x: x[0], reverse=True)
            recent_spike_count = len(recent_list)

        # Score each metric
        fade_ready_score = score_linear(fade_ready, [
            (0, 0), (1, 35), (2, 60), (3, 80), (5, 95), (8, 100),
        ])

        # Reversion score: 0% reverted = 0, 30% = 40, 50% = 70, 70%+ = 100
        reversion_score = score_linear(reversion_rate * 100, [
            (0, 0), (15, 15), (30, 40), (50, 70), (70, 95), (100, 100),
        ])
        # If not enough checked spikes yet, reversion score is neutral (50)
        if total_checked < MIN_CHECKED_SPIKES:
            reversion_score = 50.0 if pending_checks > 0 else 0.0

        volatile_score = score_linear(volatile, [
            (0, 0), (2, 15), (5, 35), (10, 55), (20, 80), (30, 100),
        ])
        tight_score = score_linear(tight_entry, [
            (0, 0), (3, 20), (8, 45), (15, 70), (25, 90), (40, 100),
        ])

        composite = (
            WEIGHT_FADE_READY * fade_ready_score
            + WEIGHT_REVERSION * reversion_score
            + WEIGHT_VOLATILE * volatile_score
            + WEIGHT_TIGHT * tight_score
        )

        # Pre-game penalty: if ALL FADE-ready markets are PRE_GAME, apply 0.3x multiplier
        if fade_ready > 0 and fade_phase_live == 0 and fade_phase_unknown == 0:
            composite *= 0.3

        return {
            "total_markets": total_markets,
            "warmed_up": warmed_up,
            "ready": ready,
            "volatile": volatile,
            "fade_ready": fade_ready,
            "tight_entry": tight_entry,
            "total_oi": total_oi,
            "recent_spike_count": recent_spike_count,
            "recent_spikes": recent_list[:TOP_SPIKES_SHOWN],
            "fade_ready_score": fade_ready_score,
            "reversion_rate": reversion_rate,
            "reversion_score": reversion_score,
            "total_checked": total_checked,
            "total_reverted": total_reverted,
            "pending_checks": pending_checks,
            "volatile_score": volatile_score,
            "tight_score": tight_score,
            "composite": composite,
            "update_count": self._update_count,
            "fade_phase_live": fade_phase_live,
            "fade_phase_pre": fade_phase_pre,
            "fade_phase_unknown": fade_phase_unknown,
        }


# -------------------- WebSocket Stream --------------------

class WSStream:
    def __init__(self, key_id: str, secret_key: str, tracker: ActivityTracker):
        self.key_id = key_id
        self._private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(secret_key)[:32]
        )
        self._tracker = tracker
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._reconnect_delay = WS_RECONNECT_BASE_SEC
        self._subscribed: Set[str] = set()
        self._req_counter = 0
        self._slugs: List[str] = []
        self.connected = False
        self.reconnects = 0
        self.msg_count = 0

    def set_slugs(self, slugs: List[str]):
        self._slugs = slugs

    def _sign(self, method: str, path: str, ts: str) -> str:
        msg = f"{ts}{method}{path}"
        sig = self._private_key.sign(msg.encode("utf-8"))
        return base64.b64encode(sig).decode("utf-8")

    def _ws_headers(self) -> list:
        ts = str(int(time.time() * 1000))
        sig = self._sign("GET", "/v1/ws/markets", ts)
        return [
            f"X-PM-Access-Key: {self.key_id}",
            f"X-PM-Timestamp: {ts}",
            f"X-PM-Signature: {sig}",
        ]

    def _next_req_id(self) -> str:
        self._req_counter += 1
        return f"scan_{self._req_counter}"

    def _on_open(self, ws):
        self.connected = True
        self._reconnect_delay = WS_RECONNECT_BASE_SEC
        tee_print("  WS connected")
        self._subscribe_all()

    def _subscribe_all(self):
        slugs = self._slugs
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
                self._subscribed.update(batch)
            except Exception:
                pass
            time.sleep(0.05)
        tee_print(f"  Subscribed to {len(self._subscribed)} markets")

    def subscribe_new(self, slugs: List[str]):
        new = [s for s in slugs if s not in self._subscribed]
        if not new or not self._ws:
            return
        batch_size = 100
        for i in range(0, len(new), batch_size):
            batch = new[i:i + batch_size]
            msg = {
                "subscribe": {
                    "request_id": self._next_req_id(),
                    "subscription_type": 2,
                    "market_slugs": batch,
                }
            }
            try:
                self._ws.send(json.dumps(msg))
                self._subscribed.update(batch)
            except Exception:
                pass
            time.sleep(0.05)
        if new:
            tee_print(f"  Subscribed to {len(new)} new markets")

    def _on_message(self, ws, raw_msg: str):
        self.msg_count += 1
        try:
            msg = json.loads(raw_msg)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        if "heartbeat" in msg or "request_id" in msg:
            return

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
                        self._handle_update(item)
            elif isinstance(update_payload, dict):
                self._handle_update(update_payload)
            return

        updates = msg.get("updates") or msg.get("data")
        if isinstance(updates, list):
            for u in updates:
                if isinstance(u, dict):
                    self._handle_update(u)
            return

        if any(k in msg for k in ("market_slug", "marketSlug", "slug", "best_bid", "bestBid")):
            self._handle_update(msg)

    def _handle_update(self, data: dict):
        slug = data.get("market_slug") or data.get("marketSlug") or data.get("slug")
        if not slug:
            return

        inner = (
            data.get("market_data_lite") or data.get("marketDataLite")
            or data.get("market_data") or data.get("marketData")
            or data
        )

        best_bid = inner.get("best_bid") or inner.get("bestBid")
        best_ask = inner.get("best_ask") or inner.get("bestAsk")

        if best_bid is None and best_ask is None:
            bbo = inner.get("bbo") or inner.get("best_bid_ask")
            if isinstance(bbo, dict):
                best_bid = bbo.get("best_bid") or bbo.get("bestBid") or bbo.get("bid")
                best_ask = bbo.get("best_ask") or bbo.get("bestAsk") or bbo.get("ask")

        if best_bid is None and "bids" in inner:
            bids = inner["bids"]
            if isinstance(bids, list) and bids:
                top = bids[0]
                if isinstance(top, dict):
                    best_bid = top.get("price") or top.get("p")
                elif isinstance(top, (list, tuple)) and top:
                    best_bid = top[0]

        if best_ask is None and "asks" in inner:
            asks = inner["asks"]
            if isinstance(asks, list) and asks:
                top = asks[0]
                if isinstance(top, dict):
                    best_ask = top.get("price") or top.get("p")
                elif isinstance(top, (list, tuple)) and top:
                    best_ask = top[0]

        if best_bid is None and best_ask is None:
            p = inner.get("price") or inner.get("last_price") or inner.get("lastPrice") or inner.get("mid") or inner.get("midpoint")
            if p is not None:
                try:
                    pf = float(p)
                    if 0 < pf < 1:
                        best_bid = pf - 0.005
                        best_ask = pf + 0.005
                except (ValueError, TypeError):
                    pass

        open_interest = inner.get("open_interest") or inner.get("openInterest")

        bid = extract_amount_value(best_bid) or 0.0
        ask = extract_amount_value(best_ask) or 0.0
        oi = extract_amount_value(open_interest) or 0.0

        if bid > 0 and ask > 0 and ask > bid:
            self._tracker.record_update(slug, bid, ask, oi)

    def _on_error(self, ws, error):
        pass

    def _on_close(self, ws, code, msg):
        self.connected = False

    def _run_forever(self):
        while not STOP.is_set():
            try:
                headers = self._ws_headers()
                self._ws = ws_lib.WebSocketApp(
                    US_WS_URL,
                    header=headers,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=WS_PING_INTERVAL_SEC, ping_timeout=10)
            except Exception:
                pass
            if STOP.is_set():
                break
            self.reconnects += 1
            self._subscribed.clear()
            tee_print(f"  WS reconnecting in {self._reconnect_delay:.0f}s (#{self.reconnects})")
            time.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, WS_RECONNECT_MAX_SEC)

    def start(self):
        self._thread = threading.Thread(target=self._run_forever, daemon=True, name="ws-scan")
        self._thread.start()

    def stop(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass


# -------------------- Dashboard --------------------

def print_dashboard(metrics: dict, ws: WSStream):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ws_status = "CONNECTED" if ws.connected else "DISCONNECTED"

    composite = metrics["composite"]
    fade_ready = metrics["fade_ready"]
    reversion_rate = metrics["reversion_rate"]
    total_checked = metrics["total_checked"]
    total_reverted = metrics["total_reverted"]
    pending = metrics["pending_checks"]

    # Determine label based on conditions
    has_fade = fade_ready >= 1
    has_reversion = (total_checked >= MIN_CHECKED_SPIKES and reversion_rate >= MIN_REVERSION_RATE)
    not_enough_data = total_checked < MIN_CHECKED_SPIKES

    if composite >= SCORE_FIRE and has_fade and has_reversion:
        label = "*** FIRE -- START YOUR BOTS NOW ***"
    elif composite >= SCORE_HOT and has_fade and has_reversion:
        label = "*** HOT -- GOOD TIME TO RUN BOTS ***"
    elif composite >= SCORE_HOT and has_fade and not_enough_data:
        label = "(signals found, waiting for reversion data...)"
    elif has_fade and not has_reversion and total_checked >= MIN_CHECKED_SPIKES:
        label = "(signals but NOT reverting -- trending/game events)"
    elif composite >= 40:
        label = "(building up -- watch closely)"
    elif metrics["warmed_up"] < 10:
        label = "(warming up -- z-scores not ready yet)"
    else:
        label = "(quiet -- not worth trading)"

    tee_print()
    tee_print("=" * 72)
    tee_print(f"POLYMARKET SCANNER v3 | {now_str} UTC | WS: {ws_status} | msgs: {ws.msg_count}")
    tee_print("=" * 72)
    tee_print(f"  FADE-ready (z 3.5-6): {fade_ready:<3} mkts  {score_bar(metrics['fade_ready_score'])}  {metrics['fade_ready_score']:.0f}")

    # Reversion display
    if total_checked > 0:
        rev_pct_str = f"{reversion_rate*100:.0f}%"
        rev_detail = f"({total_reverted}/{total_checked} reverted"
        if pending > 0:
            rev_detail += f", {pending} pending"
        rev_detail += ")"
    elif pending > 0:
        rev_pct_str = "---"
        rev_detail = f"({pending} spikes pending check)"
    else:
        rev_pct_str = "n/a"
        rev_detail = "(no spikes yet)"
    tee_print(f"  Reversion rate:     {rev_pct_str:<5}       {score_bar(metrics['reversion_score'])}  {metrics['reversion_score']:.0f}  {rev_detail}")

    tee_print(f"  Volatile (z>=1.5):  {metrics['volatile']:<3} mkts  {score_bar(metrics['volatile_score'])}  {metrics['volatile_score']:.0f}")
    tee_print(f"  Tight spread (<4%): {metrics['tight_entry']:<3} mkts  {score_bar(metrics['tight_score'])}  {metrics['tight_score']:.0f}")
    tee_print(f"  Warmup: {metrics['warmed_up']}/{metrics['total_markets']} markets have {MIN_WARMUP}+ data points")
    if fade_ready > 0:
        tee_print(f"  Game phase:         {metrics['fade_phase_live']} live, {metrics['fade_phase_pre']} pre-game, {metrics['fade_phase_unknown']} unknown")
        if metrics['fade_phase_live'] == 0 and metrics['fade_phase_unknown'] == 0:
            tee_print(f"  ** Score penalized 0.3x (all FADE signals are pre-game)")
    tee_print("-" * 72)
    tee_print(f"  SCORE:  {composite:.0f} / 100   {label}")
    if metrics["recent_spike_count"] > 0:
        tee_print(f"  FADE-eligible spikes in last 5 min: {metrics['recent_spike_count']}")
    tee_print("-" * 72)

    spikes = metrics.get("recent_spikes", [])
    if spikes:
        tee_print(f"  Recent spikes (FADE-eligible: z {Z_TRADEABLE}-{Z_MAX_FADE}, spread<{MAX_SPREAD_FADE*100:.0f}%):")
        for ts, slug, z, mid_val, spread in spikes:
            age = time.time() - ts
            age_str = f"{age:.0f}s ago" if age < 60 else f"{age/60:.0f}m ago"
            display_slug = slug[:32] if len(slug) > 32 else slug
            direction = "SPIKE" if z > 0 else "DIP"
            entry_ok = "OK" if spread < MAX_SPREAD_FADE else "WIDE"
            tee_print(f"    {display_slug:<33} z={z:+.1f} {direction:<5} mid={mid_val:.3f} "
                      f"spread={spread*100:.1f}% {entry_ok} ({age_str})")
    else:
        tee_print(f"  No FADE-eligible spikes yet")
        tee_print(f"    Need: z {Z_TRADEABLE}-{Z_MAX_FADE}, mid {MIN_MID}-{MAX_MID}, spread<{MAX_SPREAD_FADE*100:.0f}%")

    tee_print("=" * 72)


def alert_if_needed(metrics: dict, state: dict):
    now = time.time()
    last = state.get("last_alert_ts", 0)
    if now - last < ALERT_COOLDOWN_SEC:
        return

    composite = metrics["composite"]
    fade_ready = metrics["fade_ready"]
    reversion_rate = metrics["reversion_rate"]
    total_checked = metrics["total_checked"]

    # Must have FADE-ready markets
    if fade_ready < 1:
        return

    # Must have evidence of reversion (or not enough data yet — don't alert)
    if total_checked >= MIN_CHECKED_SPIKES and reversion_rate < MIN_REVERSION_RATE:
        return  # spikes exist but not reverting — bad FADE conditions
    if total_checked < MIN_CHECKED_SPIKES:
        return  # not enough data to confirm reversion — wait

    if composite >= SCORE_FIRE and fade_ready >= 2:
        tee_print(f"\n  *** BEEP *** Score {composite:.0f}, {fade_ready} FADE-ready mkts, "
                  f"reversion {reversion_rate:.0%}")
        if HAS_WINSOUND:
            winsound.Beep(BEEP_FREQ_FIRE, BEEP_DUR_FIRE)
        state["last_alert_ts"] = now
    elif composite >= SCORE_HOT and fade_ready >= 1:
        tee_print(f"\n  *** BEEP *** Score {composite:.0f}, {fade_ready} FADE-ready mkt(s), "
                  f"reversion {reversion_rate:.0%}")
        if HAS_WINSOUND:
            winsound.Beep(BEEP_FREQ_HOT, BEEP_DUR_HOT)
        state["last_alert_ts"] = now


# -------------------- Market Refresh Thread --------------------

def refresh_thread(client: RestClient, ws: WSStream):
    while not STOP.is_set():
        STOP.wait(MARKET_REFRESH_SEC)
        if STOP.is_set():
            break
        try:
            tee_print("\n  Refreshing market list...")
            slugs = client.discover_markets()
            if slugs:
                ws.set_slugs(slugs)
                ws.subscribe_new(slugs)
                tee_print(f"  Refresh complete: {len(slugs)} markets")
        except Exception as e:
            tee_print(f"  Refresh error: {e}")


# -------------------- Main --------------------

def main():
    if not POLYMARKET_KEY_ID or not POLYMARKET_SECRET_KEY:
        tee_print("ERROR: Set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY env vars")
        tee_print("  . .\\creds.ps1")
        return

    tee_print("=" * 72)
    tee_print("POLYMARKET US ACTIVITY SCANNER v3.0")
    tee_print("=" * 72)
    tee_print(f"  FADE filters: z {Z_TRADEABLE}-{Z_MAX_FADE}, mid {MIN_MID}-{MAX_MID}, spread<{MAX_SPREAD_FADE*100:.0f}%")
    tee_print(f"  Reversion: checks after {REVERSION_CHECK_SEC}s, need >{REVERSION_THRESHOLD*100:.0f}% revert")
    tee_print(f"  Alert: score >= {SCORE_HOT} + fade_ready >= 1 + reversion >= {MIN_REVERSION_RATE*100:.0f}%")
    tee_print(f"  Dashboard every {DASHBOARD_INTERVAL_SEC}s | Log: scanner-console-log.txt")
    tee_print()

    def shutdown(sig, frame):
        tee_print("\n  Shutting down...")
        STOP.set()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    tee_print("  Discovering markets...")
    client = RestClient(POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY)
    slugs = client.discover_markets()
    if not slugs:
        tee_print("  ERROR: No markets found")
        return
    tee_print(f"  Found {len(slugs)} active markets")

    tracker = ActivityTracker()
    ws = WSStream(POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY, tracker)
    ws.set_slugs(slugs)
    ws.start()

    t = threading.Thread(target=refresh_thread, args=(client, ws), daemon=True, name="refresh")
    t.start()

    tee_print(f"  Waiting for data... (first dashboard in {DASHBOARD_INTERVAL_SEC}s)")
    tee_print(f"  Note: reversion data takes ~{REVERSION_CHECK_SEC}s after first spike to populate\n")

    alert_state = {}

    while not STOP.is_set():
        STOP.wait(DASHBOARD_INTERVAL_SEC)
        if STOP.is_set():
            break

        metrics = tracker.get_metrics()
        print_dashboard(metrics, ws)
        alert_if_needed(metrics, alert_state)

    ws.stop()
    tee_print("  Scanner stopped.")


if __name__ == "__main__":
    main()
