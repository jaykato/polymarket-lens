# Phase 1 design

## Principles

- Use only the Gamma and CLOB APIs, and only their unauthenticated endpoints.
- Run on the Python 3.10+ standard library, with no external dependencies.
- Interpret API responses defensively, tolerating unknown added fields.
- Store both the normalized data and the raw response.
- Keep collection, storage, and web display separate, so packaging as an `.exe`
  stays possible later.

## Data flow

```text
Gamma API ── markets ───────┐
                            ├─ Collector ─ SQLite ─ local JSON API ─ Web UI
CLOB API ── history/book ───┘
```

## Safety boundaries

- `GET` is the only HTTP method issued.
- Query strings never reach SQL directly. Sort keys resolve through a table of
  known keys, and a numeric parameter that fails conversion is discarded.
- `raw_json` is never returned to the browser. It stays in the database for
  verification.
- The CLOB order creation and cancellation endpoints are not implemented.
- There are no configuration fields for private keys, wallets, or API
  credentials.
- The server binds to `127.0.0.1` alone by default.
- The interface states at all times that it is read-only and that nothing shown
  is a recommendation.

## Candidates for later work

1. Scheduled syncing and incremental updates
2. A parent-child model for events and markets
3. Order book depth and effective spread
4. Consistency checks across mutually exclusive markets
5. Calibration analysis using resolved markets
6. A packaged Windows `.exe` via PyInstaller

## Price movement summary

`movement.py` takes only the stored `price_history` as input. It makes no
additional API calls. What it returns is observation, not forecast and not
recommendation.

Because the observation interval differs per range, increments are normalized as
`Δp / √(Δt days)` before the standard deviation is taken, which gives the typical
daily swing. With fewer than three increments it returns None, and the interface
omits the row.

The three levels `Steady` / `Active` / `Volatile` describe the size of the
movement only. A large move is neither good nor bad, so they use a different
palette from the `good` / `caution` / `risk` levels in `analysis.py`.

## Sorting and filtering the list

Sort keys resolve through `database.MARKET_SORTS`; no string is passed into SQL
directly. An unknown key falls back to the default ordering.

Filtering is done on a liquidity floor. Gamma's `restricted` flag was true for
all 421 markets observed, so it could not filter anything. When the floor is 0
the condition is not added at all, because `NULL >= 0` evaluates to NULL in SQL
and markets with no liquidity figure would silently disappear.

## Chart range handling

`history.py` is the single place where a range key maps to its fetch parameters.

| Key | interval | fidelity | Display window |
|------|----------|----------|--------|
| `1d` | `1d` | 5 min | 24 hours |
| `1w` | `1w` | 60 min | 7 days |
| `1m` | `1m` | 180 min | 30 days |
| `max` | `max` | 720 min | unbounded |

An unknown key falls back to `1w`, so an unvalidated query string cannot raise.

Whether a range has already been fetched is tracked per range in the
`history_fetches` table. Points at different resolutions accumulate in the same
`price_history` table and are narrowed by the display window when read back.
Re-fetching the same range is suppressed for 15 minutes.

The interface draws the x axis on real time. Time ticks are not evenly spaced;
they are placed at round positions aligned to day and hour boundaries. The y
axis is fixed at 0–100%, with no automatic zoom to the data range, so that a
small move never looks like a large one.

## Market condition analysis

`analysis.py` computes these internal measures on a 0–100 scale.

- Pricing: the bid/ask spread
- Exit flexibility: a logarithmic score on liquidity
- Participation: a logarithmic score on cumulative volume
- Execution: the three combined at 50% / 35% / 15%

The internal values exist for reproducibility and testing; they are never shown
as numbers in the interface. The interface converts `good` / `caution` / `risk`
into a colour, a bar, and a short sentence. This assessment describes transaction
cost and market depth, not directional prediction or the chance of profit.

---

このアプリは日本からの投資を勧誘するものではありません。Polymarket対象国の投資家・トレーダーが、マーケットの可視化を効率よくするツールです。日本からはPolymarketへ投資・トレードを行うことはできません。
