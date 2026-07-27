# Architecture

How Polymarket Lens is put together, and why it is put together that way.

## Principles

1. **Public, unauthenticated APIs only.** Gamma and CLOB. Nothing that needs a
   key, and nothing that can move money.
2. **Standard library only.** Python 3.10+, no third-party packages. Anyone who
   has Python can run it without an install step.
3. **Read defensively.** API responses are parsed tolerantly and unknown fields
   are allowed through, because the upstream shape is not under our control.
4. **Keep both forms.** Normalised columns for querying, plus the raw response
   for later verification.
5. **Separate collection, storage, and presentation.** So the parts can be
   tested independently and packaged differently later.

## Data flow

```text
Gamma API ── markets ───────┐
                            ├─ Collector ─ SQLite ─ local JSON API ─ Web UI
CLOB API ── history/book ───┘
```

Nothing leaves the machine except `GET` requests to Polymarket. The web layer
binds to `127.0.0.1` and serves both the JSON API and the static front end.

## Modules

| Module | Responsibility |
|---|---|
| `client.py` | Public API client: throttling, retries with backoff, response shape checks |
| `collector.py` | Sync orchestration: markets, then histories and order books |
| `database.py` | SQLite schema, data access, sort definitions |
| `history.py` | Chart ranges and the fetch parameters each one needs |
| `analysis.py` | Market conditions: execution cost from spread, liquidity, volume |
| `movement.py` | Price movement summary computed from stored history |
| `search.py` | Local-first search with public-search fallback, history backfill |
| `web.py` | Local HTTP server and JSON API |
| `cli.py` | `init` / `sync` / `stats` / `serve` |
| `static/` | Front end: HTML, CSS, vanilla JavaScript |

The front end has no build step and no external requests. The price chart is
drawn on a `<canvas>` by hand rather than pulling in a charting library, so
there is no CDN dependency and nothing to bundle.

## Storage

Every table lives in one SQLite file, `data/polymarket.db` by default.

### `markets`

Keyed on `condition_id`. Holds the question, dates, volume, liquidity, spread,
status flags, fee flag, and the raw API response.

### `outcomes`

Keyed on token ID. Links to a market, keeps the Gamma array position as
`outcome_index`, and stores the current price.

### `price_history`

Composite key of token ID and UTC Unix timestamp, so re-fetching the same
moment updates the price instead of duplicating the row.

### `order_book_snapshots`

Best bid, best ask, spread, and the raw book at the time of capture.

> **Note:** this table is currently written but never read. Nothing in the API
> layer or the UI queries it. It is collected in anticipation of depth and
> effective-spread analysis, which is not yet built.

### `history_fetches`

Keyed on token ID and range key (`1d`, `1w`, `1m`, `max`). Records when each
range was last fetched and how many points came back. Switching chart ranges
serves stored data immediately and only calls the API for ranges that are
missing or older than fifteen minutes.

### Numeric handling

Money and prices are stored as decimal **strings** and converted to Python
`Decimal` for arithmetic, to avoid binary floating point error. Times are stored
in UTC and converted to the viewer's local zone only at display time.

## Chart ranges

`history.py` is the single place that maps a range to its fetch parameters.

| Key | `interval` | `fidelity` | Display window |
|---|---|---|---|
| `1d` | `1d` | 5 min | 24 hours |
| `1w` | `1w` | 60 min | 7 days |
| `1m` | `1m` | 180 min | 30 days |
| `max` | `max` | 720 min | unbounded |

Unknown keys resolve to `1w`, so an unvalidated query string cannot raise.

All fetched points accumulate in the same `price_history` table and are sliced
by window at display time. Viewing `1d` and then returning to `1w` therefore
leaves the last 24 hours denser than the rest of the week. Every point is a real
observation; nothing is interpolated or thinned.

The x axis is drawn on real timestamps rather than array position, so uneven
observation intervals stretch the line rather than hiding the gap. The y axis is
fixed at 0–100%; auto-zooming to the data range would make a one-point move look
dramatic.

## Market conditions

`analysis.py` normalises three inputs to 0–100:

- **Pricing** — bid/ask spread
- **Exit flexibility** — liquidity, on a log scale
- **Participation** — cumulative volume, on a log scale
- **Execution** — the three combined at 50% / 35% / 15%

The numeric scores are kept for test reproducibility and are **not** shown in
the UI. They are converted to `good` / `caution` / `risk`, then to a colour, a
bar, and a short sentence. This measures cost and depth, never direction or
profitability.

## Price movement

`movement.py` takes only stored history as input and makes no API calls.

Observation intervals differ between ranges, so increments are normalised by
`Δp / √(Δt in days)` before taking the standard deviation. That gives a typical
daily swing that is comparable across ranges. With fewer than three intervals it
returns nothing and the UI omits the row.

The result is labelled `Steady`, `Active`, or `Volatile`. These describe the
size of the movement only. Because a large move is neither good nor bad, they
use a different colour set from the `good` / `caution` / `risk` of market
conditions, so the two are never confused.

## Sorting and filtering

Sort keys resolve through a fixed table in `database.py`; a query string never
becomes SQL. Unknown keys fall back to the default order.

Filtering uses a liquidity floor. An earlier attempt used Polymarket's
`restricted` flag, but all 421 markets observed had it set, so it could filter
nothing. When the floor is zero no condition is added at all, because `NULL >= 0`
evaluates to `NULL` in SQL and would silently drop markets whose liquidity is
unknown.

## Safety boundaries

- `GET` only.
- Order creation and cancellation endpoints are not implemented.
- No configuration field accepts a key, wallet, or credential.
- The server binds to `127.0.0.1` by default.
- Query strings never reach SQL directly; numeric parameters that fail to parse
  are disabled rather than guessed.
- `raw_json` is stored but never sent to the browser.

## Tests

53 tests, all offline, run with `python -m unittest discover`. They read saved
API samples from `docs/api_samples/` so no network is required. `test_web.py`
starts a real server on an ephemeral port with a stubbed API client.

## Not yet built

Listed roughly by how much they would add:

1. Calibration analysis against resolved markets — the only item here with a
   real chance of revealing a systematic pricing bias. Requires collecting
   resolved markets first; the database currently holds none.
2. Consistency checks across mutually exclusive markets.
3. Order book depth and effective spread for a given size — the data is already
   being collected and simply is not used.
4. Scheduled sync and incremental updates.
5. Event and market parent/child modelling.
6. A packaged Windows executable.

---

このアプリは日本からの投資を勧誘するものではありません。Polymarket対象国の投資家・トレーダーが、マーケットの可視化を効率よくするツールです。日本からはPolymarketへ投資・トレードを行うことはできません。
