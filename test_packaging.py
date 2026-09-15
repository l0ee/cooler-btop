import configparser
import copy
import email.parser
import email.policy
import hashlib
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

import yaml

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = pathlib.Path(__file__).resolve().parent
PACKAGE = ROOT / "cooler_btop"
APP_ID = "io.github.l0ee.cooler_btop"
NATIVE_SUFFIXES = {".a", ".dll", ".dylib", ".exe", ".lib", ".node", ".o", ".pyd", ".so"}
NATIVE_MAGICS = (
    b"\x7fELF",
    b"MZ",
    b"\xca\xfe\xba\xbe",
    b"\xca\xfe\xba\xbf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xbe\xba\xfe\xca",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xbf\xba\xfe\xca",
    b"!<arch>\n",
)
RELEASE_ARTIFACTS = [
    "cooler-btop-2.0.0-1.fc44.noarch.rpm",
    "cooler-btop-2.0.0-1.fc44.src.rpm",
    "cooler_btop-2.0.0-py3-none-any.whl",
    "cooler_btop-2.0.0.tar.gz",
]
RELEASE_PATHS = [f"dist/{name}" for name in RELEASE_ARTIFACTS] + ["dist/SHA256SUMS"]
RPM_ABSENCE_CHECK = '''set +e
query_output=$(LC_ALL=C rpm -q cooler-btop 2>&1)
query_status=$?
set -e
test "$query_status" -eq 1
test "$query_output" = "package cooler-btop is not installed"'''


def has_native_magic(header):
    return any(header.startswith(magic) for magic in NATIVE_MAGICS)


def find_native_members(names):
    return {
        name for name in names
        if pathlib.PurePosixPath(name).suffix.lower() in NATIVE_SUFFIXES
        or ".so." in pathlib.PurePosixPath(name).name.lower()
    }


def write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def load_workflow(name):
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def named_step(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


def assert_publish_artifact_flow(testcase, workflow):
    publish = workflow["jobs"]["publish"]
    downloads = [
        (index, step)
        for index, step in enumerate(publish["steps"])
        if step.get("uses", "").partition("@")[0] == "actions/download-artifact"
    ]
    testcase.assertEqual(len(downloads), 1)
    download_index, download = downloads[0]
    testcase.assertEqual(download.get("with"), {
        "name": "release-files",
        "path": "dist",
    })

    verification_index = next(
        index
        for index, step in enumerate(publish["steps"])
        if step.get("name") == "Verify downloaded artifacts"
    )
    publication_index = next(
        index
        for index, step in enumerate(publish["steps"])
        if shlex.split(step.get("run", ""))[:3] == ["gh", "release", "create"]
    )
    testcase.assertLess(download_index, verification_index)
    testcase.assertLess(verification_index, publication_index)
    verification_lines = [
        line.strip()
        for line in publish["steps"][verification_index]["run"].splitlines()
        if line.strip()
    ]
    testcase.assertEqual(verification_lines, [
        'test "$(find dist -maxdepth 1 -type f | wc -l)" -eq 5',
        "cd dist",
        "sha256sum --check SHA256SUMS",
    ])


def shell_command_invocations(script):
    invocations = []
    separators = {";", "&&", "||", "|", "&", "(", ")"}
    assignment = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

    for line in script.replace("\\\n", " ").splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        segment = []
        for token in list(lexer) + [";"]:
            if token not in separators:
                segment.append(token)
                continue
            while segment and assignment.match(segment[0]):
                segment.pop(0)
            while segment and pathlib.PurePosixPath(segment[0]).name == "env":
                segment.pop(0)
                while segment and (segment[0].startswith("-") or assignment.match(segment[0])):
                    segment.pop(0)
            if segment:
                invocations.append(segment)
            segment = []
    return invocations


def assert_no_prohibited_run_commands(testcase, workflows):
    for workflow_name, workflow in workflows.items():
        for job_name, job in workflow["jobs"].items():
            for step in job["steps"]:
                location = f"{workflow_name}:{job_name}:{step.get('name', '<unnamed>')}"
                for invocation in shell_command_invocations(step.get("run", "")):
                    executable = pathlib.PurePosixPath(invocation[0]).name.casefold()
                    arguments = invocation[1:]
                    normalized_arguments = [argument.casefold() for argument in arguments]

                    if any("/usr/bin/btop" in argument for argument in invocation):
                        testcase.fail(f"/usr/bin/btop path use in {location}")
                    if executable in {"gcc", "cc", "clang", "cmake", "ninja", "cargo", "rustc"}:
                        testcase.fail(f"native project compiler in {location}")
                    if executable == "dnf" and "install" in normalized_arguments:
                        if any(
                            "dist/" in argument or argument.endswith(".rpm")
                            for argument in normalized_arguments
                        ):
                            testcase.fail(f"generated RPM installation via dnf in {location}")
                    if executable == "rpm" and any(
                        argument in {"--install", "--upgrade"}
                        or argument.startswith("-i")
                        or argument.startswith("-U")
                        for argument in arguments
                    ):
                        testcase.fail(f"generated RPM installation via rpm in {location}")
                    if executable == "gh" and "api" in normalized_arguments:
                        testcase.fail(f"GitHub API publication in {location}")
                    if executable == "gh" and all(
                        command in normalized_arguments for command in ("release", "upload")
                    ):
                        testcase.fail(f"secondary GitHub release upload in {location}")
                    if executable == "git" and any(
                        command in normalized_arguments for command in ("tag", "push")
                    ):
                        testcase.fail(f"git tag or push in {location}")
                    if executable in {"twine", "pypi"}:
                        testcase.fail(f"package registry publication in {location}")
                    if executable in {"gpg", "cosign"}:
                        testcase.fail(f"artifact signing in {location}")


def make_fake_rpm_tools(temp_path):
    bin_dir = temp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "rpm", """#!/bin/bash
set -eu
query=
case "$1" in
    -K)
        if [ -n "${RELEASE_LOG:-}" ]; then
            printf 'rpm-check:%s\n' "$DIST_DIR" >> "$RELEASE_LOG"
        fi
        exit 0
        ;;
    -qlp)
        query=qlp
        printf '%s\n' '/usr/bin/cooler-btop'
        ;;
    -qp)
        case "$2" in
            --provides)
                query=provides
                printf '%s\n' 'cooler-btop = 2.0.0-1.fc44'
                ;;
            --conflicts) query=conflicts ;;
            --obsoletes) query=obsoletes ;;
            --scripts) query=scripts ;;
            --triggers) query=triggers ;;
            --qf)
                query=filecaps
                if [ "${EMPTY_FILECAPS:-0}" != 1 ]; then
                    printf '/usr/bin/cooler-btop\t(none)\n'
                fi
                ;;
            *) exit 64 ;;
        esac
        ;;
    *) exit 64 ;;
esac
if [ "${RPM_FAIL_QUERY:-}" = "$query" ]; then
    exit 7
fi
""")
    write_executable(bin_dir / "rpm2cpio", """#!/bin/bash
set -eu
if [ "${RPM2CPIO_FAIL:-0}" = 1 ]; then
    exit 9
fi
printf 'fake payload\n'
""")
    write_executable(bin_dir / "cpio", f"""#!{sys.executable}
import os
import pathlib
import sys

sys.stdin.buffer.read()
root = pathlib.Path.cwd()
launcher = root / "usr/bin/cooler-btop"
launcher.parent.mkdir(parents=True)
launcher.write_text("#!/bin/sh\\nprintf 'Cooler btop v2.0.0\\n'\\n", encoding="utf-8")
launcher.chmod(0o755)
(root / "usr/lib/python3.14/site-packages/cooler_btop").mkdir(parents=True)
magic = os.environ.get("PAYLOAD_MAGIC_HEX")
if magic:
    (root / "payload.bin").write_bytes(bytes.fromhex(magic) + b"fixture")
""")
    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    return bin_dir, environment


