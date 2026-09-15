# Fedora/Nobara 44 Desktop Release Design

**Status:** Approved in chat on 2026-09-13

## Purpose

Prepare Cooler btop 2.0.0 for its first public release as a working desktop
application on Nobara/Fedora 44. The release should be easy to install, find in
the desktop application menu, launch, use, diagnose, and remove without
replacing the distribution's `btop` package.

The existing application, telemetry, Textual interface, web dashboard, and RPM
packaging are the starting point. The release work focuses on delivery and
trust rather than adding generated feature ideas.

## Product Scope

The supported desktop product is the Fedora 44 noarch RPM. It installs the
`cooler-btop` command, terminal desktop launcher, icon, AppStream metadata, and
man page. Nobara systems based on Fedora 44 are included in the supported
target.

The wheel remains a CLI and developer distribution. It does not provide
desktop-menu integration. Daemon mode remains optional, local and read-only by
default, and is not installed as a service.

The canonical project URL is:

`https://github.com/l0ee/cooler-btop`

Version 2.0.0 has not previously been published and remains the first release
version.

## Non-Goals

The first release does not add:

- Flatpak, AppImage, Debian, macOS, or multi-version Fedora packaging.
- SQLite history browsing or retention configuration.
- Persisted TUI settings, layout editors, themes, plugins, or alert systems.
- New telemetry unless required to fix a release acceptance failure.
- A system service, automatic daemon startup, or browser-based process control.
- Privileged PMU access, destructive system controls, active scanning, or
  arbitrary command execution.
- A PyInstaller standalone executable.

The unsupported standalone-executable target and documentation are removed
rather than expanded into another release format.

## Desktop Launch Experience

The desktop file launches `cooler-btop --desktop` in a terminal. Desktop mode
uses the same Textual application as a normal CLI launch and changes only fatal
error handling:

- Exceptions during app construction and execution are caught at the CLI
  boundary.
- The terminal prints a clear failure heading, exception type, message, and a
  pointer to the project's issue tracker.
- When attached to an interactive terminal, desktop mode waits for Enter before
  returning so the desktop shell cannot immediately hide the error.
- Normal CLI mode returns a nonzero status without waiting for input.
- Successful exit behavior remains unchanged; `q` exits the TUI.

Telemetry sample failures continue to preserve the last valid snapshot and
retry. Their visible status includes a concise exception category instead of
showing only `RETRY`; detailed diagnostic text can remain in the existing
tooltip. This does not expose errors through the web API or persist them.

## User Documentation And Metadata

The README leads with the supported RPM desktop workflow:

1. Download the Fedora 44 RPM and `SHA256SUMS` from the same GitHub release.
2. Verify the RPM with `sha256sum --check`.
3. Install the local file with DNF.
4. Launch Cooler btop from the desktop application menu or with
   `cooler-btop`.
5. Exit with `q` and remove with DNF when desired.

The documentation labels the RPM as the desktop package and wheel installation
as CLI-only. It states the Fedora/Nobara 44 support boundary and that the RPM is
checksummed but unsigned until the maintainer creates and controls a GPG key.

Python, RPM, desktop, AppStream, man-page, changelog, and security metadata use
the same project name, version, release date, URL, and support statement.
`pyproject.toml` is the authoritative version source, with tests enforcing the
duplicated values required by other formats. Placeholder email addresses and
unsupported performance claims are removed. Text describing psutil is aligned
with the shipped dependency and fallback behavior.

Security reports go through GitHub private vulnerability reporting at the
canonical repository. The documentation instructs users not to disclose
vulnerability details in a public issue. Enabling private vulnerability
reporting in the repository settings is a manual publication prerequisite.

## Release Artifacts

The release build produces exactly these public artifacts for version 2.0.0:

- `cooler_btop-2.0.0-py3-none-any.whl`
- `cooler_btop-2.0.0.tar.gz`
- `cooler-btop-2.0.0-1.fc44.src.rpm`
- `cooler-btop-2.0.0-1.fc44.noarch.rpm`
- `SHA256SUMS`

