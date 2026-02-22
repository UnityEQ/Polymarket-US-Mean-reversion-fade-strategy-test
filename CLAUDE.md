# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Polymarket US CLOB Hunter — a two-process Python system for monitoring and trading on the Polymarket US (CFTC-regulated) prediction market platform. API docs: https://docs.polymarket.us/api/introduction

## Architecture (Two Processes)

### `monitor.py` — Market Monitor
Real-time market data ingestion via WebSocket. Detects price anomalies (spikes/dips) using z-score analysis and writes signals to CSV files that the trade bot consumes.

- **PolymarketUSClient**: REST client for market discovery and balance checks (runs every 5 min)
- **MarketWebSocket**: Streams BBO data via `wss://api.polymarket.us/v1/ws/markets` using `SUBSCRIPTION_TYPE_MARKET_DATA_LITE` (subscription_type=2). Uses wildcard subscribe (`market_slugs: []`) to receive all markets in a single subscription — no batching or cap. Falls back to batched subscribes (100 per message) if wildcard fails.
- **MonitorState**: Global singleton holding price history, mid cache, spread cache, regime tracking
- **Signal pipeline**: BBO update → mid calculation → delta/z-score → percentile gate → severity classification → trade hint (FADE/TREND) → CSV output
- **Regime detection**: Tracks spike outcomes to classify market as `MEAN_REVERT` or `TRENDING`
- **Volume refresh thread**: Background thread running every 60s, re-fetches REST volumes and logs OI count statistics
- **Game phase classification**: `classify_game_phase()` returns `PRE_GAME`/`LIVE`/`POST_GAME`/`UNKNOWN`. Priority: (1) slug date for cross-day (tomorrow+=PRE_GAME, yesterday-=POST_GAME), (2) Polymarket native score data for same-day (definitive pre/in/post), (3) gameStartTime from meta (if in future → PRE_GAME), (4) API endDate fallback. Written to `game_phase` column + `pm_period` and `pm_score_diff` columns in both CSVs.
- **Polymarket score integration**: `PMScoreCache` fetches live game data every 60s from Polymarket's own `GET /v1/market/slug/{slug}` endpoint → `events[0]` fields (`live`, `ended`, `period`, `score`). `pm_score_refresh_thread()` runs in background. Covers NBA, CBB, NFL, UFC, MLS. Only fetches today's date (UTC) — past/future dates already classified by slug-date heuristics. 1:1 slug match — no team mapping or fuzzy matching needed. Rate limited at 100ms between requests (~5-8s per cycle for 50-80 slugs). Slug parser: `parse_slug_parts()` extracts sport/teams/date from slugs like `aec-cbb-duke-mich-2026-02-21`.
- **Output**: Writes to `poly_us_outliers_YYYY-MM-DD.csv` and `poly_us_triggers_YYYY-MM-DD.csv`

### `trade.py` — Trade Bot
Tails the CSV files written by monitor.py and executes trades (paper or live). Implements BUY_NO-only FADE mean-reversion strategy (fades YES-price spikes by buying NO side).