def make_fake_release_tools(temp_path):
    bin_dir, environment = make_fake_rpm_tools(temp_path)
    log_path = temp_path / "release.log"
    python_path = bin_dir / "fake-python"
    write_executable(python_path, """#!/bin/bash
set -eu
log() {
    printf '%s:%s\n' "$1" "$DIST_DIR" >> "$RELEASE_LOG"
}
case " $* " in
    *"tomllib.load"*)
        printf '2.0.0\n'
        ;;
    *" -m unittest "*)
        sleep 0.1
        log test
        ;;
    *" -m pip wheel "*)
        log wheel
        printf 'fresh wheel\n' > "$DIST_DIR/cooler_btop-2.0.0-py3-none-any.whl"
        ;;
    *"build_sdist"*)
        log sdist
        printf 'fresh sdist\n' > "$DIST_DIR/cooler_btop-2.0.0.tar.gz"
        ;;
    *" -m venv "*)
        log wheel-smoke
        venv=${!#}
        mkdir -p "$venv/bin"
        ln -s "$0" "$venv/bin/python"
        ;;
    *" -m pip install "*)
        bin=$(dirname "$0")
        cat > "$bin/cooler-btop" <<'EOF'
#!/bin/sh
printf 'Cooler btop v2.0.0\n'
EOF
        chmod +x "$bin/cooler-btop"
        ;;
    *)
        exit 64
        ;;
esac
""")
    write_executable(bin_dir / "rpmbuild", """#!/bin/bash
set -eu
case "$1" in
    -bs)
        printf 'srpm:%s\n' "$DIST_DIR" >> "$RELEASE_LOG"
        printf 'fresh srpm\n' > "$DIST_DIR/cooler-btop-2.0.0-1.fc44.src.rpm"
        ;;
    -bb)
        printf 'rpm:%s\n' "$DIST_DIR" >> "$RELEASE_LOG"
        printf 'fresh rpm\n' > "$DIST_DIR/cooler-btop-2.0.0-1.fc44.noarch.rpm"
        ;;
    *) exit 64 ;;
esac
""")
    environment["RELEASE_LOG"] = str(log_path)
    return environment, python_path, log_path


