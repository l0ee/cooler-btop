# Changelog

## [2.0.0] - 2026-09-13

### Added
- **Textual TUI**: Rewrote the terminal interface using the Textual framework.
- **Braille Graphs**: Integrated high-density Unicode Braille mapping for Network and CPU history.
- **CPU Core Heatmaps**: Replaced flat bars with color-coded density grids for per-core monitoring.
- **Process Trees**: Added hierarchical process-tree sorting.
- **Headless Daemon**: Added an optional HTTP/SSE metrics server (`--daemon`).
- **Web Dashboard**: Added a self-contained HTML dashboard with local CSS and vanilla canvas graphs.
- **Read-only Web API**: Added JSON and SSE metrics endpoints without remote process controls.
- **Historical SQLite Logging**: Added optional metric recording via `--log-db`.

### Changed
- **Linux Telemetry**: Uses direct `/proc` and `/sys` readers with psutil fallbacks.
- **Bandwidth Usage**: Compresses REST payloads with `gzip` when clients support it.
- **Packaging**: Ships pure Python wheels and a local source archive suitable for RPM builds.

### Fixed
- Stabilized Textual `DataTable` rendering, eliminating Row/Column lookup crashes.
- Fixed unclosed file descriptor `ResourceWarnings` across the `FastTelemetry` garbage collection sweeps.
- Removed obsolete system service packaging and unsafe container privileges.