- **PaperBroker**: Simulated broker using CSV mid prices from monitor + JSON mids file
- **LiveBroker**: Real broker using Ed25519-signed REST calls to `api.polymarket.us`
- **TailState**: File tailer that reads new CSV lines incrementally
- **Signal parsing**: `row_to_signal_from_triggers()` and `row_to_signal_from_outliers()` extract (slug, side, mid, z) tuples with delta/spread/volume filters
- **Signal quality gates**: Two-stage filtering — Stage 1 in signal parsers (z threshold, delta band, spread, volume, regime), Stage 2 in `try_open()` (signal age, cooldown, z, mid range, cash, game phase, BUY_NO-only filter, Polymarket score-based late-game close contest filter)
- **Adaptive strategy selection**: `SignalTracker` records signal directions per market. `choose_strategy()` counts same-direction signals in a 5-min window — if 75%+ of 10+ signals are same direction, uses TREND (sustained move); otherwise FADE (isolated spike reverts). `extract_signal_direction()` pulls SPIKE/DIP from raw CSV row before parsing. Log line shows `cluster=X/Y(Z%)` for TREND entries.
- **Fill detection**: Three-layer approach — POST response parsing → order status polling (`_get_order_status`) → portfolio position fallback (`_check_position_exists`)
- **Order execution**: Entry orders use IOC (Immediate-Or-Cancel); close fallback also IOC
- **Exit priority**: TP (6%) → SL (4%) → Trailing Stop (activate 2.5%, trail 2%, peak decays 25%/60s) → Breakeven (8min, 1.5% tolerance) → Time Exit (12min)
- **TP and trailing stop** use `exec_price` from `get_executable_exit_price()` (bid for SELL_LONG, ask for SELL_SHORT) to prevent false profit-taking on inflated mid. **SL, breakeven, time exit** use mid to avoid false triggers from spread noise.
- **Exec price guard**: Before BE and time exits, checks if executable price would cause loss > SL_PCT. If so, defers to SL logic instead — prevents gap losses when mid looks flat but book has moved.
- **Output**: Writes to `poly_us_trades_YYYY-MM-DD.csv` (includes `strategy` column)

### `scanner.py` — Activity Scanner
Standalone pre-flight tool. Runs its own mini z-score pipeline on WebSocket BBO data and scores market conditions for both FADE and TREND profitability. Tracks reversion rate (spikes reverting = good for FADE) and continuation rate (spikes NOT reverting = good for TREND). Beeps (`winsound.Beep`) when either strategy's conditions are met. No CSV output, no trading.

- **ActivityTracker**: Per-market `MarketState` with price history (deque of 50), computes z-scores on every BBO update. Computes separate FADE and TREND composite scores. FADE: 4 weighted metrics (fade-ready 35%, reversion 30%, volatile 15%, tight-spread 20%). TREND: 4 weighted metrics (trend-ready 35%, continuation 30%, volatile 15%, trend-tight 20%).
- **SpikeRecord**: Tracks every eligible spike with `fade_eligible` and `trend_eligible` flags. After 3 minutes, checks if price reverted (>50% = FADE good) or continued (<20% reversion = TREND good). Maintains rolling 10-min window.
- **WSStream**: Same WebSocket + BBO parsing as monitor.py (MARKET_DATA_LITE, wildcard subscribe with batched fallback)
- **Game phase tracking**: `MARKET_META` dict stores timing fields per slug from discovery. `classify_game_phase()` (same logic as monitor.py) classifies strategy-ready markets by phase. If all strategy-ready markets are `PRE_GAME`, that strategy's composite gets 0.3x penalty.
- **Alert**: Beeps when either FADE or TREND score >= 65 with sufficient data (3+ checked spikes, reversion >= 30% for FADE or continuation >= 40% for TREND).
- **Output**: Console + `scanner-console-log.txt` (truncated each run via `tee_print()`)

### Data Flow
```
Polymarket REST (every 60s) → PMScoreCache → classify_game_phase() + CSV pm_period/pm_score_diff
monitor.py (WebSocket BBO + REST discovery) → poly_us_triggers_*.csv / poly_us_outliers_*.csv → trade.py (tail + execute)
  ├─ classify_game_phase() → game_phase column (PM score-enriched: LIVE/PRE_GAME/POST_GAME)
  ├─ pm_period + pm_score_diff columns → trade.py late-game close contest filter
  └─ STATE.meta[slug] stores start_date/end_date/game_id/game_start_time from REST  ↕ poly_mids_latest.json (paper mode)
scanner.py (WebSocket BBO + REST discovery) → console dashboard + system beep (standalone, no file output)
  └─ MARKET_META stores timing fields → game phase counts in dashboard + pre-game score penalty
```

