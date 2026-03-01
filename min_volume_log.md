# MIN_VOLUME Tuning Log

**Track every run's fill/no-fill outcomes vs OI to find the right MIN_VOLUME floor.** Currently MIN_VOLUME=10 (trade.py). Too low = orders sent to ghost markets that can't fill. Too high = missed opportunities in legitimately tradeable thin markets. Review this section each session to spot trends by sport, day, and time.

| Date | Market | Sport | OI | Mid | Filled? | Notes |
|------|--------|-------|----|-----|---------|-------|
| 2026-02-07 | SAC vs NO | NBA | 91 | 0.3645 | NO | IOC found no counterparty. Spread 3.9% looked tight but zero depth. $1 order couldn't fill. |
| 2026-02-08 | Mary vs MinnSt | CBB | 161 | 0.2095 | PAPER | Time exit, -$0.027. Flat pre-game market. |
| 2026-02-08 | UCF vs Cin | CBB | 362 | 0.4420 | PAPER | Time exit, -$0.010. Zero movement in 20 min. |
| 2026-02-08 | Tulsa vs SFL | CBB | 413 | 0.3725 | PAPER | Time exit, -$0.010. Zero movement in 20 min. |
| 2026-02-08 | Det vs Cha | NBA | 778 | 0.5880 | PAPER | Time exit, -$0.030. Small adverse move. |
| 2026-02-08 | UCF vs Cin #2 | CBB | 362 | 0.4325 | PAPER | TP HIT in 4 min! +$0.089. Best trade of the session. |
| 2026-02-08 | USC vs PennSt | CBB | ? | 0.4585 | NO | BUY_SHORT IOC expired x2. Slippage-based price (0.456, 0.463) didn't cross. Pre-BBO fix. |
| 2026-02-08 | TxTech vs WVir | CBB | ? | 0.6330 | NO | BUY_LONG IOC expired. price.value=0.652 (mid*1.03) didn't reach ask. Pre-BBO fix. |
| 2026-02-08 | USC-PennSt | CBB | 139K | 0.26-0.35 | SKIP | Book spread 11-58%. Entry slippage 5.5-14.6%. High OI ≠ tight spread for pre-game. |
| 2026-02-08 | NCG-Furman | CBB | 4.9K | 0.33-0.44 | SKIP | Book spread 10-22%. Entry slippage 7-13%. |
| 2026-02-08 | Tulsa-SFL | CBB | 56K | 0.30 | SKIP | Tightest spread: bid=0.279 ask=0.318 (3.9¢). Slippage 3.6% — just over 3% guard. |
| 2026-02-08 | NCG-Furman | CBB | 6.7K | 0.4265 | YES (LIVE) | **First clean book-based fill.** BID 1: 0.426 x 5282. SL hit in 31s, -$0.16 (15.3% loss vs 6% target). Game event gap. |
| 2026-02-08 | UCF-Cin | CBB | 15.6K | 0.358 | YES (LIVE) | BUY_LONG fill at ask=0.360 (4 shares!). SL -$0.105. **6 close IOC orders expired** — slippage pricing 0.308 > bid 0.283. `[ACTED]` |
| 2026-02-08 | NCG-Furman | CBB | 6.7K | 0.540 | YES (LIVE) | BUY_LONG at ask=0.547, TP in 64s. +$0.051. Only clean live win. |
| 2026-02-08 | UCF-Cin | CBB | 15.6K | 0.367 | YES (LIVE) | BUY_SHORT fill at bid=0.363. SL in 32s, -$0.113. Game event gap (bid 0.356→0.392). |
| 2026-02-08 | Mary-MinnSt | CBB | 6.4K | 0.238 | YES (LIVE) | BUY_SHORT fill at bid=0.224 (14 shares!). **False SL in 33s** — `_calc_profit_pct` bug made -4.6% look like -14.7%. `[ACTED]` |
| 2026-02-08 | Rice-UAB #1 | CBB | ? | 0.2555 | YES (LIVE) | BUY_SHORT at bid=0.240. TP in 11.5 min, +$0.012. First trade with corrected profit formula. |
| 2026-02-08 | Mary-MinnSt #2 | CBB | ? | 0.3175 | YES (LIVE) | BUY_LONG at ask=0.318. **TP triggered at a loss** (-$0.019). Ask spiked 0.318→0.363, mid=0.339 hit 6% TP, but bid=0.315 < entry. `[ACTED]` |
| 2026-02-08 | Rice-UAB #2 | CBB | ? | 0.278 | YES (LIVE) | BUY_SHORT at bid=0.264. SL in 34s, -$0.116. Legit game event. |
| 2026-02-08 | Rice-UAB (high mid) | CBB | ? | 0.5005 | NO | BUY_SHORT IOC expired at 0.494 vs bid 0.499. Bid only 196 shares deep — race condition. |
| 2026-02-08 | Chi-BKN | NBA | ? | 0.588 | YES (LIVE) | BUY_LONG at ask=0.598. Breakeven 10 min, -$0.010. Price flat, order 404 on poll → portfolio fallback confirmed. |
| 2026-02-08 | SAC-NO | NBA | ? | 0.324 | YES (LIVE) | BUY_SHORT at bid=0.309 (624 shares). Breakeven 10 min, -$0.039. Pre-game, price flat (bid stuck at 0.309 entire hold). Entry slippage 2.96% — just under 3% guard. |
| 2026-02-08 | SEA-NE | NFL | ? | ~0.64 | SKIP | Entry slippage 5-13%. Pre-game NFL, wide spread (bid=0.621, ask=0.678). |
| 2026-02-08 | PVAM-FLAM | CBB | ? | ~0.44 | SKIP | Entry slippage 11-19%. Pre-game CBB, very thin (7 bids, 8 offers). |
| 2026-02-08 | DET-CHA | NBA | ? | ? | SKIP | Entry slippage 7.2%. Pre-game NBA. |
| 2026-02-08 | ATL-MIN | NBA | ? | ? | SKIP | Entry slippage 5.9%. Pre-game NBA. |
| 2026-02-09 | UTA-MIA | NBA | ? | 0.293 | YES (LIVE) | BUY_NO, breakeven 10min, -$0.024. Tight entry spread but price didn't revert. Live game. |
| 2026-02-09 | MIL-ORL | NBA | ? | 0.212 | YES (LIVE) | BUY_NO, breakeven 10min, -$0.026. Low mid, price drifted against. Live game. |
| 2026-02-09 | StFPA-ChiSt | CBB | ? | varies | NO (x6) | Entry slippage 5-19%. Volatile live CBB, 6 attempts all rejected by 3% guard. |
| 2026-02-09 | jackst-arpb | CBB | ? | 0.227 | YES (LIVE) | BUY_NO, SL in live game, -$0.092. YES moved 0.227→0.281 (game event). Confirms live CBB adverse selection. |
| 2026-02-09 | iowa-maryland | CBB | ? | 0.808 | SKIP | Pre-game, mid > MAX_MID_PRICE=0.65. Monitor accepted (MID_MAX=0.88) but trade.py would reject. |
| 2026-02-09 | stfpa-chist (pre) | CBB | ? | varies | SKIP (x10) | All pre-game ACCEPT_FADE signals had 5-15% real book spreads. Entry slippage guard rejected all. |

