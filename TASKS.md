# TASKS

## Release Blockers

## Next Release
- [ ] Browse SQLite history in the TUI with bounded retention.
- [ ] Add a privacy mode that masks process command arguments.
- [ ] Add local disk-space, CPU-temperature, and sustained-swap alerts.
- [ ] Add one-shot JSON and CSV export commands.
- [ ] Add per-process disk I/O to the process table.

## Later
- [ ] Review and promote justified ideas from `docs/generated-task-ideas.md` based on user requests and tested hardware.

## Needs Threat Model
- [ ] Review any privileged, destructive, remote-control, active-scanning, plugin, arbitrary-command, or sensitive-data feature before implementation.

## Done
- [x] (Cycle #65, 2026-09-15) Prepared the Cooler btop 2.0.0 Fedora/Nobara 44 release candidate; passed 165 tests, desktop/AppStream validation, live hardware/privacy telemetry, and wheel, sdist, source-RPM, binary-RPM payload and checksum verification.
- [x] (Cycle #64, 2026-09-13) Added safe unprivileged Intel i915 frequency telemetry, hardened hardware collection and privacy behavior, and produced verified wheel, source RPM, and Fedora/Nobara binary RPM artifacts; verified with 130 tests and live hybrid-GPU checks.
- [x] (Cycle #63, 2026-09-10) Added bounded TUI metrics recording and replay, sample stepping, original timestamps, and return-to-live controls; verified with 38 UI/server/collector tests.