### Log Files (all truncated on each new run)
- **monitor-console-log.txt**: Console output from monitor.py via FileHandler
- **trade-console-log.txt**: Console output from trade.py via stdout/stderr Tee
- **scanner-console-log.txt**: Console output from scanner.py via `tee_print()` (truncated each run)
- **poly_us_triggers_YYYY-MM-DD.csv**: ACCEPT/REJECT signals from monitor with z-scores, deltas, spreads
- **poly_us_outliers_YYYY-MM-DD.csv**: Outlier signals from monitor with FADE/TREND hints
- **poly_us_trades_YYYY-MM-DD.csv**: Executed trades with entry/exit prices, PnL, fees
- **todo.txt**: Tasks for the Claude agent

## Running

```powershell
# Terminal 1: Monitor (requires creds.ps1)
. .\creds.ps1 && python monitor.py

# Terminal 2: Trade bot (requires tcreds.ps1)
. .\tcreds.ps1    # Sets env vars including LIVE=true, then runs trade.py
# OR manually:
python trade.py              # Paper mode (default)
$env:LIVE="true"; python trade.py    # Live mode (PowerShell)
```

```powershell
# Terminal 0 (optional): Activity scanner — run first to check if conditions are tradeable
. .\screds.ps1 && python scanner.py
```

Debug: `DEBUG=1 python monitor.py` | `DEBUG_REJECTIONS=true python trade.py`

```powershell
# WebSocket wildcard test: verify empty market_slugs[] subscribes to all markets
. .\creds.ps1
python monitor.py --ws-test                # 30s default
python monitor.py --ws-test --duration=60  # custom duration
```

## Dependencies

```
pip install websocket-client requests cryptography psutil
```

## Key Configuration Constants

**monitor.py**: `BASE_SPIKE_THRESHOLD=0.003`, `BASE_Z_SCORE_MIN=0.8`, `WATCH_Z=1.5`, `ALERT_Z=3.0`, `MAX_SPREAD_PCT=0.15`, `MID_MIN=0.12`, `MID_MAX=0.55`, `HISTORY_LEN=50`, `V24_MIN=10`, `SHARES_ACTIVITY_MIN=50`, `COOLDOWN_PER_SLUG=60`, `VOLUME_REFRESH_SEC=60`, `MARKET_REFRESH_SEC=300`

**trade.py (FADE)**: `Z_OPEN=4.0`, `Z_OPEN_OUTLIER=4.5`, `TP_PCT=0.06`, `SL_PCT=0.04`, `BREAKEVEN_EXIT_SEC=480`, `BREAKEVEN_TOLERANCE=0.015`, `TIME_EXIT_SEC_PRIMARY=720`, `TRAILING_ACTIVATE_PCT=0.025`, `TRAILING_STOP_PCT=0.02`

**trade.py (TREND)**: `ENABLE_TREND=False` (disabled — 5W/31L -$2.87 historically), `TREND_Z_OPEN=3.5`, `TREND_TP_PCT=0.12`, `TREND_SL_PCT=0.05`, `TREND_TIME_EXIT_SEC=480`, `TREND_TRAILING_ACTIVATE_PCT=0.025`, `TREND_TRAILING_STOP_PCT=0.02`. **Adaptive selection** (inactive): `SIGNAL_CLUSTER_WINDOW_SEC=300`, `SIGNAL_CLUSTER_MIN_COUNT=10`, `SIGNAL_CLUSTER_RATIO=0.75`

