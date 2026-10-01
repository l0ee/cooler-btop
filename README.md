# Cooler btop 2.0.0

Cooler btop is a terminal system monitor with an optional local, read-only web
dashboard. The supported desktop package targets Fedora 44 and Nobara systems
based on Fedora 44.

## At a glance

| Area | Capabilities |
| --- | --- |
| Terminal | Textual interface, process filtering, per-core views, and history graphs |
| Telemetry | CPU, memory, storage, network, GPU, listening ports, and processes |
| Local dashboard | Optional read-only web interface with JSON and server-sent events |
| History | In-memory capture/replay and optional SQLite logging in daemon mode |
| Packaging | Python CLI distribution, Fedora/Nobara 44 desktop RPM, and container workflow |

Use the [desktop installation](#fedoranobara-44-desktop-installation) for the supported RPM, or see the [Python package](#cli-only-python-package) and [container](#container-daemon) options.

## Fedora/Nobara 44 Desktop Installation

The first RPM is checksummed but unsigned. The v2.0.0 tag predates the current
release hardening, so verify the exact `SHA256SUMS` entry as shown below and do
not treat that RPM as cryptographically authenticated. These commands download the
Fedora 44 binary RPM and `SHA256SUMS` from the same GitHub release, verify only
the downloaded RPM's exact manifest entry, and install it with DNF:

```bash
curl --fail --location --remote-name "https://github.com/l0ee/cooler-btop/releases/download/v2.0.0/cooler-btop-2.0.0-1.fc44.noarch.rpm" &&
curl --fail --location --remote-name "https://github.com/l0ee/cooler-btop/releases/download/v2.0.0/SHA256SUMS" &&
awk '$2 == "cooler-btop-2.0.0-1.fc44.noarch.rpm" { print }' SHA256SUMS | sha256sum --check - &&
sudo dnf install ./cooler-btop-2.0.0-1.fc44.noarch.rpm
```

New tagged releases publish a GitHub artifact attestation covering every file
listed in `SHA256SUMS`; maintainer verification instructions are in
`CONTRIBUTING.md`. Release tags must be protected signed annotated tags; the
workflow rejects unprotected or lightweight tags and tags outside reviewed
`main` history, but cannot verify a maintainer's local key.

Launch **Cooler btop** from the desktop application grid or run
`cooler-btop` in a terminal. Press `q` to exit. The RPM installs
`/usr/bin/cooler-btop`; it does not replace or conflict with `btop`, install a
system service, or start daemon mode.

Remove the desktop package with:

```bash
sudo dnf remove cooler-btop
```

## Features

- A Textual terminal interface for CPU, memory, storage, network, GPU,
  IPv4 TCP listening-port, and process telemetry.
- Process filtering and sorting, per-core CPU views, Braille history graphs,
  and an in-memory capture/replay mode.
- An optional JSON/SSE API and local web dashboard started with `--daemon`.
- Optional SQLite metric logging in daemon mode with `--log-db`.
- Direct Linux `/proc` and `/sys` readers with psutil fallbacks.

## Terminal Controls

- `q`: exit
- `tab`: cycle panes
- `f`: filter processes
- `k`: request termination of the selected process
- `c`: sort by CPU
- `m`: sort by memory
- `r`: start or stop an in-memory recording
- `e`: replay a recording
- `p`: pause or resume
- `[` / `]`: step through replay samples
- `l`: return to live monitoring

Recordings hold up to 300 samples, remain in memory, and are lost when the
application exits. Process inspection and termination are disabled during
replay.

## CLI-only Python Package

The wheel is a CLI-only distribution and does not install desktop-menu
metadata. Build a local wheel without installing anything system-wide with:

```bash
./install.sh
```

The script prints an optional per-user installation command. It never invokes
privilege escalation, installs a service, or starts daemon mode.

Build the wheel and source archive with `make distributions`. If local RPM
tooling is already installed, `make rpm-source` creates a source RPM from
`dist/cooler_btop-2.0.0.tar.gz` without installing it.

## Container Daemon

The container bind is non-loopback, so Compose requires an authentication token
before it will start:

```bash
export COOLER_BTOP_AUTH_TOKEN="use-a-long-random-value"
docker compose up --build
```

This starts the read-only dashboard on
`http://127.0.0.1:8080/?token=$COOLER_BTOP_AUTH_TOKEN`. The token in the URL is
exchanged for an HttpOnly same-origin cookie so the browser's native SSE client
can authenticate. The query token is accepted only on the dashboard root, not
on API or SSE routes. Treat that URL as a secret; for scripted API access,
prefer an `Authorization: Bearer ...` header. The daemon binds `0.0.0.0` inside the
container so the published port works, but Compose exposes it only on the host
loopback interface. The container runs without host PID mode, host `/proc` or
`/sys` mounts, blanket privileges, or Linux capabilities. Its metrics describe
the container's process and resource namespace, not the host.

Outside the container, daemon mode binds to `127.0.0.1` by default. If you bind
it to another interface, provide `--auth-token` (or
`--auth-token-file`, `COOLER_BTOP_AUTH_TOKEN`, or
`COOLER_BTOP_AUTH_TOKEN_FILE`) and apply network access controls because
metrics may include host and process information. Inline `--auth-token` values
are visible in shell history and process listings; prefer the file option with
mode `0600` or an environment variable supplied by a secret manager. The
daemon serves plain HTTP and has no built-in TLS, so use a TLS reverse proxy,
VPN, or SSH tunnel before exposing it beyond a trusted local machine. Use
`--privacy-mode` to replace process command arguments with process names, and
`--log-retention N` to cap SQLite history at N rows. SQLite metric files are
created owner-readable only (`0600`); keep their parent directory private too.

## Development

Use **Python 3.9 or newer** and an isolated virtual environment:

```bash
git clone https://github.com/l0ee/cooler-btop.git
cd cooler-btop
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m cooler_btop
```

| Command | Purpose |
| --- | --- |
| `make test` | Run the unittest suite |
| `make package-check` | Check packaging behavior |
| `make coverage` | Measure coverage with the configured minimum |
| `make distributions` | Build the wheel and source archive |

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and release guidance and [SECURITY.md](SECURITY.md) for vulnerability reporting.

## License

Released under the [MIT License](LICENSE).
