# Polymarket API initial verification

Verification date: 2026-07-25 (JST)

## Scope

The following unauthenticated, read-only endpoints were called successfully:

1. `GET https://gamma-api.polymarket.com/markets`
2. `GET https://clob.polymarket.com/book`
3. `GET https://clob.polymarket.com/prices-history`

Captured responses are stored under `docs/api_samples/`.

## Confirmed response behavior

- Gamma `GET /markets` returns a JSON object rather than a one-element array when
  `limit=1` at the time of verification.
- Several Gamma fields that are logically arrays are JSON-encoded strings:
  `outcomes`, `outcomePrices`, and `clobTokenIds`.
- `conditionId` is the stable market identifier used by the order book response.
- A market contains two CLOB token IDs. Their positions align with the corresponding
  entries in `outcomes`.
- Gamma returns a mixture of numeric strings (`volume`, `liquidity`), JSON numbers
  (`bestBid`, `bestAsk`), and booleans. Parsing must be explicit.
- CLOB order book `price`, `size`, timestamps, and minimum sizes are strings.
- Price history uses Unix seconds in `t` and a JSON number in `p`.
- The live market response includes fee metadata. Fee behavior must be read from
  response fields instead of assuming every market is fee-free.
- `restricted=true` appeared on the sampled market. It must not be interpreted as
  a resolution or active-state flag.

## Differences and updates relative to the project brief

- The brief's three API families remain correct: Gamma, CLOB, and Data.
- New integrations should prefer keyset pagination:
  `GET /markets/keyset` and `GET /events/keyset`. The offset endpoints still exist
  but are scheduled for future deprecation according to the official changelog.
- The current API rate limits are substantially higher than the conservative local
  target in the brief. The application should still self-limit, cache, and retry
  because public limits may change.
- Current live responses contain fee fields (`feesEnabled`, `feeType`,
  `feeSchedule`). The database schema should preserve these.

## Initial implementation decisions

- Keep the application read-only and use no wallet, private key, or trading API.
- Store raw API payloads for debugging before mapping them into normalized tables.
- Parse decimal values with `Decimal`, not binary floating point.
- Store timestamps in UTC and convert to the viewer's timezone only at display time.
- Use `conditionId` as the stable market key and token IDs as outcome-price keys.
- Treat optional and unknown fields defensively so API additions do not break
  collection.

## Official references

- https://docs.polymarket.com/market-data/overview
- https://docs.polymarket.com/api-reference/introduction
- https://docs.polymarket.com/api-reference/rate-limits
- https://docs.polymarket.com/changelog

---

このアプリは日本からの投資を勧誘するものではありません。Polymarket対象国の投資家・トレーダーが、マーケットの可視化を効率よくするツールです。日本からはPolymarketへ投資・トレードを行うことはできません。