| 2026-02-22 | fair-quin | CBB | ? | 0.270 | YES (LIVE) | BUY_NO at YES=0.260 (bid). 1.34 shares. SL hit after 6min, -$0.1168 (10.8% actual vs 4% target). Peak exec profit 4.1% (ask=0.240 for 60s) but TP 6% unreachable. Trailing stop armed but peak decayed + consecutive reset killed it (see analysis). Game event gap: mid 0.295→0.325 in one 5s poll. |
| 2026-02-28 | CHI-COL #1 | NHL | ? | 0.255 | YES (LIVE) | BUY_NO, bid=0.260/ask=0.290. SL in 33s, -$0.0901. Book gapped bid=0.240/ask=0.330 (9¢ spread) in 30s. First NHL trade ever. |
| 2026-02-28 | CHI-COL #2 | NHL | ? | 0.310 | YES (LIVE) | BUY_NO, bid=0.310/ask=0.320. SL in 64s, -$0.1817. Ask gapped 0.320→0.570 in one 5s poll. 18.2% actual loss (4.5x target). Re-entry into trending market. |
| 2026-02-28 | BUF-TB #1 | NHL | ? | 0.310 | YES (LIVE) | BUY_NO, bid=0.300/ask=0.320 (depth 3865 qty on bid1). BE in 8min, -$0.0382. Price sat perfectly flat — pure fee drag. Delta 1.6% barely passed 1.5% floor. |
| 2026-02-28 | BUF-TB #2 | NHL | ? | 0.370 | YES (LIVE) | BUY_NO, bid=0.390/ask=0.400 (thin: bid1=129, ask1=25). SL in 35s, -$0.1232. Book gapped bid=0.440/ask=0.460. 12.3% actual loss (3.1x target). |
| 2026-02-28 | DET-CAR | NHL | ? | 0.270 | SKIP | Entry slippage guard: 5.6% > 3.0% threshold. Spread bid=0.250/ask=0.290 (14.8% book spread). Correctly prevented 5th loss. |

