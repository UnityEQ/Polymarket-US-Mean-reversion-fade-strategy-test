"""
basic.py — Simple examples for the Polymarket US API.

Set environment variables before running:
    $env:POLYMARKET_KEY_ID = "your-key-id"
    $env:POLYMARKET_SECRET_KEY = "your-base64-secret"

Commands:
    python basic.py                  Run all REST examples (balance, markets, market detail, order book)
    python basic.py markets [N]      List N active markets (default 20)
    python basic.py market <slug>    Get single market details by slug
    python basic.py book <slug>      Get order book with bids, offers, spread
    python basic.py balance          Get account balance
    python basic.py stream           Stream live BBO for ALL markets via wildcard subscribe
    python basic.py stream <slug>    Stream live BBO for a single market

Dependencies:
    pip install requests websocket-client cryptography
"""

import os, sys, json, time, base64
import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

# ─── Config ───────────────────────────────────────────────────────────
API_BASE = "https://api.polymarket.us"
WS_URL   = "wss://api.polymarket.us/v1/ws/markets"

KEY_ID     = os.getenv("POLYMARKET_KEY_ID", "")
SECRET_KEY = os.getenv("POLYMARKET_SECRET_KEY", "")

if not KEY_ID or not SECRET_KEY:
    print("Set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY env vars.")
    print("Generate at: https://polymarket.us/developer")
    sys.exit(1)

# ─── Auth ─────────────────────────────────────────────────────────────
# Ed25519 signing: message = timestamp_ms + method + path (no query string)

_private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
    base64.b64decode(SECRET_KEY)[:32]
)

def _sign(method: str, path: str) -> dict:
    """Build auth headers for a REST request."""
    ts = str(int(time.time() * 1000))
    sig = _private_key.sign(f"{ts}{method}{path}".encode())
    return {
        "X-PM-Access-Key": KEY_ID,
        "X-PM-Timestamp": ts,
        "X-PM-Signature": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }

def get(path: str, params: dict = None) -> dict:
    """Authenticated GET request."""
    headers = _sign("GET", path)  # sign path only, no query string
    r = requests.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

# ─── Helper ───────────────────────────────────────────────────────────
def amount(obj) -> str:
    """Extract value from Amount object {value, currency} or raw value."""
    if isinstance(obj, dict):
        return obj.get("value", "?")
    return str(obj) if obj is not None else "—"

# ─── Example 1: List active markets ──────────────────────────────────
def list_markets(limit: int = 20):
    """GET /v1/markets — list active markets."""
    data = get("/v1/markets", {"limit": str(limit), "active": "true", "closed": "false"})
    markets = data.get("markets", [])
    print(f"\n{'='*60}")
    print(f" Active Markets ({len(markets)} shown)")
    print(f"{'='*60}")
    for m in markets:
        slug = m.get("slug", "?")
        title = m.get("title", m.get("question", "?"))
        state = m.get("state", "?")
        print(f"  {slug}")
        print(f"    {title}  [{state}]")
    print()

# ─── Example 2: Get single market by slug ────────────────────────────
def get_market(slug: str):
    """GET /v1/market/slug/{slug} — note: singular /market/, not /markets/."""
    data = get(f"/v1/market/slug/{slug}")
    # API wraps response in {"market": {...}}
    m = data.get("market", data)
    print(f"\n{'='*60}")
    print(f" Market: {slug}")
    print(f"{'='*60}")
    print(f"  Title:   {m.get('title', m.get('question', '?'))}")
    print(f"  State:   {m.get('state', '?')}")
    print(f"  Active:  {m.get('active', '?')}")
    start = m.get("startDate", m.get("start_date", "—"))
    end = m.get("endDate", m.get("end_date", "—"))
    print(f"  Start:   {start}")
    print(f"  End:     {end}")
    print()

# ─── Example 3: Get order book ───────────────────────────────────────
def get_book(slug: str):
    """GET /v1/markets/{slug}/book — full order book with bids and offers."""
    data = get(f"/v1/markets/{slug}/book")
    md = data.get("marketData", {})
    bids = md.get("bids", [])
    offers = md.get("offers", [])
    state = md.get("state", "?")

    print(f"\n{'='*60}")
    print(f" Order Book: {slug}  [state: {state}]")
    print(f"{'='*60}")
    print(f"  {'BIDS':<30} {'OFFERS'}")
    print(f"  {'─'*28}   {'─'*28}")
    rows = max(len(bids), len(offers))
    for i in range(min(rows, 10)):
        bid_str = ""
        ask_str = ""
        if i < len(bids):
            bid_str = f"${amount(bids[i].get('px')):>6}  x {float(bids[i].get('qty', 0)):>10.2f}"
        if i < len(offers):
            ask_str = f"${amount(offers[i].get('px')):>6}  x {float(offers[i].get('qty', 0)):>10.2f}"
        print(f"  {bid_str:<30} {ask_str}")

    if bids and offers:
        best_bid = float(amount(bids[0].get("px")))
        best_ask = float(amount(offers[0].get("px")))
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        print(f"\n  Best bid: ${best_bid:.3f}  |  Best ask: ${best_ask:.3f}")
        print(f"  Mid: ${mid:.4f}  |  Spread: ${spread:.4f} ({spread/mid*100:.1f}%)")
    print()