**trade.py (shared)**: `MAX_CONCURRENT_POS=3`, `SIZED_CASH_FRAC=0.10`, `MIN_DELTA_PCT=0.015`, `MAX_DELTA_PCT=0.15`, `MIN_OPEN_INTERVAL_SEC=90`, `MIN_MID_PRICE=0.25`, `MAX_MID_PRICE=0.40`, `FADE_NO_SIDE_ONLY=True` (BUY_NO only), `MIN_VOLUME=10`, `SLIPPAGE_TOLERANCE_PCT=3.0`, `CROSS_BUFFER=0.005`, `MAX_BOOK_SPREAD_PCT=0.15`, `DAILY_LOSS_LIMIT=-3.00`, `CIRCUIT_BREAKER_ENABLED=True`, `ORDER_TIMEOUT_SEC=15`, `FILL_POLL_ATTEMPTS=10`, `CLOSE_RETRY_ATTEMPTS=3`, `BLOCK_PRE_GAME=True`, `BLOCK_POST_GAME=True`, `ALLOW_UNKNOWN_PHASE=True`, `BLOCK_LATE_CLOSE=True` with sport-specific thresholds (CBB: period>=2/margin<=8, NBA: period>=4/margin<=10, NFL: period>=4/margin<=8, MLS: period>=2/margin<=1), tiered spread limits by z-score (`MAX_SPREAD_BASE=0.10/MID=0.13/HIGH=0.16`). Entry slippage guard capped at `min(TP_PCT/2, 3%)`. Raw book spread guard rejects if `(ask-bid)/mid > MAX_BOOK_SPREAD_PCT`. Daily loss circuit breaker pauses new entries when `realized_pnl <= DAILY_LOSS_LIMIT`. **Exec price guard**: BE and time exits check executable price before closing — if exec loss would exceed SL_PCT, defers to SL logic instead of closing at a gapped price. Order placement logs `[ORDER]` with intent, price.value, qty, cost/share, IOC, bbo tag. Fill polling logs each attempt's state or 404.

## Class Index

**monitor.py**: `PolymarketUSClient` (REST discovery + balance), `MonitorState` (global singleton: price history, caches, regime), `MarketWebSocket` (WS streaming + BBO parsing), `PMScoreCache` (fetches Polymarket native score data every 60s via `GET /v1/market/slug/{slug}` → `events[0]`, caches game state/period/score for `classify_game_phase()` and CSV enrichment; 1:1 slug match, no external dependency)

**trade.py**: `PMUSEnums` (API enum constants), `RateLimiter` (token bucket), `RearmTracker` (signal rearm after cooldown), `MarketLossTracker` (per-market loss counting), `SignalTracker` (adaptive strategy selection via signal clustering — records per-market signal directions, `choose_strategy()` returns FADE or TREND based on 5-min window ratio), `Position` (open position state + `strategy` field), `PaperBroker` (simulated fills from CSV/JSON mids), `PolymarketUSAuth` (Ed25519 signing), `LiveBroker` (real order placement + fill detection + book spread guard), `TailState` (incremental CSV tailer), `SkipCounters` (signal rejection stats incl. `game_phase_blocked`, `circuit_breaker`). Key functions: `get_exit_params(strategy)` returns strategy-specific thresholds, `extract_signal_direction()` gets SPIKE/DIP from raw row, `row_to_trend_from_triggers/outliers()` parse TREND signals (enter WITH move), `row_to_signal_from_triggers/outliers()` parse FADE signals (enter AGAINST move).

**scanner.py**: `RestClient` (minimal REST for market discovery), `SpikeRecord` (spike outcome tracking with reversion/continuation + fade/trend eligibility flags), `MarketState` (per-market price history + peak z-score), `ActivityTracker` (z-score pipeline + dual FADE/TREND composite scoring), `WSStream` (WebSocket BBO streaming → tracker callback)

## Critical API Patterns (Polymarket US)

### Price Calculation
- **`price.value` is ALWAYS the YES-side price**, regardless of order intent.
- **Book-based crossing (preferred)**: Both `open()` and `_attempt_close()` fetch real order book via `_extract_book_bbo()` → `GET /v1/markets/{slug}/book`. For buy-side crossing (BUY_LONG entry, SELL_SHORT close): `price.value = best_ask + CROSS_BUFFER`. For bid-side crossing (BUY_SHORT entry, SELL_LONG close): `price.value = best_bid - CROSS_BUFFER`. `CROSS_BUFFER = $0.005` (module constant). Falls back to slippage-based pricing if book unavailable.
- **Entry slippage guard**: Rejects if `|actual_cost - ideal_cost| / ideal_cost > TP_PCT / 2` — prevents entering when spread eats more than half the TP target.
- **Shared price cache**: `_refresh_price_cache()` fetches book BBO once every 5 seconds and caches `(mid, bid, ask, timestamp)`. Both `get_current_yes_mid()` (returns mid) and `get_executable_exit_price()` (returns bid or ask) share this cache to avoid redundant API calls.
- **Debug liquidity**: `_debug_liquidity()` logs full order book (top 3 levels each side, market state, cross check) before every order POST. Log lines tagged `[DEBUG]`.
- For `BUY_SHORT` qty: divide cash by NO cost `(1 - order_price)`, NOT by YES mid.
- For `SELL_SHORT` close (fallback only): `price.value = best_ask + CROSS_BUFFER` (book-based) or `1.0 - (no_price / slippage_mult)` (slippage fallback).
- Order log includes `bbo=bid/ask` or `bbo=NONE`. `[BBO]` log shows source (`/book` or `/bbo fallback`) and depth.