`SHA256SUMS` covers the four package artifacts and not itself. Its entries are
stable, relative filenames sorted bytewise, with no unrelated files from
`dist/`.

The existing Makefile gains a release-oriented target that builds and validates
these artifacts. It does not install packages, invoke privilege escalation,
delete user data, compile native extensions, sign artifacts, publish releases,
or alter `/usr/bin/btop`.

## Continuous Integration And Publication

Normal CI retains Python package tests and adds a Fedora 44 job. The Fedora job:

- Installs declared build/test tools in an ephemeral CI environment.
- Runs the complete test suite.
- Validates desktop and AppStream metadata.
- Builds the source and binary RPMs.
- Checks RPM digests, dependencies, payload, scripts, capabilities, Provides,
  Conflicts, and Obsoletes.
- Confirms the RPM owns `/usr/bin/cooler-btop` and does not own, replace,
  provide, conflict with, or obsolete `btop`.
- Extracts or stages the package for a launcher smoke test without installing it
  over the runner's system `btop`.

A release workflow triggered by a `v*` tag reruns the release checks, creates
the five artifacts, creates the matching GitHub release, and attaches the
artifacts to it. Preparing the workflow does not create a tag, push code, or
publish a release during implementation.

The first RPM remains unsigned because no maintainer-controlled GPG key exists.
No key is generated or stored automatically. GPG signing is a documented
follow-up and does not block a clearly labeled checksummed first release.

## Task Management

`TASKS.md` becomes a short operational queue with these sections:

- `Release Blockers`
- `Next Release`
- `Later`
- `Needs Threat Model`
- `Done`

Release blockers contain only work required by this design and failed release
checks. `Next Release` contains no more than five approved, coherent features.
Long-term telemetry, UI, API, reporting, and integration ideas remain available
without becoming immediate work. Privileged, destructive, remote-control,
plugin, active-scanning, and sensitive-data features are isolated under `Needs
Threat Model`.

The original generated checklist is preserved in a clearly labeled archive for
traceability. Duplicates and already implemented entries are consolidated in
the operational queue rather than presented as hundreds of unfinished release
requirements.

## Testing Strategy

Changes are implemented test-first. Automated coverage includes:

- Desktop argument parsing and routing.
- Fatal app-construction and app-run failures in desktop and normal CLI modes.
- Desktop mode waiting only on an interactive error path.
- `q` exiting the Textual app.
- Visible telemetry retry status containing a safe error category.
- Cross-file version, URL, support, and release-date consistency.
- Wheel and sdist contents and installed console entry point.
- Exact release artifact and checksum-manifest contents.
- RPM payload and collision-safety contracts.
- Absence of native binaries and removed standalone-executable claims.

## Release Acceptance

The release candidate is acceptable only when all of the following pass:

- Complete Python test suite.
- Textual pilot launch, responsive layout, and `q` exit tests.
- Wheel build and clean-environment installation smoke test.
- Sdist extraction followed by the complete test suite.
- Fedora 44 source and binary RPM builds, including RPM `%check`.
- `desktop-file-validate` and `appstreamcli validate --no-net`.
- RPM digest, dependency, payload, scripts, capability, Provides, Conflicts,
  and Obsoletes inspection.
- Extracted RPM launcher version smoke test without system installation.
- Live Intel, NVIDIA, sensor, zram, and privacy telemetry smoke checks on the
  available development hardware.
- Correct `SHA256SUMS` verification for all four package artifacts.
- Confirmation that `cooler-btop` is not installed during validation and the
  existing `/usr/bin/btop` package ownership is unchanged.

Any failed acceptance check becomes the highest-priority release blocker.
Optional feature work does not begin until the release candidate passes.

## Operational Constraints

- Do not install Cooler btop system-wide during implementation or validation.
- Do not replace, rename, remove, or modify `/usr/bin/btop`.
- Do not create, import, or expose signing keys.
- Do not compile or dynamically load native extensions.
- Do not publish, tag, commit, or push without an explicit user request.
- Report unsigned-artifact status and unavailable external publication checks
  plainly.
