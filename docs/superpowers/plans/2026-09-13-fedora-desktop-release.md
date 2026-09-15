# Fedora/Nobara 44 Desktop Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a tested, checksummed Cooler btop 2.0.0 desktop release candidate for Nobara/Fedora 44 that is ready to publish from GitHub.

**Architecture:** Keep the existing Textual application and pure-Python package intact. Add a desktop-only failure path at the CLI boundary, make RPM delivery the documented desktop path, centralize release validation in Make/RPM tooling, and let GitHub Actions reproduce the same checks before attaching artifacts to a tagged release.

**Tech Stack:** Python 3.9+, unittest, Textual, setuptools, GNU Make, Fedora 44 RPM `%pyproject_*` macros, freedesktop desktop/AppStream metadata, Bash release checks, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-13-fedora-desktop-release-design.md`

## Global Constraints

- The first public version is exactly `2.0.0` and the RPM release is `1.fc44`.
- The supported desktop target is Fedora 44 and Nobara systems based on Fedora 44.
- The canonical URL is `https://github.com/l0ee/cooler-btop`.
- The RPM owns `/usr/bin/cooler-btop`, never `/usr/bin/btop`, and never Provides, Conflicts with, or Obsoletes `btop`.
- The release remains pure Python; do not compile or dynamically load native extensions.
- Do not install Cooler btop system-wide, invoke privilege escalation, alter `/usr/bin/btop`, create signing keys, publish releases, create tags, or push changes.
- The RPM is checksummed and explicitly unsigned until the maintainer controls a GPG key.
- This directory has no Git metadata. Replace commit steps with a read-only review checkpoint and do not initialize a repository unless the user explicitly requests it.

---

### Task 1: Make Desktop Launch Failures Actionable

**Files:**
- Modify: `test_main.py`
- Modify: `test_ui.py`
- Modify: `cooler_btop/main.py:235-287,380-400,528-577`
- Modify: `packaging/io.github.l0ee.cooler_btop.desktop:6`
- Modify: `test_packaging.py:297-308`

**Interfaces:**
- Consumes: `run_cli() -> int`, `BtopCloneApp.run()`, `BtopCloneApp.sample_error: str`.
- Produces: hidden CLI flag `--desktop`; helper `_run_tui(args) -> int`; desktop launcher command `cooler-btop --desktop`; visible retry state `RETRY <ExceptionType>`.

- [x] **Step 1: Add failing CLI tests for desktop and normal failure paths**

Add imports for `io` and `Mock` to `test_main.py`, then add tests that patch `sys.argv`, `cooler_btop.main.BtopCloneApp`, `sys.stdin`, and `sys.stderr`:

```python
def test_desktop_failure_stays_visible_and_returns_nonzero(self):
    from cooler_btop import main
    stdin = Mock()
    stdin.isatty.return_value = True
    stderr = io.StringIO()
    with patch.object(sys, "argv", ["cooler-btop", "--desktop"]), \
            patch.object(main, "BtopCloneApp", side_effect=RuntimeError("display failed")), \
            patch.object(main.sys, "stdin", stdin), \
            patch.object(main.sys, "stderr", stderr):
        self.assertEqual(main.run_cli(), 1)
    stdin.readline.assert_called_once_with()
    self.assertIn("RuntimeError: display failed", stderr.getvalue())
    self.assertIn("github.com/l0ee/cooler-btop/issues", stderr.getvalue())

def test_terminal_failure_does_not_wait_for_input(self):
    from cooler_btop import main
    app = Mock()
    app.run.side_effect = RuntimeError("display failed")
    stdin = Mock()
    with patch.object(sys, "argv", ["cooler-btop"]), \
            patch.object(main, "BtopCloneApp", return_value=app), \
            patch.object(main.sys, "stdin", stdin), \
            patch.object(main.sys, "stderr", io.StringIO()):
        self.assertEqual(main.run_cli(), 1)
    stdin.readline.assert_not_called()
```

- [x] **Step 2: Add failing TUI and desktop metadata tests**

Change `test_failed_sample_retains_snapshot_and_retries` to require `RETRY OSError` in the rendered context while retaining the full tooltip assertion. Add a pilot test that presses `q` and asserts `app.is_running` becomes false. Change the desktop assertion to:

```python
self.assertEqual(entry["Exec"], "cooler-btop --desktop")
self.assertEqual(entry["TryExec"], "cooler-btop")
```