### Order Endpoints
- Create order: `POST /v1/orders` — do NOT use `synchronousExecution: true` (returns empty executions even when orders fill async). Use async + polling/portfolio fallback.
- Get order: `GET /v1/order/{orderId}` — **response wraps in `{"order": {...}}`**, must unwrap before reading `state`/`avgPx`
- Cancel: `POST /v1/order/{orderId}/cancel` — requires `{"marketSlug": "..."}` in body
- Close position: `POST /v1/order/close-position` (singular `/v1/order/`, NOT `/v1/orders/`) — **only supports `marketSlug` + `slippageTolerance`**. Do NOT send `intent`, `synchronousExecution`, or `maxBlockTime`. **`slippageTolerance` must be an object**: `{"currentPrice": {"value": "0.50", "currency": "USD"}, "bips": 300}`. A raw float causes `proto: syntax error`.
- Entry and close fallback orders use `TIME_IN_FORCE_IMMEDIATE_OR_CANCEL` (IOC). GTC sits on book if not immediately marketable.

### Fill Detection & Order States
- Orders can fill on exchange while POST response returns empty executions. Must check multiple sources.
- Fill price field is `avgPx` (Amount object `{value, currency}`), NOT `averagePrice`.
- **Order states ARE prefixed**: `ORDER_STATE_FILLED`, `ORDER_STATE_PARTIALLY_FILLED`, `ORDER_STATE_CANCELED`, `ORDER_STATE_REJECTED`, `ORDER_STATE_EXPIRED`. Use exact enum matching via `PMUSEnums` constants — NOT substring matching.
- **Execution types ARE prefixed**: `EXECUTION_TYPE_FILL`, `EXECUTION_TYPE_PARTIAL_FILL`. Check via `PMUSEnums.EXEC_FILL` / `PMUSEnums.EXEC_PARTIAL_FILL`.
- **Portfolio fallback**: `GET /v1/portfolio/positions` (no query params — endpoint doesn't support `?market=` filter). Response is dict keyed by market slug.
- **Portfolio fallback must distinguish entry vs close**: `_wait_for_fill(is_close=False)` default: position EXISTS = entry filled. `_wait_for_fill(is_close=True)`: position EXISTS = close FAILED (still open), position GONE = close succeeded. All `_attempt_close` callers pass `is_close=True`.
- **Never cancel blindly after timeout** — order may have already filled. Check portfolio first.
- **Private WebSocket** (`wss://api.polymarket.us/v1/ws/private` with `SUBSCRIPTION_TYPE_ORDER`) is the recommended approach for real-time fill updates instead of polling. Not yet implemented.

### Response Formats
- API returns Amount objects `{value, currency}` — use `extract_amount_value()` (monitor) or `to_float()` (trade) to unwrap.
- Volume field names vary: `volume24hr`, `volume24h`, `last24HrVolume`, `volume24Hr`, `volumeNum`, `volume`.
- Rate limits: 40 order req/sec, 200 global req/sec.

## Important Code Patterns

- Auth uses Ed25519 key signing: `timestamp_ms + method + path` → sign → base64. **Path only, NO query string** — `_api_get` strips query params via `path.split("?")[0]` before signing. Both files implement auth independently.
- `json.dumps` is monkey-patched in trade.py for deterministic serialization (sorted keys, no spaces) — required for signature verification.
- WebSocket message format varies (snake_case vs camelCase, nested vs flat) — `_handle_single_update()` has extensive fallback parsing.
- CSV files use daily date suffixes and are referenced by both processes simultaneously (RLock in monitor, file tailer in trade).

### Liquidity Pipeline (cross-file concern)
**Sports markets do NOT return volume fields** (`volume24hr`, `volumeNum`, `volume` are absent from both listing and detail REST responses). Liquidity is determined from WebSocket `MARKET_DATA_LITE` data instead:
- `openInterest` (string) — total open positions (most reliable, always present)
- `sharesTraded` (string) — lifetime shares traded (can be empty string `""` for pre-game markets)

Liquidity proxy chain at signal time: `REST volume24h > WS sharesTraded (if > SHARES_ACTIVITY_MIN=50) > WS openInterest`. Both `sharesTraded` and `openInterest` are extracted in `_handle_single_update()`, stored in `STATE.meta[slug]`, and updated on every BBO tick. First-capture of openInterest logs `[OI]` line.

**Dual volume gates**: monitor rejects signals with `effective_vol < V24_MIN (10)` as `REJECT_VOLUME`. trade.py independently rejects with `MIN_VOLUME (10)`. The CSV `volume` column carries `effective_vol` (which may be openInterest). Periodic stats show `OI>0: X/Y` in heartbeat, discover, and volume refresh logs.

The `_extract_volume()` helper only searches REST fields — its "no REST volume" debug messages are expected for sports markets and do not indicate a problem. The WS openInterest path is entirely separate.

## Known Issues to Investigate

Track observed bugs/concerns here. Remove entries once fixed and verified.

- **OI wipe on discover refresh**: `discover(refresh=True)` replaces `STATE.meta` dicts, wiping WS-populated `open_interest` and `shares_traded` values. OI counts drop from 18/49 → 1/49 and slowly rebuild as new WS messages arrive. During active games, this 5-min refresh window could cause valid signals to get `REJECT_VOLUME`. Fix: preserve OI/sharesTraded values across refresh, or merge new meta into existing instead of replacing.

## Market Intelligence

Observations gathered from live runs and log analysis. Use these to identify patterns, refine strategy, and inform parameter tuning. Add new findings with dates. Mark entries with `[ACTED]` once rolled into a code/config change.

### Market Structure
- **High-YES-mid markets are signal factories but untradeable for FADE** (2026-02-07): Michigan vs Ohio State (CBB) generated 49 signals in ~15 min during a live game, 8 of which were ACCEPT_FADE with z=4-5+. All had mid 0.749-0.790, correctly filtered by MAX_MID_PRICE=0.55 (lowered from 0.65). Heavy favorites in sports consistently sit above 0.55 — lots of noise, zero edge for mean-reversion.
- **Winners historically had mid < 0.40** (from early runs): Trades that hit TP were in lower-mid markets where NO liquidity is deeper and % swings are more meaningful.
- **CBB favorites tend to have very high YES mids** (2026-02-07): Michigan at 0.787 YES — thin NO books cause volatile % swings on small absolute moves (0.4 cent → z=4.5+). NFL similarly skewed for favorites.
- **NFL markets dwarf CBB/NBA in open interest** (2026-02-07): SEA vs NE had OI=620,844 vs CBB markets at 28-1,496 and NBA at 9-959. Higher OI = deeper books = more reliable mid prices.

### Timing & Activity Patterns
- **Saturday night pre-game is quiet** (2026-02-07): Before games tip off, most markets produce zero signals. Only active live games generate meaningful price movement.
- **Live games trend directionally on in-game events** (2026-02-07): Michigan game showed sustained DIP signals (score/momentum shift) — these are real information, not noise. FADE strategy correctly avoids via mid filter, but worth remembering: don't try to force entries during live action in high-mid markets.
- **Signal bursts cluster in seconds during live games** (2026-02-07): 3-4 ACCEPT signals within 1-2 seconds are common — cooldown filter correctly prevents stacking entries on the same move.
- **Pre-game Sunday morning (07:00-09:00) produces mostly flat time exits** (2026-02-08): 4 of 5 trades hit time_exit with minimal or zero price movement. Markets just aren't moving enough pre-game. Fee drag from round-trip costs (~$0.015/trade) erodes equity. One winner (UCF mid=0.43, TP in 4 min) covered all 4 losses. Net +$0.0117 but razor-thin edge.
- **WS BBO spread vs REST book spread diverge massively for pre-game CBB** (2026-02-08): Monitor's WS BBO flashes narrow spreads (passing 10-15% filter) but REST order book reveals real standing spreads of 11-58%. All 10 qualifying signals rejected by entry slippage guard (3.6-14.6% vs 3.0% threshold). Narrowest real spread was Tulsa-SFL at 3.9 cents (13% of mid). Pre-game CBB is untradeable for FADE with 6% TP.

### Liquidity & Volume
- **OI is the only reliable liquidity metric for sports** (2026-02-07): REST API returns zero volume fields for all sports markets. openInterest from WebSocket is the sole proxy. See Known Issues for the OI wipe bug.
- **Low-OI markets (< 100) may have unreliable mid prices** (2026-02-07): Markets like rice-uab (OI=28), phi-por (OI=9) have such thin books that mid prices may not reflect true value.
- **OI=91 too thin for fills** (2026-02-07): SAC vs NO order placed IOC at mid=0.3645 (in the sweet spot), z=3.77, but no counterparty existed. Spread looked tight (3.9%) from WS but zero depth behind it. Even a $1 order couldn't fill. MIN_VOLUME=10 let this through — likely need MIN_VOLUME >= 200+ for reliable fills.

### Strategy Implications
- **FADE sweet spot appears to be mid 0.25-0.40** (strengthened 2026-02-21): `[ACTED]` MAX_MID_PRICE lowered from 0.45 to 0.40. All BUY_NO TP wins in v15.3 had mid < 0.40 (0.258-0.362). BUY_NO SL losses at mid 0.414 and 0.421 would be filtered by 0.40 ceiling.
- **Consider sport-specific filters** (hypothesis): NFL has deep liquidity but favorites are heavily skewed. CBB underdogs (low mid) might be the best FADE candidates if OI is sufficient. NBA TBD — tomorrow's 9-game slate (2026-02-08) will provide first real NBA data.
- **Live CBB game events = adverse selection for FADE** (2026-02-08): NCG-Furman entry at mid=0.4265 (in the sweet spot) with z=3.8, clean fill on 5,282 shares. But the spike was real info — game event pushed YES from 0.447 → 0.488+ in one tick, SL loss 15.3% (2.5x target). FADE assumes mean-reversion, but live game events ARE the new mean. The signal quality filters (z, delta, spread) can't distinguish noise from real game momentum.
- **TREND gap risk is severe — actual SL losses 3-6x target** (2026-02-21): 3 TREND SL exits had actual losses of 17.5%, 29.3%, 16.7% vs 5% target. Price gaps through the stop on live game events. 5-second polling can't catch intra-tick jumps. `[ACTED]` Added raw book spread guard (`MAX_BOOK_SPREAD_PCT=15%`) — would have blocked trade #1 (22% spread). Also added daily loss circuit breaker (`DAILY_LOSS_LIMIT=-$0.30`).
- **TREND trailing stop is the best exit for momentum** (2026-02-21): Both trailing stop exits were wins (+$0.049, +$0.031), peaking at 10% and 7.6%. `[ACTED]` Lowered `TREND_TRAILING_ACTIVATE_PCT` from 3.5% to 2.5% to capture partial profits sooner before game events reverse.
- **FADE breakeven exits dominate during flat pre-game/early-game** (2026-02-21): Feb 20 session: 7/9 trades hit breakeven with avg -$0.015 fee drag each. Markets simply don't move enough for FADE — price sits flat then exits at a loss from round-trip fees.
- **TREND 3W/3L is better hit rate than FADE 1W/8L but worse risk/reward** (2026-02-21): TREND avg win $0.083 vs avg loss $0.142. Still underwater on risk/reward due to gap losses. Need the trailing stop activation to capture more partial wins.

### MIN_VOLUME Tuning Log
Detailed fill/no-fill log with per-trade data in [`min_volume_log.md`](min_volume_log.md). Key findings (as of 2026-02-10):
- **OI=91 too thin** for fills. OI >= 6.7K fills reliably during live games.
- **High OI ≠ tight spread**: USC-PennSt OI=139K but pre-game spread 11-58%. OI measures total positions, not active depth.
- **NFL OI (620K+) >> CBB (28-15.6K) >> NBA (9-959)**. May need sport-specific thresholds.
- **Avg SL loss (12.6%) is 1.8x avg TP win (7.0%)** due to live game-event gap risk. Inherent to sports — no code fix.
- **Pre-game markets (all sports) consistently untradeable** — wide real spreads, flat prices, breakeven exits at a loss.
- **Cumulative live stats: 13W/29L, -$0.47** (through 2026-02-21). v15.3 ADAPTIVE: 7W/12L +$0.098 (first profitable session). BUY_NO: 6W/7L +$0.530, BUY: 1W/4L -$0.402. `[ACTED]` v15.4: BUY_NO only (`FADE_NO_SIDE_ONLY=True`) — BUY-side fades dips which are real game info in live sports. Projected v15.3 PnL with BUY_NO filter: +$0.500.

## Agent Instructions

1. **Bug tracking**: When you discover a potential bug or concern during log analysis that cannot be immediately verified or fixed, add it to "Known Issues to Investigate". Remove the entry after the fix is implemented and verified working.
2. **Intelligence gathering**: During every log analysis session, look for patterns, anomalies, and market behavior insights. Add noteworthy observations to "Market Intelligence" with the date. If an observation leads to a code/config change, mark it `[ACTED]` but keep it for historical context. Over time, recurring patterns here should inform strategy refinements and parameter tuning.
3. **MIN_VOLUME tuning**: On every log analysis session, check for order attempts (filled or unfilled) and add a row to the table in [`min_volume_log.md`](min_volume_log.md) with the market, sport, OI, mid, fill result, and notes. Update the summary in this file's MIN_VOLUME section if key findings change. This is a high-priority recurring metric — the right threshold directly determines whether the bot can trade at all.

### REST API Endpoint Paths
- **List markets**: `GET /v1/markets` (plural) — returns `{"markets": [...]}`
- **Get market by slug**: `GET /v1/market/slug/{slug}` (SINGULAR `/v1/market/` + `slug/` prefix) — response wraps in `{"market": {...}}`, must unwrap. NOT `/v1/markets/{slug}`.
- **Order book**: `GET /v1/markets/{slug}/book` — returns `{"marketData": {"bids": [...], "offers": [...], "state": "..."}}`. Each level has `{"px": {"value": "0.35"}, "qty": "10.5"}`. Primary source for bid/ask pricing.
- **BBO**: `GET /v1/markets/{slug}/bbo` — returns BBO data but response format may not match expected `marketDataLite.bestBid/bestAsk` fields. Used as fallback only; order book endpoint is more reliable.
- **WebSocket wildcard subscribe**: `market_slugs: []` subscribes to ALL active markets in one message. Server sends an initial snapshot burst (one message per market) then streams live BBO updates. Response uses camelCase (`marketDataLite`, `marketSlug`, `requestId`). Verified 2026-02-20: 216 unique slugs received (101.4% of REST-discovered 213, includes markets filtered by discovery).
- WebSocket `MARKET_DATA_LITE` does NOT include `volume24hr` — only `sharesTraded` (lifetime) and `openInterest`. Volume requires REST polling (which returns nothing for sports markets).


## API Documentation

Full docs: https://docs.polymarket.us/api/introduction

Key references:
- Orders: https://docs.polymarket.us/api-reference/orders/overview
- Markets: https://docs.polymarket.us/api-reference/market/overview
- WebSocket: https://docs.polymarket.us/api-reference/websocket/overview
- Portfolio: https://docs.polymarket.us/api-reference/portfolio/overview
- Auth: https://docs.polymarket.us/api/authentication
- Python SDK: https://docs.polymarket.us/sdks/python/quickstart