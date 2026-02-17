# Polymarket US Trade Bot Credentials
# Run this in a separate terminal from monitor.py

# Set credentials (same as creds.ps1)
$env:POLYMARKET_KEY_ID = ""
$env:POLYMARKET_SECRET_KEY = ""

# Trading mode: "false" = paper trading, "true" = live trading
$env:LIVE = "true"

# Optional: Override CSV paths if needed
# $env:OUTLIERS_CSV = "poly_us_outliers_2026-02-05.csv"
# $env:ARBITRAGE_CSV = "poly_us_arbitrage_2026-02-05.csv"

Write-Host "=== Polymarket US Trade Bot ===" -ForegroundColor Cyan
if ($env:LIVE -eq "true") {
    Write-Host "MODE: LIVE TRADING" -ForegroundColor Red
} else {
    Write-Host "MODE: PAPER TRADING" -ForegroundColor Green
}
Write-Host ""

python trade.py