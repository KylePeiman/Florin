# Kalshi Authentication

Kalshi uses RSA-PSS (SHA-256) for all authenticated REST and WebSocket requests. This document covers the signing scheme, header format, key setup, and common errors.

---

## Table of Contents

- [API Key Setup](#api-key-setup)
- [Signing Scheme](#signing-scheme)
- [Request Headers](#request-headers)
- [WebSocket Authentication](#websocket-authentication)
- [Balance and Order Placement](#balance-and-order-placement)
- [Common Errors](#common-errors)
- [Implementation Reference](#implementation-reference)

---

## API Key Setup

1. Log in to [kalshi.com](https://kalshi.com) and go to **Profile → API Keys**.
2. Create a new API key and download the PEM private key file.
3. Set the following in your `.env` file:

```env
KALSHI_API_KEY_ID=your-key-id-here
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi_private_key.pem
```

The key ID is a UUID shown on the API keys page. The private key file is a standard PEM-encoded RSA private key (no passphrase).

---

## Signing Scheme

**Algorithm**: RSA-PSS with SHA-256 digest, `salt_length = PSS.DIGEST_LENGTH`

**Sign string format**:

```
{timestamp_ms}{METHOD_UPPER}/trade-api/v2{path_no_query}
```

Where:
- `timestamp_ms` — current Unix time in milliseconds as a string (integer, no decimal)
- `METHOD_UPPER` — HTTP method in uppercase: `GET`, `POST`, `DELETE`
- `/trade-api/v2{path_no_query}` — the full path including the `/trade-api/v2` prefix, with any query string stripped

**Example** for `GET /markets?limit=100`:

```
1710000000000GET/trade-api/v2/markets
```

**Signature**: base64-encoded RSA-PSS signature of the UTF-8 encoded sign string.

```python
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
import base64, time

ts = str(int(time.time() * 1000))
path_no_query = "/markets"   # strip ?limit=100
sign_path = f"/trade-api/v2{path_no_query}"
message = (ts + "GET" + sign_path).encode()

signature = private_key.sign(
    message,
    asym_padding.PSS(
        mgf=asym_padding.MGF1(hashes.SHA256()),
        salt_length=asym_padding.PSS.DIGEST_LENGTH,
    ),
    hashes.SHA256(),
)
sig_b64 = base64.b64encode(signature).decode()
```

---

## Request Headers

Every authenticated request must include:

| Header | Value |
|---|---|
| `KALSHI-ACCESS-KEY` | Your API key ID (the UUID) |
| `KALSHI-ACCESS-TIMESTAMP` | Unix timestamp in milliseconds (string, same value used in signing) |
| `KALSHI-ACCESS-SIGNATURE` | Base64-encoded RSA-PSS signature |

Header names are case-sensitive and use uppercase with hyphens. Do not use `X-` prefixes or lowercase variants.

---

## Base URL

```
https://api.elections.kalshi.com/trade-api/v2
```

The old `trading-api.kalshi.com` domain redirects with a `401`. Always use `api.elections.kalshi.com`.

---

## WebSocket Authentication

The Kalshi WebSocket endpoint:

```
wss://api.elections.kalshi.com/trade-api/ws/v2
```

Authentication uses the same RSA-PSS signing scheme, passed as HTTP headers during the WebSocket handshake. The `websockets` library accepts extra headers via the `extra_headers` parameter:

```python
import websockets

headers = auth_headers("GET", "/trade-api/ws/v2")
async with websockets.connect(url, extra_headers=headers) as ws:
    ...
```

After connection, subscribe to orderbook deltas for specific tickers:

```json
{
  "id": 1,
  "cmd": "subscribe",
  "params": {
    "channels": ["orderbook_delta"],
    "market_tickers": ["KXBTC-25JAN0800-T84000"]
  }
}
```

---

## Balance and Order Placement

### Check balance

```
GET /portfolio/balance
```

Response:
```json
{
  "balance": 10000,
  "portfolio_value": 12500
}
```

`balance` is in **cents** (integer). Divide by 100 for dollars.

### Place an order

```
POST /portfolio/orders
```

Request body:
```json
{
  "ticker": "KXBTC-25JAN0800-T84000",
  "action": "buy",
  "type": "limit",
  "side": "yes",
  "count": 5,
  "yes_price": 82
}
```

Use `"yes_price"` for YES orders and `"no_price"` for NO orders. Prices are in **cents** (1–99).

Response fields to check:
- `status`: `"executed"` when filled (not `"filled"`)
- `fill_count`: number of contracts filled (not `"filled_count"`)

If any leg of a multi-leg arb fails to fill within 2 seconds, all placed legs are cancelled via `DELETE /portfolio/orders/{order_id}`.

---

## Price Field Formats

Kalshi changed the API price field format in March 2026:

| Period | Field | Type | Unit |
|---|---|---|---|
| Before March 2026 | `yes_ask` | integer | cents (1–99) |
| After March 2026 | `yes_ask_dollars` | float | dollars (0.01–0.99) |

The `_parse_selections` method in `KalshiFetcher` handles both formats via a `_read()` fallback helper — it tries `yes_ask_dollars` first, then falls back to `yes_ask`, converting to cents in either case.

---

## Common Errors

| Symptom | Cause | Fix |
|---|---|---|
| `401 Unauthorized` on any request | Using old `trading-api.kalshi.com` domain | Use `api.elections.kalshi.com` |
| `401` on portfolio endpoints but not market endpoints | Sign string missing `/trade-api/v2` prefix | Include the full prefix in the sign path |
| `401` with correct domain and prefix | Timestamp drift — Kalshi rejects requests more than a few seconds old | Ensure system clock is synced (NTP) |
| `401` on WebSocket | Headers not passed during handshake | Pass `extra_headers` to `websockets.connect()` |
| `order.status` never `"executed"` | Checking for `"filled"` instead of `"executed"` | Check `status == "executed"` |
| Fill count is always 0 | Checking `filled_count` instead of `fill_count` | Use `fill_count` field |
| `cryptography` import error | `cryptography` package not installed | `pip install cryptography` |

---

## Implementation Reference

The full authentication implementation is in `src/fetchers/kalshi.py`:

- `_load_private_key(pem_path)` — loads the RSA private key from PEM
- `KalshiFetcher._auth_headers(method, path)` — builds and returns the three signed headers
- `KalshiFetcher.get_balance()` — fetches account balance in cents
- `KalshiFetcher.place_order(...)` — places a limit order and checks fill status

The streaming WebSocket auth is in `src/streaming/kalshi_ws.py`.
