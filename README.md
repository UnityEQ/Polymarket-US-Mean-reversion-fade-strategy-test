# Polymarket US Hunter

A mean-reversion (FADE) trading system for the [Polymarket US](https://polymarket.us) prediction market. Monitors real-time sports market prices via WebSocket, detects price anomalies using z-score analysis, and auto-trades the spikes that revert.

## Setup

**Update your API credentials in all three creds files before running anything:**

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

**The monitor** connects to the Polymarket US WebSocket, streams best-bid/offer data for all active sports markets, and runs a z-score pipeline on every price update. When it detects a spike or dip that exceeds configurable thresholds, it writes a signal row to a CSV file with the market slug, direction, z-score, spread, and game phase.

**The trade bot** tails those CSV files in real-time. When it sees a qualifying FADE signal (spike likely to revert), it places an IOC order through the REST API. It then monitors the position and exits via take-profit (6%), stop-loss (6%), trailing stop, breakeven timeout (10min), or time exit (20min).

**The scanner** runs its own mini z-score pipeline independently and scores overall market conditions. It tracks whether spikes actually revert (reversion rate) and beeps when conditions support the FADE strategy. It doesn't trade or write files — it's purely a go/no-go indicator.

## Files

| File | What it does |
|------|-------------|
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
| `poly_us_triggers_YYYY-MM-DD.csv` | ACCEPT/REJECT signals with z-scores, deltas, spreads |
| `poly_us_outliers_YYYY-MM-DD.csv` | Outlier signals with FADE/TREND classification |
| `poly_us_trades_YYYY-MM-DD.csv` | Executed trades with entry/exit prices and PnL |
| `monitor-console-log.txt` | Monitor console output |
| `trade-console-log.txt` | Trade bot console output |
| `scanner-console-log.txt` | Scanner console output |

## Debug Modes

```powershell
$env:DEBUG="1"; python monitor.py              # Verbose monitor logging
$env:DEBUG_REJECTIONS="true"; python trade.py   # Log every rejected signal with reason
```

## Notes

- This was built and tested during the Polymarket US beta period (early 2026). The platform had low liquidity at the time — thin order books, wide spreads, and limited participants. The FADE strategy works best with higher retail flow creating noise-driven price spikes. Performance should improve as the platform grows.
- The trade bot defaults to paper mode. Set `LIVE=true` in your environment (or use `tcreds.ps1`) to trade real money.
- Sports markets on Polymarket US do not return volume data via REST API. The system uses WebSocket `openInterest` as a liquidity proxy instead.