- [x] **Step 3: Run focused tests and capture the expected failures**

Run: `python3 -m unittest -v test_main test_ui.TerminalPilotTests.test_failed_sample_retains_snapshot_and_retries test_ui.TerminalPilotTests.test_q_exits_application test_packaging.LinuxPackagingTests.test_desktop_launcher_runs_only_cooler_btop_in_a_terminal`

Expected: FAIL because `--desktop`, the error wait, visible exception category, `q` test behavior, and updated desktop command do not exist yet.

- [x] **Step 4: Implement the minimal desktop CLI boundary**

Import `sys` at module scope. Add an argparse flag with `help=argparse.SUPPRESS`. Move both app construction and execution inside one exception boundary:

```python
def _run_tui(args):
    try:
        app = BtopCloneApp(refresh_interval=args.interval, show_pet=not args.no_pet)
        app.run()
    except Exception as error:
        print(
            f"Cooler btop could not start.\n{type(error).__name__}: {error}\n"
            "Report reproducible failures at "
            "https://github.com/l0ee/cooler-btop/issues",
            file=sys.stderr,
        )
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        if args.desktop and sys.stdin.isatty():
            print("Press Enter to close this window.", file=sys.stderr)
            sys.stdin.readline()
        return 1
    return 0
```

Make `run_cli()` return `_run_tui(args)` for TUI mode. Update `_update_context()` so a sample error beginning with `OSError:` renders as `RETRY OSError` while the tooltip retains the full string. Set desktop `Exec=cooler-btop --desktop`.

- [x] **Step 5: Run focused tests and the full suite**

Run: `python3 -m unittest -v test_main test_ui test_packaging.LinuxPackagingTests.test_desktop_launcher_runs_only_cooler_btop_in_a_terminal`

Expected: PASS.

Run: `python3 -m unittest discover -v`

Expected: PASS with the increased test count.

- [x] **Step 6: Review checkpoint**

Inspect only the Task 1 files for input waits outside desktop error mode, exception leakage, changed daemon behavior, or weakened process-control safeguards. Record findings; no commit is possible because the directory is not a Git repository.

### Task 2: Align Public Documentation And Metadata

**Files:**
- Modify: `test_packaging.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `CHANGELOG.md`
- Modify: `packaging/io.github.l0ee.cooler_btop.metainfo.xml`
- Modify: `packaging/cooler-btop.spec`
- Modify: `packaging/cooler-btop.1`
- Modify: `Makefile`
- Modify: `MANIFEST.in`

**Interfaces:**
- Consumes: canonical package version and URL from `pyproject.toml`.
- Produces: consistent 2.0.0 release metadata dated `2026-09-13`; RPM-first desktop instructions; GitHub security-reporting path; no PyInstaller release claim.

- [x] **Step 1: Add failing release-contract tests**

In `test_packaging.py`, parse `pyproject.toml`, AppStream XML, RPM spec, desktop file, changelog, README, SECURITY, and Makefile. Assert:

```python
self.assertEqual(project["version"], "2.0.0")
self.assertEqual(project["urls"]["Homepage"], "https://github.com/l0ee/cooler-btop")
self.assertEqual(project["authors"], [{"name": "l0ee"}])
self.assertEqual(release.attrib, {"version": "2.0.0", "date": "2026-09-13"})
self.assertIn("* Sun Sep 13 2026 l0ee <l0ee@users.noreply.github.com> - 2.0.0-1", spec)
self.assertIn("dnf install ./cooler-btop-2.0.0-1.fc44.noarch.rpm", readme)
self.assertIn("dnf remove cooler-btop", readme)
self.assertIn("security/advisories/new", security)
self.assertNotIn("build-exe", makefile + readme)
self.assertNotIn("entirely stripped out `psutil`", changelog.casefold())
```

Also assert the README clearly contains `Fedora 44`, `Nobara`, `CLI-only`, `unsigned`, and checksum verification guidance.

- [x] **Step 2: Run the focused contract tests and capture RED**

Run: `python3 -m unittest -v test_packaging.SourceLayoutTests test_packaging.LinuxPackagingTests`

Expected: FAIL on placeholder author email, inconsistent dates, missing project URLs/install guidance/security URL, and remaining `build-exe`/psutil claims.

- [x] **Step 3: Make pyproject metadata authoritative**

Use this public identity in `pyproject.toml`:

```toml
authors = [
    {name = "l0ee"}
]

