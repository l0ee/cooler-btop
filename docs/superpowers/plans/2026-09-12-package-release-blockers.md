# Package Release Blockers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Subagents are prohibited for this implementation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce self-contained Python and RPM source artifacts whose metadata, offline dashboard, CI checks, and container deployment accurately match the shipped application.

**Architecture:** Keep `pyproject.toml` as the Python metadata source and generate both wheel and sdist with setuptools' PEP 517 backend. Serve one packaged HTML dashboard containing local CSS and vanilla canvas graphs, build the RPM from that local sdist, and keep the container as an unprivileged read-only view of its own namespace.

**Tech Stack:** Python 3.9+, setuptools build backend, unittest, HTML/CSS/vanilla JavaScript canvas, Fedora RPM `%pyproject_*` macros, Docker Compose, GitHub Actions.

**Spec:** Approved requirements in the user's 2026-09-12 package release blocker request; no separate design document is needed.

## Global Constraints

- Build requirement is `setuptools>=61`; license metadata must be accepted by that setuptools generation.
- Textual requirement is exactly `textual>=4.0,<9` in project metadata and `requirements.txt`.
- The dashboard is read-only, has no external URLs or scripts, and renders process data with `textContent`.
- RPM `Source0` is the locally generated `cooler_btop-%{version}.tar.gz`; its root is `cooler_btop-%{version}`.
- The RPM never Provides, Conflicts with, or Obsoletes `btop`, and no native binaries ship in source or Python artifacts.
- Docker binds daemon mode to `0.0.0.0` without privileged mode or host PID mode and documents container-namespace monitoring scope.
- No `sudo`, network access, dependency or RPM installation, system-wide changes, or subagents.

---

### Task 1: Add Release Contract Tests

**Files:**
- Modify: `test_packaging.py`
- Modify: `test_web.py`

**Interfaces:**
- Consumes: source tree, `pyproject.toml`, RPM spec, Makefile, workflow, Docker files, wheel, and sdist.
- Produces: regression checks for every release blocker and a captured RED run before production changes.

- [ ] **Step 1: Extend metadata and artifact checks**

```python
self.assertEqual(metadata["build-system"]["requires"], ["setuptools>=61", "wheel"])
self.assertEqual(project["license"], {"text": "MIT"})
self.assertIn("textual>=4.0,<9", project["dependencies"])
self.assertFalse(native_members)
```

- [ ] **Step 2: Build an sdist through `setuptools.build_meta` and inspect its name, single root directory, tests, packaging assets, and absence of native files**

```python
subprocess.run([
    sys.executable, "-c",
    "import setuptools.build_meta as b; b.build_sdist(r'" + str(output) + "')",
], check=True)
```

- [ ] **Step 3: Check RPM, Makefile, CI, Docker, and obsolete-service contracts**

```python
self.assertIn("Source0:        cooler_btop-%{version}.tar.gz", spec)
self.assertIn("python3 -m unittest discover -v", spec)
self.assertNotRegex(spec, r"(?im)^(Provides|Conflicts|Obsoletes):.*\\bbtop\\b")
self.assertNotIn("privileged:", compose)
self.assertNotIn('pid: "host"', compose)
self.assertFalse((ROOT / "cooler-btop.service").exists())
```

- [ ] **Step 4: Check the web tree for external URLs, external scripts, unresolved local assets, unsafe process HTML, and mutating controls**

```python
self.assertNotRegex(source, r"https?://|(?<!:)//")
self.assertNotIn("new Chart", source)
self.assertIn("nameCell.textContent", source)
self.assertNotRegex(source, r"/api/(kill|terminate)|fetch\\s*\\([^)]*method")
```

- [ ] **Step 5: Run focused tests and preserve the failing assertions**

Run: `python3 -m unittest -v test_packaging test_web`
Expected: FAIL on current metadata, CDN references, absent assets, PyPI Source0, obsolete service, privileged Docker, and missing sdist/full-test contracts.