## Observations

- OI=91 confirmed too thin (2026-02-07). Need more data points to find the floor.
- NFL OI (620K+) is a different universe from CBB (28-1,496) and NBA (9-959). May need sport-specific MIN_VOLUME thresholds.
- Key question: what's the minimum OI where IOC orders reliably fill? Track fills AND near-misses to build the picture.
- **3 consecutive IOC expirations (2026-02-08)**: All used slippage-based pricing (mid +/- 3%). Root cause: actual spreads wider than 6%, so 3% slippage from mid doesn't reach the other side. Fixed by switching to book-based crossing (fetch real bid/ask, price at best + buffer). `[ACTED]`
- **High OI ≠ tight spread for pre-game** (2026-02-08): USC-PennSt has OI=139K but REST book spread is 11-58%. OI measures total positions, not active depth near mid. Need to track book spread separately from OI.
- **Entry slippage guard threshold (3%) may be too tight for CBB** (2026-02-08): Tulsa-SFL narrowest at 3.6% — barely over threshold. Could consider TP_PCT * 0.6 (= 3.6%) to allow the tightest CBB markets through, but risk-reward is thin. Need data from live games and NFL to calibrate.
- **Live CBB game events cause gap risk through SL** (2026-02-08): NCG-Furman gapped from mid=0.447 to mid=0.488 in a single 5-sec tick (live game event). Actual loss 15.3% vs 6% SL target (2.5x). This is inherent to live sports — no code fix possible. The entry was clean (OI=6.7K, 5282 shares at bid, 0.1% slippage), the loss was pure adverse selection from a real game event.
- **Close fallback IOC used slippage pricing, not book** (2026-02-08): UCF-Cin BUY_LONG close needed sell at bid=0.283 but fallback priced at mid/1.03=0.308 → 6 expired orders, eventually filled on retry. Fixed by adding book-based close pricing matching entry approach. `[ACTED]`
- **Overall live trade stats: 1W/3L, -$0.328** (2026-02-08): Avg SL loss ($0.126) is 1.8x avg TP win ($0.070). SL gap slippage is systematic in live sports. FADE strategy has structural disadvantage: signals during live games are real information (game events), not noise to revert.
- **CRITICAL BUG: `_calc_profit_pct` used wrong denominator for BUY_NO** (2026-02-08): Formula was `(entry-current)/entry` but should be `(entry-current)/(1-entry)`. At mid=0.238, SL triggered at -14.7% when real NO-side loss was only -4.6%. Caused false SL on Mary-MinnSt (trade would have survived). At low mids, SL/TP fire on 1-2 cent moves instead of 4-5 cents. Fixed by dividing by `(1-entry)` for BUY_NO. BUY_LONG unaffected. `[ACTED]`
- **CRITICAL BUG: TP used mid price, not executable price** (2026-02-08): Mary-MinnSt BUY_LONG at 0.318. Ask spiked from 0.318→0.363, mid inflated to 0.339 (6.8% "profit"), TP fired. But sell crossed bid at 0.315 (below entry) → -$0.019 loss on a "take profit" exit. Fix: `get_executable_exit_price()` returns bid (SELL_LONG) or ask (SELL_SHORT) for TP and trailing stop evaluation. SL/breakeven still use mid to avoid false exits from spread noise. `[ACTED]`
- **Session 2 live stats: 1W/3L, -$0.133** (2026-02-08): 1 clean TP (+$0.012), 1 false TP (-$0.019), 1 game-event SL (-$0.116), 1 breakeven (-$0.010). Equity $8.57→$8.48. Two bugs found and fixed (profit formula, mid-based TP). Cumulative live: 2W/6L.
- **Session 3 live stats: 0W/1L, -$0.039** (2026-02-08): SAC-NO BUY_NO, pre-game NBA. Entry slippage 2.96% barely passed 3.0% guard. Price completely flat for 10 min (bid stuck at 0.309), breakeven exit. Loss is just fill spread + fees. All other signals (SEA-NE NFL, PVAM-FLAM CBB, DET-CHA NBA, ATL-MIN NBA) correctly rejected by slippage guard.
- **Pre-game NBA confirms same pattern as pre-game CBB** (2026-02-08): Wide spreads, flat prices, breakeven exits at a loss. Both Chi-BKN ($-0.010) and SAC-NO ($-0.039) were flat pre-game entries. Cumulative live: 2W/7L, $-0.172.
- **API rate concern: uncached `get_executable_exit_price` was hammering `/book` endpoint** (2026-02-08): 3-4 API calls/sec per position during hold. Fixed by sharing the 5-second `_price_cache` between `get_current_yes_mid` and `get_executable_exit_price` — now 1 call per 5 seconds total. `[ACTED]`
- **Live NBA games: 2 entries, 2 breakeven exits, net -$0.05** (2026-02-09): Utah-Miami BUY_NO at mid=0.293, breakeven 10min (-$0.024). Milwaukee-Orlando BUY_NO at mid=0.212, breakeven 10min (-$0.026). Both had tight entry spreads but prices didn't revert — NBA live games trend on in-game events just like CBB. Confirms FADE's structural weakness during live play.
- **StFPA-ChiSt (CBB) produced 6 entry attempts, all rejected by slippage guard** (2026-02-09): Spreads ranged 5-19%, all exceeding 3% entry slippage limit. Volatile mid-game CBB with wide books.
- **Scanner v2 false positive diagnosis** (2026-02-09): Scanner said HOT but trade bot only took 2 breakeven trades. Root causes: (1) scanner spread threshold 10% vs trade.py 3%, (2) counted z=7.0 TREND signals as tradeable, (3) no reversion tracking. Fixed in scanner v3.0. `[ACTED]`
- **Cumulative live stats: 2W/9L, -$0.22** (2026-02-09): The only reliable wins were CBB mid-range markets (0.35-0.55) with sufficient OI. Pre-game and live-game NBA consistently produce breakeven exits or worse.
- **All pre-game ACCEPT_FADE signals are untradeable (confirmed again)** (2026-02-09 evening): 10 ACCEPT_FADE signals generated during pre-game window — every single one had real book spreads of 5-15%, all rejected by entry slippage guard. Markets: iowa-maryland, stfpa-chist, iowa-maryland again, multiple CBB pre-game. WS BBO continues to flash narrow spreads that don't match REST book reality. Game phase filter (`BLOCK_PRE_GAME`) now provides cheaper rejection (no book fetch needed).
- **Monitor MID_MAX=0.88 vs trade.py MAX_MID_PRICE=0.65 mismatch wastes signals** (2026-02-09): iowa-maryland ACCEPT_FADE at mid=0.808 passes monitor's MID_MAX but trade.py always rejects at 0.65. Monitor should tighten MID_MAX to match trade.py to avoid unnecessary CSV writes and skip counter noise. `[ACTED]` — all three files now use 0.55.
- **Scanner v3.0 correctly stayed cold all session** (2026-02-09 evening): 0 FADE-ready, composite score 12-14/100 across the entire run. No false positives. Only 3-5/53 markets warmed up after 15+ minutes — scanner WS warmup is very slow, likely because pre-game BBO updates are infrequent (minutes between ticks).
- **jackst-arpb: live CBB game event SL, same pattern** (2026-02-09): BUY_NO at mid=0.227, SL hit as YES moved 0.227→0.281 during live game. -$0.092. Confirms live game FADE remains adverse selection — game events push prices directionally and don't revert. `[ACTED]` via game phase filter.
- **OI wipe on discover refresh still happening** (2026-02-09): Logs showed OI counts drop from 34/85 → 6/85 on 5-min refresh. Recovery takes several minutes of WS ticks. Known issue, not yet fixed.
- **game_phase classification bug: ALL markets were "LIVE"** (2026-02-10): API `startDate` is market creation time, not game start time. Since creation date is always in the past, `classify_game_phase()` returned "LIVE" for everything — including tomorrow's games. `BLOCK_PRE_GAME` was doing nothing. Fix: check slug date FIRST (tomorrow+=PRE_GAME, yesterday-=POST_GAME), use API dates only for same-day end_date checks. Today's games show UNKNOWN (can't determine game start from API). `[ACTED]`
- **MAX_MID_PRICE lowered from 0.65 to 0.55** (2026-02-10): BYU-Baylor at mid=0.631 was another breakeven loss in the 0.55-0.65 range. Historical winners cluster at 0.20-0.45. Tightened ceiling across all three files (monitor MID_MAX, trade MAX_MID_PRICE, scanner MAX_MID) to 0.55. `[ACTED]`