[project.urls]
Homepage = "https://github.com/l0ee/cooler-btop"
Issues = "https://github.com/l0ee/cooler-btop/issues"
Security = "https://github.com/l0ee/cooler-btop/security/advisories/new"
```

Keep the current dependency ranges and pure-Python package configuration.

- [x] **Step 4: Rewrite publication-facing documentation**

Replace promotional and unsupported performance wording in the README with factual capabilities. Lead with Fedora/Nobara 44 desktop installation, checksum verification, app-grid/CLI launch, `q` exit, and DNF removal. Label wheel installation CLI-only and keep daemon/container security boundaries. Remove the standalone binary section.

Update SECURITY to support 2.0.x on Fedora/Nobara 44 and link private GitHub vulnerability reporting. Correct CHANGELOG to state that direct `/proc` and `/sys` readers are used with psutil fallbacks, and date 2.0.0 as 2026-09-13.

- [x] **Step 5: Synchronize Linux metadata**

Set AppStream release date to `2026-09-13`; add bug-tracker and VCS URLs. Update the RPM changelog to:

```spec
* Sun Sep 13 2026 l0ee <l0ee@users.noreply.github.com> - 2.0.0-1
- Initial Fedora 44 and Nobara desktop package
```

Document `--desktop` in the man page as an application-menu error-handling mode. Remove `build-exe` from `.PHONY` and delete its Make recipe. Change `MANIFEST.in` to include all `.github/workflows/*.yml` release inputs.

- [x] **Step 6: Validate metadata and run tests**

Run: `python3 -m unittest -v test_packaging`

Run: `desktop-file-validate packaging/io.github.l0ee.cooler_btop.desktop`

Run: `appstreamcli validate --no-net packaging/io.github.l0ee.cooler_btop.metainfo.xml`

Expected: all PASS.

- [x] **Step 7: Review checkpoint**

Review all public claims against the actual code and artifacts. Confirm instructions never claim broad Linux support, signed RPMs, automatic service installation, host-wide container monitoring, or a standalone executable.

### Task 3: Build Deterministic Release Artifacts And Inspect The RPM

**Files:**
- Modify: `test_packaging.py`
- Modify: `Makefile`
- Create: `packaging/verify-rpm.sh`
- Modify: `MANIFEST.in`

**Interfaces:**
- Consumes: `dist/cooler_btop-2.0.0.tar.gz`, Fedora RPM tools, and four exact artifact names.
- Produces: Make targets `rpm-binary`, `rpm-check`, `checksums`, and `release`; executable `packaging/verify-rpm.sh RPM_PATH`; deterministic `dist/SHA256SUMS`.

- [x] **Step 1: Add failing Make and RPM-verifier contract tests**

Extend `LinuxPackagingTests` to require the four targets, the verifier in the sdist, direct binary RPM output to `DIST_DIR`, exact four-file checksum input, and these verifier checks:

```python
self.assertIn('rpm -K "$rpm_path"', verifier)
self.assertIn('/usr/bin/cooler-btop', verifier)
self.assertIn('/usr/bin/btop', verifier)
self.assertIn('--provides', verifier)
self.assertIn('--conflicts', verifier)
self.assertIn('--obsoletes', verifier)
self.assertIn('%{FILECAPS}', verifier)
self.assertIn('rpm2cpio', verifier)
self.assertIn('--version', verifier)
```

Add a test that creates four small files in a temporary directory, invokes `make checksums DIST_DIR=<temp>`, validates `sha256sum --check`, and asserts `SHA256SUMS` contains exactly the four sorted artifact names.

- [x] **Step 2: Run focused tests and capture RED**

Run: `python3 -m unittest -v test_packaging.LinuxPackagingTests`

Expected: FAIL because release targets and `packaging/verify-rpm.sh` do not exist.

- [x] **Step 3: Add Make release targets**

Read version from `pyproject.toml` with Python and define exact Fedora 44 names. Add:

```make
VERSION := $(shell $(PYTHON) -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
RPM_RELEASE := 1.fc44
WHEEL_FILE := cooler_btop-$(VERSION)-py3-none-any.whl
SDIST_FILE := cooler_btop-$(VERSION).tar.gz
SRPM_FILE := cooler-btop-$(VERSION)-$(RPM_RELEASE).src.rpm
RPM_FILE := cooler-btop-$(VERSION)-$(RPM_RELEASE).noarch.rpm

rpm-binary: source-dist
	rpmbuild -bb packaging/cooler-btop.spec \
		--define "_sourcedir $(abspath $(DIST_DIR))" \
		--define "_rpmdir $(abspath $(DIST_DIR))" \
		--define "_rpmfilename %%{NAME}-%%{VERSION}-%%{RELEASE}.%%{ARCH}.rpm"

checksums:
	cd "$(DIST_DIR)" && sha256sum "$(WHEEL_FILE)" "$(SDIST_FILE)" \
		"$(SRPM_FILE)" "$(RPM_FILE)" > SHA256SUMS
	cd "$(DIST_DIR)" && sha256sum --check SHA256SUMS

rpm-check:
	packaging/verify-rpm.sh "$(DIST_DIR)/$(RPM_FILE)"
```

Make `release` call the existing tests and builders plus `rpm-binary`, `rpm-check`, and `checksums` sequentially so a failed check stops immediately.

- [x] **Step 4: Implement `packaging/verify-rpm.sh`**

Use `set -eu`, require one RPM path, and perform these checks without installation:

- `rpm -K` succeeds.
- `rpm -qlp` includes exactly `/usr/bin/cooler-btop` under `/usr/bin` and excludes `/usr/bin/btop`.
- `rpm -qp --provides`, `--conflicts`, and `--obsoletes` contain no exact `btop` capability.
- `rpm -qp --scripts` and `--triggers` are empty.
- `%{FILECAPS}` reports `(none)` for every payload entry.
- No payload filename has a native-library/binary suffix or ELF/PE/Mach-O/archive magic except the text console launcher.
- `rpm2cpio | cpio -idm` extracts into a temporary directory.
- The extracted `cooler-btop --version` returns `Cooler btop v2.0.0` with `PYTHONPATH` pointed at the extracted site-packages directory.

Always remove the temporary directory with a trap. Add the script to `MANIFEST.in`.

- [x] **Step 5: Run focused tests and a local release build**

Run: `python3 -m unittest -v test_packaging.LinuxPackagingTests`

Run: `make release`

Expected: tests pass; RPM `%check` passes; `dist/` contains the four package artifacts and `SHA256SUMS`; checksum and RPM verification pass; Cooler btop is not installed.

- [x] **Step 6: Review checkpoint**

Inspect Make recipes and shell quoting for path safety, stale-file inclusion, accidental installation, privilege escalation, deletion, key access, and native compilation. Confirm the checksum manifest names only the four public package artifacts.

### Task 4: Add Fedora CI And Tag-Driven GitHub Releases

**Files:**
- Modify: `test_packaging.py`
- Modify: `.github/workflows/ci.yml`
- Create: `.github/workflows/release.yml`
- Modify: `MANIFEST.in`

**Interfaces:**
- Consumes: `make test`, `make distributions`, `make release`, and `dist/SHA256SUMS`.
- Produces: Python 3.9/current CI, Fedora 44 RPM CI, and a `v*` tag workflow that creates a GitHub release containing exactly five release files.

- [x] **Step 1: Add failing workflow contract tests**

Assert normal CI contains an Ubuntu Python matrix with `3.9` and `3.14`, plus a Fedora job using `container: fedora:44`, RPM/AppStream/desktop dependencies, `make release`, and a final `rpm -q cooler-btop` non-installation check.

Assert `release.yml`:

```python
self.assertIn('tags: [ "v*" ]', release_workflow)
self.assertIn("contents: write", release_workflow)
self.assertIn("make release", release_workflow)
self.assertIn('v$(python3 -c', release_workflow)
self.assertIn("dist/SHA256SUMS", release_workflow)
self.assertIn("gh release create", release_workflow)
```

Also assert the sdist contains both workflow files.

- [x] **Step 2: Run packaging tests and capture RED**

Run: `python3 -m unittest -v test_packaging`

Expected: FAIL because Fedora and release workflows do not exist.

- [x] **Step 3: Expand normal CI**

Keep the Ubuntu package tests and wheel smoke test, using a Python matrix for 3.9 and 3.14. Add a Fedora 44 container job that installs only required tools with DNF, invokes `make release`, verifies `SHA256SUMS`, confirms `rpm -q cooler-btop` reports not installed, and uploads the five artifacts for CI inspection.

- [x] **Step 4: Add the tag release workflow**

Use two jobs:

1. A Fedora 44 container build job checks that `GITHUB_REF_NAME` equals `v` plus the version read from `pyproject.toml`, runs `make release`, and uploads one Actions artifact containing the four packages and `SHA256SUMS`.
2. An Ubuntu publish job downloads that artifact, verifies `SHA256SUMS`, and runs `gh release create "$GITHUB_REF_NAME" ... --verify-tag --generate-notes` with `GH_TOKEN: ${{ github.token }}` and `contents: write` permission.

Do not add package registry publication, PyPI credentials, GPG secrets, or automatic tag creation.

- [x] **Step 5: Validate workflow syntax and contracts**

Run: `python3 -m unittest -v test_packaging`

Run: `python3 -c 'import pathlib, yaml; [yaml.safe_load(path.read_text()) for path in pathlib.Path(".github/workflows").glob("*.yml")]'`

If PyYAML is unavailable locally, record that syntax parsing was not run; do not install it without explicit need. The static packaging contracts and GitHub workflow structure remain required.

- [x] **Step 6: Review checkpoint**

Review workflow permissions, triggers, artifact paths, tag/version matching, command quoting, and absence of untrusted pull-request publication paths or secret use. Confirm ordinary pushes cannot publish a release.

### Task 5: Curate The Operational Task Queue And Complete Acceptance

**Files:**
- Modify: `TASKS.md`
- Create: `docs/generated-task-ideas.md`
- Modify: `MANIFEST.in`
- Verify: all source, package, metadata, workflow, and artifact files changed by Tasks 1-4.

**Interfaces:**
- Consumes: the original generated checklist and all release checks.
- Produces: a concise operational queue plus a preserved generated-ideas archive; final evidence that the repository is ready to publish.

- [x] **Step 1: Preserve and triage generated tasks**

Copy the original unchecked generated list into `docs/generated-task-ideas.md` with a heading explaining that entries are unapproved ideas, may duplicate shipped behavior, and are not release requirements.

Rewrite `TASKS.md` with:

```markdown
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
```

Retain the existing Cycle 63 and 64 records and add a release-readiness record only after all acceptance checks pass. Add the generated archive to the sdist manifest.

- [x] **Step 2: Run source and generated-file checks**

Run: `python3 -m unittest discover -v`

Run: `make release`

Expected: all tests and release checks pass from the final source tree.

- [x] **Step 3: Validate installed-format metadata**

Run:

```bash
desktop-file-validate packaging/io.github.l0ee.cooler_btop.desktop
appstreamcli validate --no-net packaging/io.github.l0ee.cooler_btop.metainfo.xml
rpm -K dist/cooler-btop-2.0.0-1.fc44.noarch.rpm dist/cooler-btop-2.0.0-1.fc44.src.rpm
rpm -q cooler-btop
rpm -qf /usr/bin/btop
```

Expected: metadata and digests pass; `cooler-btop` is not installed; `/usr/bin/btop` remains owned by Nobara's `btop` package.

- [x] **Step 4: Run live hardware and privacy smoke checks**

Collect one complete snapshot from the source package. Require CPU, memory, process, disk, network, sensors, zram where present, Intel frequency-only telemetry, NVIDIA retry recovery where the GPU is awake, and no serialized host MAC/IP values. Run `python3 benchmark.py` and record the measured result without turning it into an unsupported performance promise.

- [x] **Step 5: Inspect final release contents**

Confirm `dist/` has the four exact package artifacts plus `SHA256SUMS`. Run `sha256sum --check dist/SHA256SUMS` from the correct directory, query RPM metadata and dependencies, and inspect wheel/sdist/RPM payloads for missing assets, native binaries, placeholder contacts, stale dates, and unsupported claims.

- [x] **Step 6: Request final code review**

Dispatch a read-only reviewer with the design, this plan, changed files, test output, artifact paths, and operational constraints. Fix every Critical or Important finding test-first, rerun affected checks, then rerun the full suite and `make release`.

- [x] **Step 7: Record readiness accurately**

When and only when every acceptance check passes, add a Cycle 65 Done entry stating the exact checks and artifact scope. Report that the source is ready to initialize/push to GitHub, but do not initialize Git, commit, tag, push, enable repository security settings, or publish the release without an explicit request.