# ─── Example 4: Get account balance ──────────────────────────────────
def get_balance():
    """GET /v1/account/balances"""
    data = get("/v1/account/balances")
    print(f"\n{'='*60}")
    print(f" Account Balance")
    print(f"{'='*60}")
    for b in data.get("balances", []):
        curr = b.get("currency", "?")
        bal = b.get("currentBalance", b.get("balance", "?"))
        print(f"  {curr}: ${bal}")
    print()

# ─── Example 5: Stream live BBO via WebSocket ────────────────────────
def stream_bbo(slugs: list = None):
    """
    Subscribe to real-time best-bid/best-ask updates.

    - market_slugs: []     → wildcard, streams ALL active markets
    - market_slugs: [slug] → single market only
    """
    from websocket import WebSocketApp

    target = slugs if slugs else []
    label = ", ".join(target) if target else "ALL markets (wildcard)"

    def on_open(ws):
        print(f"\n  Connected — subscribing to {label}")
        ws.send(json.dumps({
            "subscribe": {
                "request_id": "sub_1",
                "subscription_type": 2,  # MARKET_DATA_LITE
                "market_slugs": target,
            }
        }))

    seen = set()

    def on_message(ws, raw):
        data = json.loads(raw)

        # Skip heartbeats
        if "heartbeat" in data:
            return

        # BBO update — server uses camelCase
        bbo = data.get("marketDataLite")
        if not bbo:
            return

        slug = bbo.get("marketSlug", "?")
        bid  = amount(bbo.get("bestBid"))
        ask  = amount(bbo.get("bestAsk"))
        last = amount(bbo.get("lastTradePx"))
        oi   = amount(bbo.get("openInterest"))

        is_new = slug not in seen
        seen.add(slug)
        tag = " [NEW]" if is_new else ""

        print(f"  {slug:<50} bid={bid:>6}  ask={ask:>6}  last={last:>6}  OI={oi:>10}{tag}")

        # After initial snapshot, print count
        if is_new and len(seen) % 50 == 0:
            print(f"  --- {len(seen)} unique markets streaming ---")

    def on_error(ws, err):
        print(f"  Error: {err}")

    def on_close(ws, code, msg):
        print(f"  Closed (code={code}): {msg}")
        print(f"  Total unique markets seen: {len(seen)}")

    # Auth headers for WebSocket handshake
    ts = str(int(time.time() * 1000))
    sig = _private_key.sign(f"{ts}GET/v1/ws/markets".encode())
    headers = [
        f"X-PM-Access-Key: {KEY_ID}",
        f"X-PM-Timestamp: {ts}",
        f"X-PM-Signature: {base64.b64encode(sig).decode()}",
    ]

    print(f"\n{'='*60}")
    print(f" Live BBO Stream")
    print(f"{'='*60}")
    print(f"  Press Ctrl+C to stop\n")

    ws = WebSocketApp(WS_URL, header=headers,
                      on_open=on_open, on_message=on_message,
                      on_error=on_error, on_close=on_close)
    ws.run_forever(ping_interval=30, ping_timeout=10)

# ─── CLI ──────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "all"

    if cmd == "markets":
        limit = int(args[1]) if len(args) > 1 else 20
        list_markets(limit)

    elif cmd == "market":
        if len(args) < 2:
            print("Usage: python basic.py market <slug>")
            return
        get_market(args[1])

    elif cmd == "book":
        if len(args) < 2:
            print("Usage: python basic.py book <slug>")
            return
        get_book(args[1])

    elif cmd == "balance":
        get_balance()

    elif cmd == "stream":
        slugs = args[1:] if len(args) > 1 else None
        stream_bbo(slugs)

    elif cmd == "all":
        print("\nRunning all examples (except stream)...\n")
        get_balance()
        list_markets(10)
        # Grab first slug from market list for detail examples
        data = get("/v1/markets", {"limit": "1", "active": "true"})
        markets = data.get("markets", [])
        if markets:
            slug = markets[0].get("slug", "")
            if slug:
                get_market(slug)
                get_book(slug)
        print("Run 'python basic.py stream' for live WebSocket BBO.\n")

    else:
        print(f"Unknown command: {cmd}")
        print("Commands: markets, market <slug>, book <slug>, balance, stream [slug]")

if __name__ == "__main__":
    main()