### Task 2: Fix Python Metadata and Offline Dashboard

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Modify: `cooler_btop/server.py`
- Modify: `cooler_btop/web/index.html`
- Modify: `test_server.py`

**Interfaces:**
- Consumes: `/api/metrics/stream` SSE snapshots.
- Produces: a single `/` dashboard asset with embedded CSS and JavaScript; `MetricsHandler.assets` maps only `/` to `index.html`.

- [ ] **Step 1: Set setuptools, license, and Textual metadata to the approved ranges**
- [ ] **Step 2: Replace Tailwind and Chart.js use with embedded responsive CSS and a small canvas history renderer**
- [ ] **Step 3: Preserve process sorting/filtering and build all process cells using `createElement` plus `textContent`**
- [ ] **Step 4: Remove `/styles.css` and `/app.js` from the server allowlist and update server route tests**
- [ ] **Step 5: Run `python3 -m unittest -v test_web test_server test_packaging.SourceLayoutTests`**

### Task 3: Make the Local Sdist the RPM Source

**Files:**
- Modify: `MANIFEST.in`
- Modify: `packaging/cooler-btop.spec`
- Modify: `Makefile`

**Interfaces:**
- Consumes: setuptools backend `build_sdist("dist")` output.
- Produces: `dist/cooler_btop-2.0.0.tar.gz`, `%autosetup -n cooler_btop-%{version}`, and an optional source RPM in `dist/` when `rpmbuild` exists.

- [ ] **Step 1: Include root `test_*.py`, package tests, Docker/CI release inputs, and required packaging files in the sdist**
- [ ] **Step 2: Point `Source0` to `cooler_btop-%{version}.tar.gz`, request runtime build requirements, and run full unittest discovery plus import/desktop/AppStream checks in `%check`**
- [ ] **Step 3: Add `source-dist`, `distributions`, and guarded `rpm-source` Make targets; call `setuptools.build_meta.build_sdist` directly**
- [ ] **Step 4: Run `python3 -m unittest -v test_packaging.DistributionTests test_packaging.LinuxPackagingTests`**

### Task 4: Align CI, Docker, Stress Test, and Documentation

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `stress_test.py`
- Modify: `README.md`
- Delete: `cooler-btop.service`

**Interfaces:**
- Consumes: full tests and `make distributions`.
- Produces: CI-built wheel/sdist, a non-root container daemon bound to all container interfaces, and an SSE stress check with no process-control request.

- [ ] **Step 1: Build wheel and sdist in CI, inspect packaging through the full test suite, and smoke-test the wheel without disabling dependency installation**
- [ ] **Step 2: Run the container as a non-root user with a read-only filesystem, dropped capabilities, no host PID namespace, no privileged mode, and `--host 0.0.0.0`**
- [ ] **Step 3: Replace the HTTP kill section in `stress_test.py` with checks that concurrent SSE consumers complete and the metrics endpoint remains healthy**
- [ ] **Step 4: Remove the obsolete root service and correct README claims about offline/read-only web mode and container namespace scope**
- [ ] **Step 5: Run `python3 -m unittest -v test_packaging test_web test_server`**

### Task 5: Validate Release Artifacts

**Files:**
- Verify: complete source tree and temporary artifact directories.

**Interfaces:**
- Consumes: all changes above.
- Produces: exact full-test, wheel, sdist, metadata, archive-layout, and RPM parse/source-build evidence.

- [ ] **Step 1: Run `python3 -m unittest discover -v`**
- [ ] **Step 2: Run `make distributions` with an isolated temporary output location where practical**
- [ ] **Step 3: Inspect wheel metadata and contents plus sdist root, tests, packaging files, and native-file absence**
- [ ] **Step 4: Run `rpmspec -P packaging/cooler-btop.spec` and `make rpm-source` if local RPM tooling and macros permit it**
- [ ] **Step 5: Run `desktop-file-validate` and `appstreamcli validate --no-net` when available**
- [ ] **Step 6: Review the final diff and report RED evidence, changed/deleted files, command results, and any unavailable RPM validation**
