# Packaging Conversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Subagents are prohibited for this implementation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the flat application into an installable `cooler_btop` Python package with validated wheel and Fedora/Nobara desktop packaging.

**Architecture:** Move every runtime implementation and the web dashboard under one importable package, expose both `python -m cooler_btop` and the `cooler-btop` console script, and keep only a forwarding root launcher. Keep Python metadata in `pyproject.toml`; keep distribution-specific desktop, AppStream, icon, man, and RPM files under `packaging/`.

**Tech Stack:** Python 3.9+, setuptools, unittest, Textual, Rich, psutil, requests, freedesktop desktop/AppStream metadata, RPM `%pyproject_*` macros.

**Spec:** Approved requirements in the user's 2026-09-12 implementation request; no separate design document was requested.

## Global Constraints

- Project name is `cooler-btop`, version `2.0.0`, and platform is Linux.
- Direct dependencies include `textual`, `rich`, `psutil`, and `requests`.
- Runtime modules use package-relative imports and have no duplicate root implementations.
- The RPM owns `/usr/bin/cooler-btop`, never `/usr/bin/btop`, and does not package or enable a daemon service.
- No system-wide installation, `sudo`, network dependency installation, RPM installation, destructive validation, or subagents.

---

### Task 1: Lock In Packaging Contracts

**Files:**
- Create: `test_packaging.py`

**Interfaces:**
- Consumes: repository source tree, `pyproject.toml`, package wheel, and `packaging/` artifacts.
- Produces: unittest contracts for package imports, wheel payload, console entry point, and Linux packaging collision safety.

- [x] **Step 1: Write package and artifact tests before production changes**
- [x] **Step 2: Run `python3 -m unittest -v test_packaging`**
- [x] **Step 3: Confirm failure is caused by the flat package, old metadata, native files, and absent Linux artifacts**

### Task 2: Create the Runtime Package

**Files:**
- Create: `cooler_btop/__init__.py`
- Create: `cooler_btop/__main__.py`
- Move: `main.py` to `cooler_btop/main.py`, then create a forwarding `main.py`
- Move: `data.py`, `fast_telemetry.py`, `ui.py`, `braille.py`, and `server.py` into `cooler_btop/`
- Move: `web/index.html` to `cooler_btop/web/index.html`
- Delete: root `__init__.py`

**Interfaces:**
- Consumes: existing `run_cli()`, `DataCollector`, widgets, telemetry, server, and dashboard implementations.
- Produces: `cooler_btop.main:run_cli`, `python -m cooler_btop`, and package-relative module imports.

- [x] **Step 1: Move runtime files without duplicating implementations**
- [x] **Step 2: Change local imports to `.ui`, `.data`, `.fast_telemetry`, `.braille`, and `.server`**
- [x] **Step 3: Add module and root launchers that raise `SystemExit(run_cli())`**
- [x] **Step 4: Run `python3 -m unittest -v test_packaging.SourceLayoutTests`**

### Task 3: Define Python and Linux Packaging

**Files:**
- Modify: `pyproject.toml`
- Modify: `MANIFEST.in` to include package and RPM source assets
- Delete: `setup.py`, `c_telemetry.c`, and `libc_telemetry.so`
- Create: `packaging/io.github.l0ee.cooler_btop.desktop`
- Create: `packaging/io.github.l0ee.cooler_btop.svg`
- Create: `packaging/io.github.l0ee.cooler_btop.metainfo.xml`
- Create: `packaging/cooler-btop.1`
- Create: `packaging/cooler-btop.spec`

**Interfaces:**
- Consumes: package from Task 2.
- Produces: setuptools wheel metadata, packaged `cooler_btop/web/index.html`, freedesktop integration files, and a Fedora/Nobara RPM recipe.

- [x] **Step 1: Make `pyproject.toml` the sole metadata/configuration source with package-data enabled**
- [x] **Step 2: Remove duplicate metadata and unused native telemetry inputs**
- [x] **Step 3: Add a terminal desktop launcher, distinct local-only SVG, console AppStream metadata, and man page**
- [x] **Step 4: Add an RPM spec using `%pyproject_wheel`, `%pyproject_install`, and `%pyproject_save_files`**
- [x] **Step 5: Run `python3 -m unittest -v test_packaging.LinuxPackagingTests`**

### Task 4: Convert Development Workflows

**Files:**
- Modify: all `test_*.py`, `benchmark.py`, `leak_test.py`, and `stress_test.py`
- Modify: `install.sh`, `update.sh`, `Makefile`, `.github/workflows/ci.yml`, `requirements.txt`, `Dockerfile`, and `README.md`

**Interfaces:**
- Consumes: package interfaces from Task 2 and packaging outputs from Task 3.
- Produces: package-path imports, non-root wheel helper, full unittest target, wheel validation target, clean-venv smoke target, and CI validation from outside the source tree.

- [x] **Step 1: Replace flat imports and patch targets with `cooler_btop.*` paths**
- [x] **Step 2: Replace root/editable/systemd installation behavior with local wheel build instructions**
- [x] **Step 3: Update Makefile targets without auto-installing tools or deleting user data**
- [x] **Step 4: Update CI to current checkout/setup-python majors, full tests, wheel build, isolated install, version smoke, and asset inspection**
- [x] **Step 5: Update supporting docs/container commands to invoke the package**

### Task 5: Verify the Distribution

**Files:**
- Verify: complete repository and generated wheel in a temporary directory

**Interfaces:**
- Consumes: all previous tasks.
- Produces: exact unittest, wheel build, wheel-content, and installed-console evidence.

- [x] **Step 1: Run `python3 -m unittest discover -v`**
- [x] **Step 2: Build with `python3 -m pip wheel --no-deps --no-build-isolation`**
- [x] **Step 3: Inspect wheel filenames, package assets, metadata, and console entry point**
- [x] **Step 4: Create a clean temporary venv, install the wheel from outside source, and run `cooler-btop --version`**
- [x] **Step 5: Re-run `python3 -m unittest discover -v` after any verification fix and report exact results**
