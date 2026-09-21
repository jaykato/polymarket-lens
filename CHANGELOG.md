# Changelog

All notable user-facing changes are recorded in this file. Version numbers use
[Semantic Versioning](https://semver.org/).

## [0.7.0] - 2026-09-21

### Added

- Order-book-aware entry-cost and return calculator for a selected YES/NO side
  and stake, including average fill, estimated fee, effective cost, break-even
  probability, and order-book capture time.
- Entry-cost market sorting and on-demand order-book retrieval.
- Resolved-market syncing, calibration reports, paper-only position scanning and
  settlement, and event-level data-consistency checks.
- An optional local Chronos-2 forecasting command; its dependencies are kept
  separate in `requirements-model.txt`.
- Research and model-selection documentation.
- A boxed **How to use this?** link in the local interface, serving the existing
  usage guide as a separate page.

### Changed

- Expanded the README and usage guide to cover the new analysis workflow and
  optional model installation.
- Increased offline automated-test coverage.

## [0.6.1] - 2026-07-27

First public release of Polymarket Lens.

### Added

- A read-only local viewer for public Polymarket market data; no wallet, private
  key, API key, or order-placement capability.
- Search, sorting, a liquidity filter, and market-detail views.
- Price-history charts for 1D, 1W, 1M, and MAX ranges, plotted using recorded
  timestamps.
- Movement summaries and market-condition indicators based on spread, liquidity,
  and volume.
- Local SQLite storage, startup and sync scripts for Windows, and offline tests.
- An English usage guide, architecture documentation, and public API samples.
- Optional donation addresses and QR codes in the README.

### Compatibility

- Tested on Windows 11 with Python 3.10 or newer.
- The base viewer uses only the Python standard library.

[0.7.0]: https://github.com/jaykato/polymarket-lens/releases/tag/v0.7.0
[0.6.1]: https://github.com/jaykato/polymarket-lens/releases/tag/v0.6.1
