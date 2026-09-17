# Security Policy

## Supported Versions

Version 2.0.x on Fedora 44 and Nobara systems based on Fedora 44 is currently
supported.

| Version | Platform | Supported |
| ------- | -------- | --------- |
| 2.0.x | Fedora 44 and Fedora 44-based Nobara | Yes |
| Other versions or platforms | Any | No |

## Reporting a Vulnerability

Do not disclose vulnerability details in a public issue. Report them privately
through [GitHub private vulnerability reporting](https://github.com/l0ee/cooler-btop/security/advisories/new).

## Threat Model & Considerations

### 1. Headless Daemon API (`--daemon`)
The headless API binds to `127.0.0.1` by default. Its dashboard and HTTP API are
read-only: they expose system metrics and do not provide process-control
operations. A non-loopback bind is rejected unless `--auth-token` or
`COOLER_BTOP_AUTH_TOKEN` is configured. API clients should send that value as a
Bearer token. The dashboard accepts a `?token=` URL once and exchanges it for a
same-origin HttpOnly cookie for its native SSE connection; protect such URLs
and use TLS when the service is reachable beyond a trusted local network.

Process command arguments can contain sensitive values. Use `--privacy-mode` to
replace them with process names. SQLite logging is bounded to 100,000 rows by
default; `--log-retention` can lower or raise that cap within its documented
range.

### 2. Container Scope
The supplied Compose service runs as an unprivileged user with a read-only
filesystem, no Linux capabilities, and no host PID namespace or host `/proc`
mount. It reports its own container namespace rather than host-wide metrics.
No elevated permissions are needed for that supported configuration.

### 3. File Descriptor Exhaustion
Each Server-Sent Events (SSE) client holds an HTTP connection open. The daemon
limits active streams to 32 by default and is intended for a small number of
local dashboard clients, not as an internet-facing multi-user metrics service.