class SourceLayoutTests(unittest.TestCase):
    def test_native_member_detection_catches_packaged_binary_variants(self):
        names = {
            "cooler_btop/native.pyd",
            "cooler_btop/libtelemetry.so.1",
            "cooler_btop/telemetry.node",
            "cooler_btop/data.json",
        }

        self.assertEqual(find_native_members(names), names - {"cooler_btop/data.json"})

    def test_runtime_is_importable_from_the_real_package(self):
        import cooler_btop
        from cooler_btop import braille, data, fast_telemetry, main, server, ui

        self.assertEqual(cooler_btop.__version__, "2.0.0")
        self.assertTrue(callable(main.run_cli))
        self.assertTrue(callable(braille.make_braille_graph))
        self.assertTrue(callable(data.prepare_processes))
        self.assertTrue(callable(fast_telemetry.select_root_block_devices))
        self.assertTrue(callable(server.run_server))
        self.assertTrue(hasattr(ui, "CPUWidget"))

    def test_package_contains_runtime_modules_and_web_asset(self):
        expected = {
            "__init__.py",
            "__main__.py",
            "main.py",
            "data.py",
            "fast_telemetry.py",
            "ui.py",
            "braille.py",
            "server.py",
            "web/index.html",
        }
        actual = {
            path.relative_to(PACKAGE).as_posix()
            for path in PACKAGE.rglob("*")
            if path.is_file()
        }
        self.assertTrue(expected <= actual, expected - actual)

    def test_pyproject_is_the_only_metadata_source(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = metadata["project"]
        dependency_names = {
            requirement.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0].strip().lower()
            for requirement in project["dependencies"]
        }

        self.assertEqual(project["name"], "cooler-btop")
        self.assertEqual(project["version"], "2.0.0")
        self.assertEqual(project["scripts"]["cooler-btop"], "cooler_btop.main:run_cli")
        self.assertEqual(dependency_names, {"textual", "rich", "psutil", "requests"})
        self.assertIn("Operating System :: POSIX :: Linux", project["classifiers"])
        self.assertFalse((ROOT / "setup.py").exists())

    def test_project_metadata_has_the_public_release_identity(self):
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]

        self.assertEqual(project["version"], "2.0.0")
        self.assertEqual(project["authors"], [{"name": "l0ee"}])
        self.assertEqual(project["urls"], {
            "Homepage": "https://github.com/l0ee/cooler-btop",
            "Issues": "https://github.com/l0ee/cooler-btop/issues",
            "Security": "https://github.com/l0ee/cooler-btop/security/advisories/new",
        })

    def test_public_docs_describe_the_supported_release(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        man_page = (ROOT / "packaging" / "cooler-btop.1").read_text(encoding="utf-8")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

        for release_detail in (
            "Fedora 44",
            "Nobara",
            "CLI-only",
            "unsigned",
        ):
            with self.subTest(release_detail=release_detail):
                self.assertIn(release_detail, readme)
        self.assertIn("security/advisories/new", security)
        self.assertIn("2.0.x", security)
        self.assertIn("Fedora 44", security)
        self.assertIn("Nobara", security)
        self.assertIn("## [2.0.0] - 2026-09-13", changelog)
        self.assertIn("psutil fallbacks", changelog)
        self.assertNotIn("build-exe", makefile + readme)
        self.assertNotIn("sudo", makefile)
        self.assertNotIn("entirely stripped out `psutil`", changelog.casefold())
        self.assertNotIn("netlink", changelog.casefold())
        for public_description in (readme, man_page):
            self.assertIn("IPv4 TCP listening-port", public_description)

    def test_readme_has_a_complete_binary_rpm_installation_workflow(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section_match = re.search(
            r"(?ms)^## Fedora/Nobara 44 Desktop Installation\n(.*?)(?=^## )",
            readme,
        )
        self.assertIsNotNone(section_match)
        shell_blocks = re.findall(r"```bash\n(.*?)\n```", section_match.group(1), re.DOTALL)
        rpm_name = "cooler-btop-2.0.0-1.fc44.noarch.rpm"
        self.assertEqual(shell_blocks[1:], ["sudo dnf remove cooler-btop"])
        self.assertNotRegex(readme, r"(?m)^dnf (?:install|remove)\b")

        cases = (
            ("rpm download fails", rpm_name, "valid", False),
            ("manifest download fails", "SHA256SUMS", "valid", False),
            ("checksum mismatch", "", "mismatch", False),
            ("missing checksum entry", "", "missing", False),
            ("malformed checksum entry", "", "malformed", False),
            ("complete success", "", "valid", True),
        )
        for label, failed_download, manifest, should_install in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temp_dir:
                temp_path = pathlib.Path(temp_dir)
                bin_dir = temp_path / "bin"
                bin_dir.mkdir()
                write_executable(bin_dir / "curl", f"#!{sys.executable}\n" + '''
import os, pathlib, sys
prefix = "https://github.com/l0ee/cooler-btop/releases/download/v2.0.0/"
assert sys.argv[1:4] == ["--fail", "--location", "--remote-name"]
assert len(sys.argv) == 5
assert sys.argv[4] in [prefix + "cooler-btop-2.0.0-1.fc44.noarch.rpm", prefix + "SHA256SUMS"]
name = sys.argv[4].removeprefix(prefix)
with pathlib.Path("downloads").open("a") as log:
    log.write(name + "\\n")
sys.exit(22 if name == os.environ["FAILED_DOWNLOAD"] else 0)
''')
                write_executable(bin_dir / "sudo", f"#!{sys.executable}\n" + '''
import pathlib, sys
pathlib.Path("install-reached").write_text(" ".join(sys.argv[1:]))
assert sys.argv[1:] == ["dnf", "install", "./cooler-btop-2.0.0-1.fc44.noarch.rpm"]
''')
                # Valid stale files must not bypass either failed download.
                rpm_path = temp_path / rpm_name
                rpm_path.write_bytes(b"binary rpm fixture\n")
                digest = hashlib.sha256(rpm_path.read_bytes()).hexdigest()
                if manifest == "mismatch":
                    digest = "0" * 64
                elif manifest == "malformed":
                    digest = "invalid"
                entry = "" if manifest == "missing" else f"{digest}  {rpm_name}\n"
                (temp_path / "SHA256SUMS").write_text(
                    entry
                    + f"{'0' * 64}  cooler-btop-2.0.0-1.fc44.src.rpm\n"
                    + f"{'0' * 64}  cooler_btop-2.0.0-py3-none-any.whl\n"
                    + f"{'0' * 64}  cooler_btop-2.0.0.tar.gz\n",
                    encoding="ascii",
                )
                environment = os.environ.copy()
                environment.update(PATH=f"{bin_dir}{os.pathsep}{environment['PATH']}",
                                   FAILED_DOWNLOAD=failed_download)
                result = subprocess.run(
                    ["bash", "--noprofile", "--norc", "-c", shell_blocks[0]],
                    cwd=temp_path,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=10,
                )

                self.assertEqual((temp_path / "install-reached").exists(), should_install,
                                 result.stdout)
                if should_install:
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(result.stdout, f"{rpm_name}: OK\n")
                    self.assertEqual((temp_path / "downloads").read_text().splitlines(),
                                     [rpm_name, "SHA256SUMS"])
                    self.assertEqual((temp_path / "install-reached").read_text(),
                                     f"dnf install ./{rpm_name}")
                else:
                    self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_public_docs_do_not_contradict_release_support_or_signing(self):
        readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
        security = " ".join((ROOT / "SECURITY.md").read_text(encoding="utf-8").split())

        self.assertIn(
            "The supported desktop package targets Fedora 44 and Nobara systems "
            "based on Fedora 44.",
            readme,
        )
        self.assertIn("The first RPM is checksummed but unsigned.", readme)
        self.assertIn(
            "Version 2.0.x on Fedora 44 and Nobara systems based on Fedora 44 is "
            "currently supported.",
            security,
        )
        for document in (readme, security):
            self.assertNotRegex(document, r"(?i)\bsupport(?:ed|s)? (?:all|any) linux\b")
            self.assertNotRegex(document, r"(?i)\bsigned\b")

    def test_build_metadata_supports_fedora_setuptools_and_textual(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = metadata["project"]
        requirements = [
            line.strip()
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        self.assertEqual(metadata["build-system"]["requires"], ["setuptools>=61", "wheel"])
        self.assertEqual(project["license"], {"text": "MIT"})
        self.assertNotIn("license-files", project)
        self.assertEqual(project["dependencies"], requirements)
        self.assertIn("textual>=4.0,<9", requirements)

    def test_source_tree_has_no_native_binaries_or_build_inputs(self):
        native_files = []
        generated_dirs = {".git", "build", "dist", "__pycache__"}
        for path in ROOT.rglob("*"):
            if not path.is_file() or generated_dirs.intersection(path.relative_to(ROOT).parts):
                continue
            if find_native_members({path.name}) or has_native_magic(path.read_bytes()[:8]):
                native_files.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(native_files, [])

    def test_release_docs_do_not_advertise_removed_unsafe_or_native_features(self):
        published_docs = "\n".join(
            (ROOT / filename).read_text(encoding="utf-8")
            for filename in ("README.md", "SECURITY.md", "CHANGELOG.md")
        ).lower()

        for obsolete_claim in (
            "/api/kill",
            "remote killer api",
            "libc_telemetry.so",
            "tailwind/chart.js",
            "requires elevated privileges",
        ):
            with self.subTest(obsolete_claim=obsolete_claim):
                self.assertNotIn(obsolete_claim, published_docs)


class DistributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp_dir.cleanup)
        build_root = pathlib.Path(cls.temp_dir.name) / "source"
        shutil.copytree(
            ROOT,
            build_root,
            ignore=shutil.ignore_patterns("build", "dist", "*.egg-info", "__pycache__", "*.pyc"),
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                cls.temp_dir.name,
                str(build_root),
            ],
            cwd=cls.temp_dir.name,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if result.returncode:
            raise AssertionError(f"wheel build failed:\n{result.stdout}")
        wheels = list(pathlib.Path(cls.temp_dir.name).glob("cooler_btop-2.0.0-*.whl"))
        if len(wheels) != 1:
            raise AssertionError(f"expected one cooler-btop wheel, found {wheels}\n{result.stdout}")
        cls.wheel = wheels[0]
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import setuptools.build_meta as backend, sys; backend.build_sdist(sys.argv[1])",
                cls.temp_dir.name,
            ],
            cwd=build_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if result.returncode:
            raise AssertionError(f"sdist build failed:\n{result.stdout}")
        sdists = list(pathlib.Path(cls.temp_dir.name).glob("cooler_btop-2.0.0.tar.gz"))
        if len(sdists) != 1:
            raise AssertionError(f"expected one cooler-btop sdist, found {sdists}\n{result.stdout}")
        cls.sdist = sdists[0]

    def test_wheel_contains_runtime_web_asset_and_console_entry_point(self):
        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
            expected = {
                "cooler_btop/__init__.py",
                "cooler_btop/__main__.py",
                "cooler_btop/main.py",
                "cooler_btop/data.py",
                "cooler_btop/fast_telemetry.py",
                "cooler_btop/ui.py",
                "cooler_btop/braille.py",
                "cooler_btop/server.py",
                "cooler_btop/web/index.html",
            }
            self.assertTrue(expected <= names, expected - names)
            self.assertEqual(find_native_members(names), set())
            self.assertEqual({
                name for name in names
                if not name.endswith("/") and has_native_magic(archive.read(name)[:8])
            }, set())
            entry_points = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
            entry_point_text = archive.read(entry_points).decode("utf-8")
        self.assertIn("cooler-btop = cooler_btop.main:run_cli", entry_point_text)

    def test_wheel_metadata_has_compatible_license_and_dependency_range(self):
        with zipfile.ZipFile(self.wheel) as archive:
            metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
            metadata = email.parser.BytesParser(policy=email.policy.default).parsebytes(
                archive.read(metadata_name)
            )

        self.assertEqual(metadata["License"], "MIT")
        self.assertIn("textual<9,>=4.0", metadata.get_all("Requires-Dist"))

    def test_sdist_matches_rpm_source_layout_and_contains_full_tests(self):
        spec = (ROOT / "packaging" / "cooler-btop.spec").read_text(encoding="utf-8")
        source = re.search(r"(?m)^Source0:\s*(\S+)$", spec).group(1)
        version = re.search(r"(?m)^Version:\s*(\S+)$", spec).group(1)
        expected_source = source.replace("%{version}", version)

        self.assertEqual(self.sdist.name, expected_source)
        with tarfile.open(self.sdist, "r:gz") as archive:
            names = {member.name for member in archive.getmembers() if member.isfile()}
        setup_dir = re.search(r"(?m)^%autosetup\s+-n\s+(\S+)$", spec).group(1)
        expected_root = setup_dir.replace("%{version}", version)
        roots = {name.split("/", 1)[0] for name in names}
        self.assertEqual(roots, {expected_root})
        expected = {
            "main.py",
            "install.sh",
            "benchmark.py",
            "test_collector.py",
            "test_main.py",
            "test_packaging.py",
            "test_server.py",
            "test_telemetry_regressions.py",
            "test_ui.py",
            "test_web.py",
            "packaging/cooler-btop.spec",
            "packaging/io.github.l0ee.cooler_btop.desktop",
            "packaging/io.github.l0ee.cooler_btop.metainfo.xml",
            "packaging/io.github.l0ee.cooler_btop.svg",
            "packaging/cooler-btop.1",
            "packaging/verify-rpm.sh",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
            ".dockerignore",
            "Dockerfile",
            "docker-compose.yml",
            "Makefile",
            "requirements.txt",
            "SECURITY.md",
        }
        relative_names = {name.removeprefix(f"{expected_root}/") for name in names}
        self.assertTrue(expected <= relative_names, expected - relative_names)
        self.assertEqual(find_native_members(relative_names), set())
        with tarfile.open(self.sdist, "r:gz") as archive:
            self.assertEqual({
                member.name for member in archive.getmembers()
                if member.isfile()
                and has_native_magic(archive.extractfile(member).read(8))
            }, set())


class LinuxPackagingTests(unittest.TestCase):
    def test_icon_is_self_contained_svg_without_embedded_user_data(self):
        icon = ROOT / "packaging" / f"{APP_ID}.svg"
        root = ET.parse(icon).getroot()
        elements = {element.tag.rsplit("}", 1)[-1] for element in root.iter()}
        attributes = {
            name.rsplit("}", 1)[-1]
            for element in root.iter()
            for name in element.attrib
        }

        self.assertEqual(root.tag.rsplit("}", 1)[-1], "svg")
        self.assertIn("title", elements)
        self.assertIn("desc", elements)
        self.assertNotIn("script", elements)
        self.assertNotIn("image", elements)
        self.assertNotIn("href", attributes)

    def test_desktop_launcher_runs_only_cooler_btop_in_a_terminal(self):
        desktop = configparser.ConfigParser(interpolation=None)
        desktop.optionxform = str
        with (ROOT / "packaging" / f"{APP_ID}.desktop").open(encoding="utf-8") as stream:
            desktop.read_file(stream)
        entry = desktop["Desktop Entry"]

        self.assertEqual(entry["Type"], "Application")
        self.assertEqual(entry["Exec"], "cooler-btop --desktop")
        self.assertEqual(entry["TryExec"], "cooler-btop")
        self.assertEqual(entry["Terminal"].lower(), "true")
        self.assertEqual(entry["Icon"], APP_ID)

    def test_appstream_metadata_describes_a_console_application(self):
        component = ET.parse(ROOT / "packaging" / f"{APP_ID}.metainfo.xml").getroot()

        self.assertEqual(component.attrib["type"], "console-application")
        self.assertEqual(component.findtext("id"), APP_ID)
        self.assertEqual(component.find("launchable").attrib["type"], "desktop-id")
        self.assertEqual(component.findtext("launchable"), f"{APP_ID}.desktop")
        self.assertIsNotNone(component.find("metadata_license"))
        self.assertIsNotNone(component.find("project_license"))

    def test_linux_release_metadata_is_synchronized(self):
        component = ET.parse(ROOT / "packaging" / f"{APP_ID}.metainfo.xml").getroot()
        release = component.find("releases/release")
        urls = {
            element.attrib["type"]: element.text
            for element in component.findall("url")
        }
        spec = (ROOT / "packaging" / "cooler-btop.spec").read_text(encoding="utf-8")
        man_page = (ROOT / "packaging" / "cooler-btop.1").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertEqual(release.attrib, {"version": "2.0.0", "date": "2026-09-13"})
        self.assertEqual(urls, {
            "homepage": "https://github.com/l0ee/cooler-btop",
            "bugtracker": "https://github.com/l0ee/cooler-btop/issues",
            "vcs-browser": "https://github.com/l0ee/cooler-btop",
        })
        self.assertIn(
            "* Sun Sep 13 2026 l0ee <l0ee@users.noreply.github.com> - 2.0.0-1",
            spec,
        )
        self.assertIn("- Initial Fedora 44 and Nobara desktop package", spec)
        self.assertTrue(
            man_page.startswith(
                '.TH COOLER-BTOP 1 "2026-09-13" "cooler-btop 2.0.0" "User Commands"\n'
            )
        )
        self.assertIn("\\-\\-desktop", man_page)
        self.assertIn("application-menu error handling", man_page)
        self.assertIn("include .github/workflows/*.yml", manifest)
        self.assertIn("include packaging/verify-rpm.sh", manifest)

    def test_rpm_spec_uses_local_sdist_and_runs_full_validation(self):
        spec = (ROOT / "packaging" / "cooler-btop.spec").read_text(encoding="utf-8")

        for macro in ("%pyproject_wheel", "%pyproject_install", "%pyproject_save_files"):
            self.assertIn(macro, spec)
        self.assertIn("%pyproject_buildrequires -r", spec)
        self.assertIn("BuildRequires:  appstream\n", spec)
        self.assertIn("BuildRequires:  python3-pip\n", spec)
        self.assertIn("BuildRequires:  python3-pyyaml\n", spec)
        self.assertIn("appstreamcli validate --no-net", spec)
        self.assertIn("Source0:        cooler_btop-%{version}.tar.gz", spec)
        self.assertIn("%autosetup -n cooler_btop-%{version}", spec)
        self.assertIn("python3 -m unittest discover -v", spec)
        self.assertIn("%{_bindir}/cooler-btop", spec)
        self.assertNotIn("/usr/bin/btop", spec)
        self.assertNotIn("cooler-btop.service", spec)
        self.assertNotIn("systemctl", spec)
        alias_lines = re.findall(
            r"(?im)^(?:Provides|Conflicts|Obsoletes):\s*(.*)$",
            spec,
        )
        self.assertFalse(
            any(re.search(r"(?:^|[\s,])btop(?:\b|\()", line, re.IGNORECASE)
                for line in alias_lines),
            alias_lines,
        )
        for artifact in (
            f"{APP_ID}.desktop",
            f"{APP_ID}.svg",
            f"{APP_ID}.metainfo.xml",
            "cooler-btop.1",
        ):
            self.assertIn(artifact, spec)

    def test_makefile_builds_both_distributions_and_local_source_rpm(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

        self.assertRegex(makefile, r"(?m)^source-dist:")
        self.assertIn("setuptools.build_meta", makefile)
        self.assertRegex(makefile, r"(?m)^distributions:.*wheel.*source-dist")
        self.assertRegex(makefile, r"(?m)^rpm-source:.*source-dist")
        self.assertIn("rpmbuild -bs", makefile)

    def test_makefile_defines_the_complete_release_pipeline(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

        for target in ("rpm-binary", "rpm-check", "checksums", "release"):
            with self.subTest(target=target):
                self.assertRegex(makefile, rf"(?m)^{target}:")
        self.assertIn('tomllib.load(open("pyproject.toml", "rb"))', makefile)
        self.assertIn("RPM_RELEASE := 1.fc44", makefile)
        self.assertIn("rpmbuild -bb packaging/cooler-btop.spec", makefile)
        self.assertIn('"_sourcedir $$dist_dir"', makefile)
        self.assertIn('"_rpmdir $$dist_dir"', makefile)
        self.assertIn(
            '"_rpmfilename %%{NAME}-%%{VERSION}-%%{RELEASE}.%%{ARCH}.rpm"',
            makefile,
        )
        self.assertRegex(makefile, r"(?m)^release:$")
        self.assertIn("$(MAKE) --no-print-directory -j1", makefile)
        self.assertIn("export DIST_DIR", makefile)
        self.assertNotIn("$(DIST_DIR)", makefile)
        self.assertNotIn("$(abspath $(DIST_DIR))", makefile)

    def test_missing_rpmbuild_diagnostic_treats_dist_dir_as_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            environment, python_path, _ = make_fake_release_tools(temp_path)
            bin_path = temp_path / "bin"
            (bin_path / "rpmbuild").unlink()
            os.symlink(shutil.which("mkdir"), bin_path / "mkdir")
            os.symlink(shutil.which("touch"), bin_path / "touch")
            environment["PATH"] = str(bin_path)
            marker_path = temp_path / "injected"
            dist_path = temp_path / "output'; touch injected; printf 'x"

            result = subprocess.run(
                [
                    shutil.which("make"),
                    "-f",
                    str(ROOT / "Makefile"),
                    "rpm-source",
                    f"DIST_DIR={dist_path}",
                    f"PYTHON={python_path}",
                ],
                cwd=temp_path,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            injected = marker_path.exists()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(injected, result.stdout)
        self.assertIn(
            f"source distribution is available in {dist_path}.",
            result.stdout,
        )

    def test_rpm_verifier_has_non_installing_payload_checks(self):
        verifier_path = ROOT / "packaging" / "verify-rpm.sh"

        self.assertTrue(verifier_path.is_file())
        self.assertTrue(os.access(verifier_path, os.X_OK))
        verifier = verifier_path.read_text(encoding="utf-8")
        self.assertTrue(verifier.startswith("#!/bin/bash\nset -euo pipefail\n"))
        self.assertIn('rpm -K "$rpm_path"', verifier)
        self.assertIn("/usr/bin/cooler-btop", verifier)
        self.assertIn("/usr/bin/btop", verifier)
        self.assertIn("--provides", verifier)
        self.assertIn("--conflicts", verifier)
        self.assertIn("--obsoletes", verifier)
        self.assertIn("%{FILECAPS}", verifier)
        self.assertIn("rpm2cpio", verifier)
        self.assertIn("--version", verifier)
        self.assertIn('realpath -- "$1"', verifier)
        self.assertIn('b"\\xca\\xfe\\xba\\xbf"', verifier)
        self.assertIn('b"\\xbf\\xba\\xfe\\xca"', verifier)
        self.assertIn("stream.read(8)", verifier)
        self.assertNotIn("path.read_bytes()", verifier)
        self.assertNotRegex(verifier, r"\brpm\s+(?:-i|--install)\b")
        self.assertNotRegex(verifier, r"\bdnf\s+install\b")

    def test_rpm_verifier_fails_when_any_metadata_query_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            _, base_environment = make_fake_rpm_tools(temp_path)
            rpm_path = temp_path / "fixture.rpm"
            rpm_path.write_bytes(b"fixture\n")

            for query in (
                "qlp",
                "provides",
                "conflicts",
                "obsoletes",
                "scripts",
                "triggers",
                "filecaps",
            ):
                with self.subTest(query=query):
                    environment = base_environment.copy()
                    environment["RPM_FAIL_QUERY"] = query
                    result = subprocess.run(
                        [str(ROOT / "packaging" / "verify-rpm.sh"), str(rpm_path)],
                        cwd=temp_path,
                        env=environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_rpm_verifier_requires_filecaps_and_propagates_rpm2cpio_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            _, base_environment = make_fake_rpm_tools(temp_path)
            rpm_path = temp_path / "fixture.rpm"
            rpm_path.write_bytes(b"fixture\n")

            for variable in ("EMPTY_FILECAPS", "RPM2CPIO_FAIL"):
                with self.subTest(variable=variable):
                    environment = base_environment.copy()
                    environment[variable] = "1"
                    result = subprocess.run(
                        [str(ROOT / "packaging" / "verify-rpm.sh"), str(rpm_path)],
                        cwd=temp_path,
                        env=environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_rpm_verifier_rejects_fat64_macho_magic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            _, base_environment = make_fake_rpm_tools(temp_path)
            rpm_path = temp_path / "fixture.rpm"
            rpm_path.write_bytes(b"fixture\n")

            for magic in ("cafebabf", "bfbafeca"):
                with self.subTest(magic=magic):
                    environment = base_environment.copy()
                    environment["PAYLOAD_MAGIC_HEX"] = magic
                    result = subprocess.run(
                        [str(ROOT / "packaging" / "verify-rpm.sh"), str(rpm_path)],
                        cwd=temp_path,
                        env=environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_rpm_verifier_rejects_swapped_fat32_macho_magic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            _, environment = make_fake_rpm_tools(temp_path)
            environment["PAYLOAD_MAGIC_HEX"] = "bebafeca"
            rpm_path = temp_path / "fixture.rpm"
            rpm_path.write_bytes(b"fixture\n")

            result = subprocess.run(
                [str(ROOT / "packaging" / "verify-rpm.sh"), str(rpm_path)],
                cwd=temp_path,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_rpm_verifier_accepts_a_leading_dash_relative_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            _, environment = make_fake_rpm_tools(temp_path)
            (temp_path / "-fixture.rpm").write_bytes(b"fixture\n")

            result = subprocess.run(
                [str(ROOT / "packaging" / "verify-rpm.sh"), "-fixture.rpm"],
                cwd=temp_path,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

        self.assertEqual(result.returncode, 0, result.stdout)

    def test_checksums_names_exact_sorted_release_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir) / 'release output "quoted"'
            temp_path.mkdir()
            for index, name in enumerate(RELEASE_ARTIFACTS):
                (temp_path / name).write_bytes(f"artifact {index}\n".encode("ascii"))
            unrelated_path = temp_path / "unrelated.txt"
            unrelated_path.write_bytes(b"must not be checksummed\n")

            result = subprocess.run(
                ["make", "checksums", f"DIST_DIR={temp_path}"],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(result.returncode, 0, result.stdout)

            manifest_path = temp_path / "SHA256SUMS"
            manifest_names = [
                line.split(None, 1)[1]
                for line in manifest_path.read_text(encoding="ascii").splitlines()
            ]
            self.assertEqual(manifest_names, RELEASE_ARTIFACTS)
            self.assertEqual(unrelated_path.read_bytes(), b"must not be checksummed\n")
            check = subprocess.run(
                ["sha256sum", "--check", "SHA256SUMS"],
                cwd=temp_path,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

        self.assertEqual(check.returncode, 0, check.stdout)

    def test_parallel_release_uses_one_fresh_stage_and_publishes_exact_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            environment, python_path, log_path = make_fake_release_tools(temp_path)
            dist_path = temp_path / 'release output "quoted"'
            dist_path.mkdir()
            for name in RELEASE_ARTIFACTS + ["SHA256SUMS"]:
                (dist_path / name).write_bytes(b"stale\n")

            result = subprocess.run(
                [
                    "make",
                    "-j4",
                    "release",
                    f"DIST_DIR={dist_path}",
                    f"PYTHON={python_path}",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            release_log = log_path.read_text() if log_path.exists() else "<missing>"
            self.assertEqual(
                result.returncode,
                0,
                f"{result.stdout}\nrelease log:\n{release_log}",
            )

            events = [line.split(":", 1) for line in log_path.read_text().splitlines()]
            self.assertEqual([event for event, _ in events], [
                "test",
                "wheel",
                "sdist",
                "wheel-smoke",
                "srpm",
                "rpm",
                "rpm-check",
            ])
            stage_paths = {path for _, path in events}
            self.assertEqual(len(stage_paths), 1)
            self.assertNotEqual(stage_paths, {str(dist_path)})
            self.assertEqual(
                sorted(path.name for path in dist_path.iterdir()),
                sorted(RELEASE_ARTIFACTS + ["SHA256SUMS"]),
            )
            for name in RELEASE_ARTIFACTS:
                self.assertTrue((dist_path / name).read_bytes().startswith(b"fresh "))
            check = subprocess.run(
                ["sha256sum", "--check", "SHA256SUMS"],
                cwd=dist_path,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

        self.assertEqual(check.returncode, 0, check.stdout)

    def test_release_rejects_and_preserves_unrelated_dist_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            environment, python_path, _ = make_fake_release_tools(temp_path)
            dist_path = temp_path / "release output with spaces"
            dist_path.mkdir()
            for name in RELEASE_ARTIFACTS + ["SHA256SUMS"]:
                (dist_path / name).write_bytes(b"stale\n")
            unrelated_path = dist_path / "user-notes.txt"
            unrelated_path.write_bytes(b"preserve me\n")

            result = subprocess.run(
                [
                    "make",
                    "-j4",
                    "release",
                    f"DIST_DIR={dist_path}",
                    f"PYTHON={python_path}",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            preserved = unrelated_path.read_bytes()

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("unrelated", result.stdout.lower())
        self.assertEqual(preserved, b"preserve me\n")

    def test_ci_runs_full_tests_and_builds_wheel_and_sdist(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

        self.assertIn("make test", workflow)
        self.assertRegex(
            makefile,
            r"(?m)^test:\s*\n\t\$\(PYTHON\) -m unittest discover -v$",
        )
        self.assertIn("make distributions", workflow)
        self.assertIn("cooler_btop-2.0.0.tar.gz", workflow)

    def test_packaging_tests_support_python_39_tomllib(self):
        source = (ROOT / "test_packaging.py").read_text(encoding="utf-8")
        ci = load_workflow("ci.yml")
        install = named_step(ci["jobs"]["python-package"], "Install package and test dependencies")

        self.assertIn(
            "try:\n    import tomllib\nexcept ModuleNotFoundError:\n    import tomli as tomllib",
            source,
        )
        self.assertIn("tomli", shlex.split(install["run"]))

    def test_ci_has_supported_python_matrix_and_fedora_release_job(self):
        workflow = load_workflow("ci.yml")

        self.assertEqual(workflow.get("on"), {
            "push": {"branches": ["main", "master"]},
            "pull_request": {"branches": ["main", "master"]},
        })
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(workflow["jobs"]), {"python-package", "fedora-release"})
        python_job = workflow["jobs"]["python-package"]
        self.assertEqual(
            python_job["strategy"]["matrix"]["python-version"],
            ["3.9", "3.14"],
        )
        fedora_job = workflow["jobs"]["fedora-release"]
        self.assertEqual(fedora_job["container"], "fedora:44")
        dependencies = named_step(fedora_job, "Install Fedora build dependencies")["run"]
        for dependency in (
            "appstream",
            "cpio",
            "desktop-file-utils",
            "pyproject-rpm-macros",
            "rpm-build",
        ):
            with self.subTest(dependency=dependency):
                self.assertIn(dependency, dependencies)
        self.assertEqual(
            named_step(fedora_job, "Build and validate release artifacts")["run"],
            "make release",
        )
        self.assertIn(
            "sha256sum --check SHA256SUMS",
            named_step(fedora_job, "Verify release checksums")["run"],
        )

    def test_python_ci_installs_build_backends_before_tests_and_distributions(self):
        job = load_workflow("ci.yml")["jobs"]["python-package"]
        setup = named_step(job, "Set up Python ${{ matrix.python-version }}")
        install = named_step(job, "Install package and test dependencies")
        commands = shell_command_invocations(install["run"])
        requirements = {
            argument
            for command in commands
            if command[:4] == ["python", "-m", "pip", "install"]
            for argument in command[4:]
        }
        for backend in ("setuptools>=61", "wheel"):
            with self.subTest(backend=backend):
                self.assertIn(backend, requirements)
        self.assertEqual(setup["with"]["python-version"], "${{ matrix.python-version }}")
        self.assertLess(job["steps"].index(setup), job["steps"].index(install))
        for consumer in ("Run unit tests", "Build wheel and source distribution"):
            with self.subTest(consumer=consumer):
                self.assertLess(job["steps"].index(install),
                                job["steps"].index(named_step(job, consumer)))

    def test_release_has_tag_only_build_and_publish_job_structure(self):
        release_path = ROOT / ".github" / "workflows" / "release.yml"
        self.assertTrue(release_path.is_file(), "release workflow is missing")
        workflow = load_workflow("release.yml")

        self.assertEqual(workflow.get("on"), {"push": {"tags": ["v*"]}})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(workflow["jobs"]), {"build", "publish"})
        build = workflow["jobs"]["build"]
        publish = workflow["jobs"]["publish"]
        self.assertEqual(build["container"], "fedora:44")
        self.assertNotIn("permissions", build)
        self.assertEqual(publish["needs"], "build")
        self.assertEqual(publish["permissions"], {"contents": "write"})
        tag_check = named_step(build, "Require tag to match project version")["run"]
        self.assertIn('expected_tag="v$(python3 -c', tag_check)
        self.assertIn('test "$GITHUB_REF_NAME" = "$expected_tag"', tag_check)
        self.assertEqual(
            named_step(build, "Build and validate release artifacts")["run"],
            "make release",
        )
        self.assertIn(
            "sha256sum --check SHA256SUMS",
            named_step(build, "Verify release checksums")["run"],
        )
        assert_publish_artifact_flow(self, workflow)

    def test_release_publish_explicitly_targets_triggering_repository(self):
        publish = load_workflow("release.yml")["jobs"]["publish"]
        release_step = named_step(publish, "Create GitHub release")

        self.assertEqual(release_step["env"], {
            "GH_REPO": "${{ github.repository }}",
            "GH_TOKEN": "${{ github.token }}",
        })

    def test_workflows_use_exact_release_artifact_paths_without_globs(self):
        ci_upload = named_step(
            load_workflow("ci.yml")["jobs"]["fedora-release"],
            "Upload Fedora release artifacts",
        )
        release = load_workflow("release.yml")
        release_upload = named_step(release["jobs"]["build"], "Upload release artifacts")

        for upload in (ci_upload, release_upload):
            with self.subTest(artifact=upload["with"]["name"]):
                paths = upload["with"]["path"].splitlines()
                self.assertEqual(paths, RELEASE_PATHS)
                self.assertFalse(any("*" in path for path in paths), paths)

        release_command = named_step(
            release["jobs"]["publish"], "Create GitHub release"
        )["run"]
        self.assertEqual(shlex.split(release_command), [
            "gh",
            "release",
            "create",
            "$GITHUB_REF_NAME",
            *RELEASE_PATHS,
            "--verify-tag",
            "--generate-notes",
        ])
        self.assertNotIn("*", release_command)

    def test_fedora_jobs_distinguish_not_installed_from_rpm_query_errors(self):
        jobs = (
            load_workflow("ci.yml")["jobs"]["fedora-release"],
            load_workflow("release.yml")["jobs"]["build"],
        )

        for job in jobs:
            with self.subTest(job=job["container"]):
                check = named_step(job, "Confirm package was not installed")["run"]
                self.assertEqual(check.strip(), RPM_ABSENCE_CHECK)

    def test_release_has_no_untrusted_or_secondary_publication_path(self):
        workflow = load_workflow("release.yml")

        self.assertEqual(set(workflow.get("on", {})), {"push"})
        self.assertNotIn("pull_request", workflow.get("on", {}))
        self.assertNotIn("release", workflow.get("on", {}))
        for permissions in (
            workflow["permissions"],
            workflow["jobs"]["publish"]["permissions"],
        ):
            self.assertNotIn("packages", permissions)
            self.assertNotIn("id-token", permissions)
        self.assertNotIn(
            "secrets.",
            (ROOT / ".github" / "workflows" / "release.yml").read_text(
                encoding="utf-8"
            ).casefold(),
        )
        assert_no_prohibited_run_commands(self, {"release.yml": workflow})

    def test_publish_artifact_flow_rejects_invalid_download_and_count_checks(self):
        workflow = load_workflow("release.yml")
        invalid_workflows = []

        wrong_name = copy.deepcopy(workflow)
        named_step(wrong_name["jobs"]["publish"], "Download release artifacts")[
            "with"
        ]["name"] = "other-files"
        invalid_workflows.append(("wrong artifact name", wrong_name))

        wrong_path = copy.deepcopy(workflow)
        named_step(wrong_path["jobs"]["publish"], "Download release artifacts")[
            "with"
        ]["path"] = "downloads"
        invalid_workflows.append(("wrong artifact path", wrong_path))

        wrong_action = copy.deepcopy(workflow)
        named_step(wrong_action["jobs"]["publish"], "Download release artifacts")[
            "uses"
        ] = "attacker/download-artifact@v5"
        invalid_workflows.append(("wrong download action", wrong_action))

        extra_download = copy.deepcopy(workflow)
        extra_download["jobs"]["publish"]["steps"].insert(1, {
            "uses": "actions/download-artifact@v4",
            "with": {"name": "untrusted-files", "path": "other"},
        })
        invalid_workflows.append(("extra download action version", extra_download))

        missing_count = copy.deepcopy(workflow)
        named_step(missing_count["jobs"]["publish"], "Verify downloaded artifacts")[
            "run"
        ] = "cd dist\nsha256sum --check SHA256SUMS"
        invalid_workflows.append(("missing file count", missing_count))

        non_regular_count = copy.deepcopy(workflow)
        verification = named_step(
            non_regular_count["jobs"]["publish"], "Verify downloaded artifacts"
        )
        verification["run"] = verification["run"].replace("-type f", "-type l")
        invalid_workflows.append(("non-regular file count", non_regular_count))

        inexact_count = copy.deepcopy(workflow)
        verification = named_step(
            inexact_count["jobs"]["publish"], "Verify downloaded artifacts"
        )
        verification["run"] = verification["run"].replace("-eq 5", "-ge 5")
        invalid_workflows.append(("inexact file count", inexact_count))

        count_after_checksum = copy.deepcopy(workflow)
        named_step(
            count_after_checksum["jobs"]["publish"], "Verify downloaded artifacts"
        )["run"] = (
            "cd dist\n"
            "sha256sum --check SHA256SUMS\n"
            'test "$(find dist -maxdepth 1 -type f | wc -l)" -eq 5'
        )
        invalid_workflows.append(("count after checksum", count_after_checksum))

        other_action_version = copy.deepcopy(workflow)
        named_step(
            other_action_version["jobs"]["publish"], "Download release artifacts"
        )["uses"] = "actions/download-artifact@future-version"
        assert_publish_artifact_flow(self, other_action_version)

        for reason, invalid_workflow in invalid_workflows:
            with self.subTest(reason=reason):
                with self.assertRaises(AssertionError):
                    assert_publish_artifact_flow(self, invalid_workflow)

    def test_run_command_contract_rejects_prohibited_operations_only(self):
        workflows = {
            "ci.yml": load_workflow("ci.yml"),
            "release.yml": load_workflow("release.yml"),
        }
        assert_no_prohibited_run_commands(self, workflows)

        disconnected = copy.deepcopy(workflows)
        disconnected["release.yml"]["review_note"] = (
            "Never run gcc, gh api, git tag, or dnf install dist/package.rpm"
        )
        disconnected["release.yml"]["jobs"]["publish"]["steps"].append({
            "name": "Commands prohibited: gcc, gh api, and git tag",
            "run": "# rpm -i dist/package.rpm\n# rm /usr/bin/btop\ntrue",
        })
        assert_no_prohibited_run_commands(self, disconnected)

        prohibited_commands = (
            ("generated RPM installation via dnf", "dnf install dist/cooler-btop.rpm"),
            (
                "generated RPM installation via dnf",
                "/usr/bin/dnf --assumeyes install ./cooler-btop.rpm",
            ),
            ("generated RPM installation via rpm", "rpm -i dist/cooler-btop.rpm"),
            ("generated RPM installation via rpm", "rpm -Uvh dist/cooler-btop.rpm"),
            (
                "generated RPM installation via rpm",
                "rpm --nodeps -i dist/cooler-btop.rpm",
            ),
            (
                "generated RPM installation via rpm",
                "MODE=test rpm --quiet -U dist/cooler-btop.rpm",
            ),
            (
                "generated RPM installation via rpm",
                "/usr/bin/env CHECK=1 /usr/bin/rpm --nodeps --install dist/cooler-btop.rpm",
            ),
            (
                "generated RPM installation via rpm",
                "true && rpm --quiet --upgrade dist/cooler-btop.rpm",
            ),
            ("/usr/bin/btop path use", "rm -f /usr/bin/btop"),
            ("native project compiler", "gcc -o native source.c"),
            ("native project compiler", "cc -o native source.c"),
            ("native project compiler", "clang -o native source.c"),
            ("native project compiler", "/usr/bin/gcc -o native source.c"),
            ("native project compiler", "cmake --build build"),
            ("native project compiler", "ninja -C build"),
            ("native project compiler", "cargo build --release"),
            ("native project compiler", "rustc source.rs"),
            (
                "GitHub API publication",
                "gh api repos/example/project/releases -f tag_name=v2.0.0",
            ),
            (
                "GitHub API publication",
                "env gh api repos/example/project/releases -f tag_name=v2.0.0",
            ),
            (
                "GitHub API publication",
                "gh --hostname github.com api repos/example/project/releases",
            ),
            ("git tag or push", "git tag v2.0.0"),
            ("git tag or push", "git -C . tag v2.0.0"),
            ("git tag or push", "env git push origin v2.0.0"),
        )
        for operation, command in prohibited_commands:
            with self.subTest(command=command):
                invalid = copy.deepcopy(workflows)
                invalid["release.yml"]["jobs"]["publish"]["steps"].insert(0, {
                    "name": "Injected prohibited operation",
                    "run": command,
                })
                with self.assertRaisesRegex(AssertionError, operation):
                    assert_no_prohibited_run_commands(self, invalid)

    def test_container_is_unprivileged_read_only_and_binds_its_api(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("USER cooler-btop", dockerfile)
        self.assertIn('"--host", "0.0.0.0"', dockerfile)
        self.assertNotIn("privileged:", compose)
        self.assertNotRegex(compose, r"(?m)^\s*pid:\s*[\"']?host")
        self.assertIn("read_only: true", compose)
        self.assertIn("cap_drop:", compose)
        self.assertRegex(compose, r"(?m)^\s*- ALL$")
        self.assertIn("no-new-privileges:true", compose)

    def test_docker_context_excludes_local_and_generated_files(self):
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for excluded in ("__pycache__/", "*.pyc", "dist/", "build/", "*.egg-info/",
                         "continuous_runner.log", ".git/"):
            self.assertIn(excluded, dockerignore)

    def test_obsolete_root_service_is_absent(self):
        self.assertFalse((ROOT / "cooler-btop.service").exists())

    def test_man_page_documents_installed_command(self):
        man_page = (ROOT / "packaging" / "cooler-btop.1").read_text(encoding="utf-8")

        self.assertIn(".TH COOLER-BTOP 1", man_page)
        self.assertIn("cooler-btop", man_page)
        self.assertIn("\\-\\-version", man_page)


if __name__ == "__main__":
    unittest.main()
