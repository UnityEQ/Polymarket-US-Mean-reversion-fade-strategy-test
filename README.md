# Disclaimer

- Only test with a balance of $10 max. NEVER test live trades on big balances
- You will not make money using this bot, it's strategy is highly dependant on alot of "noise."

- This project is for understanding how to interact with the api in different ways (this is just one of them)

# Polymarket US Hunter

A mean-reversion (FADE) trading system for the [Polymarket US](https://polymarket.us) prediction market. Monitors real-time sports market prices via WebSocket, detects price anomalies using z-score analysis, and auto-trades the spikes that revert. Uses Polymarket's native score data to classify game phase and filter out untradeable conditions (pre-game, post-game, late-game close contests).

## Using Claude AI With This Project

This entire project was built with [Claude Code](https://claude.ai/code) — an AI coding agent that lives in your terminal. The `CLAUDE.md` file in this repo teaches Claude everything about the architecture, API quirks, and strategy so it can debug, modify, and extend the code for you.

**Quick start:**

1. Download the Claude Code installer from [claude.ai/code](https://claude.ai/code) (requires an Anthropic account)
2. Run the installer — it adds `claude` to your PATH
3. Open a terminal in this project folder and type `claude`
4. That's it. Claude reads `CLAUDE.md` automatically and knows the entire codebase. Ask it to analyze logs, fix bugs, tune parameters, add features, or explain how anything works.

**Example things to ask Claude in this repo:**
- "Analyze today's trade log and tell me what happened"
- "Why did the bot take a loss on this market?"
- "Add a new filter that blocks NBA markets"
- "What's the current win rate and PnL?"

## Setup

**Update your API credentials in all three creds files before running anything:**

Get API Keys here: https://polymarket.us/developer

- `creds.ps1` — credentials for the monitor
- `tcreds.ps1` — credentials for the trade bot (also sets `LIVE=true`)
- `screds.ps1` — credentials for the scanner

Each file sets environment variables that the Python scripts read. Open each one and replace the placeholder key ID and secret key with your own from your Polymarket US account.

## Requirements

- Python 3.10+
- Windows (uses `winsound` for scanner alerts)

```
pip install websocket-client requests cryptography psutil
```

## HOW TO RUN

Each script runs in its own terminal. They are separate processes on purpose — this keeps monitoring, trading, and scanning isolated from each other so you can debug, restart, or modify one without touching the others. It also makes it easy to build on individual pieces without worrying about breaking something else.

```powershell
# Terminal 1: Market monitor — streams live prices, writes signals to CSV
.\creds.ps1

# Terminal 2: Trade bot — tails the CSV signals and executes trades
.\tcreds.ps1

# Terminal 3 (optional): Activity scanner — checks if conditions are good for FADE
.\screds.ps1
```

Run the scanner first to see if market conditions are worth trading. If it says HOT, fire up the monitor and trade bot.

## How It Works

**The monitor** connects to the Polymarket US WebSocket and subscribes to all active markets with a single wildcard subscribe (`market_slugs: []`). It streams best-bid/offer data and runs a z-score pipeline on every price update. When it detects a spike or dip that exceeds configurable thresholds, it writes a signal row to a CSV file with the market slug, direction, z-score, spread, and game phase. A background thread fetches live game data from Polymarket's own market detail endpoint every 60s to classify each market as PRE_GAME, LIVE, POST_GAME, or UNKNOWN — along with the current game period and score differential.

**The trade bot** tails those CSV files in real-time. It only trades the NO side (BUY_NO) — fading YES-price spikes that are likely to revert. When it sees a qualifying signal, it places an IOC order through the REST API. It then monitors the position and exits via take-profit (6%), stop-loss (4%), trailing stop (activates at 2.5%, trails at 2%), breakeven timeout (8min), or time exit (12min). Several safety filters protect against bad entries: pre-game and post-game markets are blocked, late-game close contests are blocked (sport-specific period + score margin thresholds), and a daily loss circuit breaker pauses trading if cumulative losses exceed a configurable limit.

**The scanner** runs its own mini z-score pipeline independently and scores overall market conditions. It tracks whether spikes actually revert (reversion rate) and beeps when conditions support the FADE strategy. It doesn't trade or write files — it's purely a go/no-go indicator.

## API Examples

`basic.py` is a standalone script with simple examples for interacting with the Polymarket US API — useful for learning the endpoints, auth, and WebSocket streaming without the complexity of the full trading system.

```powershell
# Set your credentials first
$env:POLYMARKET_KEY_ID = "your-key-id"
$env:POLYMARKET_SECRET_KEY = "your-base64-secret"

python basic.py                  # Run all REST examples (balance, markets, book)
python basic.py markets [N]      # List N active markets (default 20)
python basic.py market <slug>    # Get single market details by slug
python basic.py book <slug>      # Get order book with bids, offers, spread
python basic.py balance          # Get account balance
python basic.py stream           # Stream live BBO for ALL markets via wildcard
python basic.py stream <slug>    # Stream live BBO for a single market
```

## Files

| File | What it does |
|------|-------------|
| `basic.py` | Simple API examples — REST endpoints and WebSocket streaming |
| `monitor.py` | WebSocket market data ingestion, z-score signal detection, CSV output |
| `trade.py` | CSV signal tailing, order execution (paper or live), position management |
| `scanner.py` | Standalone condition scorer, reversion tracking, audible alerts |
| `creds.ps1` | Environment variables for monitor (API key + secret) |
| `tcreds.ps1` | Environment variables for trade bot (API key + secret + LIVE flag) |
| `screds.ps1` | Environment variables for scanner (API key + secret) |
| `CLAUDE.md` | Detailed architecture docs, API patterns, and strategy notes |
| `min_volume_log.md` | Historical fill/no-fill tracking by market and OI level |

## Output Files (generated at runtime)

| File | Contents |
|------|----------|
| `poly_us_triggers_YYYY-MM-DD.csv` | ACCEPT/REJECT signals with z-scores, deltas, spreads, game phase, period/score |
| `poly_us_outliers_YYYY-MM-DD.csv` | Outlier signals with FADE/TREND classification, game phase, period/score |
| `poly_us_trades_YYYY-MM-DD.csv` | Executed trades with entry/exit prices and PnL |
| `poly_us_blowout_YYYY-MM-DD.csv` | Late-game blowout snapshots for end-of-game strategy research |
| `monitor-console-log.txt` | Monitor console output |
| `trade-console-log.txt` | Trade bot console output |
| `scanner-console-log.txt` | Scanner console output |

## Debug Modes

```powershell
$env:DEBUG="1"; python monitor.py              # Verbose monitor logging
$env:DEBUG_REJECTIONS="true"; python trade.py   # Log every rejected signal with reason
python monitor.py --ws-test                    # Test wildcard WebSocket subscribe (30s)
python monitor.py --ws-test --duration=60      # Same but listen for 60s
```

## Live Score Integration

The monitor fetches live game data from Polymarket's own market detail endpoint (`GET /v1/market/slug/{slug}` → `events[0]`) every 60 seconds. This is a 1:1 slug match with zero external dependencies — no team mapping or fuzzy matching needed. It powers three filters that prevent the bot from entering bad trades:

1. **Pre-game blocking** — Game hasn't started yet, so price spikes are just noise from thin books. Blocked.
2. **Post-game blocking** — Game is over, so prices are settling to 0 or 1. Blocked.
3. **Late-game close contest blocking** — Final period of a close game (e.g. 4th quarter NBA, margin <= 10). Price spikes here are real game events, not noise. Blocked.

Covers NBA, CBB, NFL, UFC, and MLS. Markets that can't be matched to score data fall through to UNKNOWN and are still tradeable — score integration only improves filtering when data is available, it never blocks the bot from running.

## Notes

- This was built and tested during the Polymarket US beta period (early 2026). The platform had low liquidity at the time — thin order books, wide spreads, and limited participants. The FADE strategy works best with higher retail flow creating noise-driven price spikes. Performance should improve as the platform grows.
- The trade bot defaults to paper mode. Set `LIVE=true` in your environment (or use `tcreds.ps1`) to trade real money.
- Sports markets on Polymarket US do not return volume data via REST API. The system uses WebSocket `openInterest` as a liquidity proxy instead.
- The bot only trades the NO side (`FADE_NO_SIDE_ONLY=True`). Fading YES-price dips (buying YES) was structurally unprofitable in live sports — dips are real game information, not noise. BUY_NO fading YES-price spikes has a positive edge.
