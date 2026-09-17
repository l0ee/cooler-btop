"""Bounded subprocess coverage for the installed CLI and daemon lifecycle.

These tests deliberately use the source-tree module entrypoint rather than
mocking ``run_server``.  They need only the normal Python dependencies and the
Linux process information exposed by the host; Docker and special hardware
are not required.
"""

import http.client
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parent
STARTUP_TIMEOUT = 15.0
REQUEST_TIMEOUT = 2.0
SHUTDOWN_TIMEOUT = 8.0
STREAM_TIMEOUT = 8.0
DIRECT_OPENER = build_opener(ProxyHandler({}))


def _subprocess_environment():
    """Return an environment that imports this checkout in a child process."""

    environment = os.environ.copy()
    source_path = str(ROOT)
    existing_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not existing_path
        else os.pathsep.join((source_path, existing_path))
    )
    # ``requests`` is used by the real --status command.  Keep both loopback
    # addresses out of any ambient CI proxy configuration.
    for key in ("NO_PROXY", "no_proxy"):
        current = environment.get(key, "")
        entries = [entry for entry in current.split(",") if entry]
        for loopback in ("127.0.0.1", "127.0.0.2", "localhost"):
            if loopback not in entries:
                entries.append(loopback)
        environment[key] = ",".join(entries)
    return environment


def _port_is_available(host):
    """Return an unused TCP port on *host*, keeping the probe short-lived."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, 0))
        return probe.getsockname()[1]


def _paired_loopback_port():
    """Find a port that is free on both loopback addresses used by the test."""

    host = "127.0.0.2"
    try:
        alias_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        alias_probe.bind((host, 0))
    except OSError as error:
        raise unittest.SkipTest(
            "the test host does not provide an IPv4 loopback alias: "
            f"{host} ({error})"
        ) from error
    else:
        alias_probe.close()

    for _ in range(20):
        first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            for probe in (first, second):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            first.bind(("127.0.0.1", 0))
            port = first.getsockname()[1]
            second.bind((host, port))
            return port
        except OSError:
            continue
        finally:
            first.close()
            second.close()
    raise RuntimeError("could not find a paired loopback port")


class DaemonProcess:
    """Own one real daemon subprocess and make cleanup deterministic."""

    def __init__(
        self,
        host,
        port,
        *,
        db_path=None,
        interval=0.2,
        auth_token=None,
        privacy_mode=False,
        connect_host=None,
    ):
        command = [
            sys.executable,
            "-m",
            "cooler_btop",
            "--daemon",
            "--host",
            host,
            "--port",
            str(port),
            "--interval",
            str(interval),
        ]
        if db_path is not None:
            command.extend(("--log-db", str(db_path)))
        if auth_token is not None:
            command.extend(("--auth-token", auth_token))
        if privacy_mode:
            command.append("--privacy-mode")
        self.host = host
        self.connect_host = connect_host or host
        self.port = port
        self.auth_token = auth_token
        self.command = command
        self.process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=_subprocess_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.output = ""

    def _finish_output(self):
        if self.process.poll() is not None:
            try:
                self.output = self.process.communicate(timeout=1.0)[0]
            except subprocess.TimeoutExpired:
                # A child that has exited should not normally take this path,
                # but cleanup must stay bounded if its pipe is unexpectedly
                # inherited by another process.
                self.output = "<daemon output unavailable>"
        return self.output

    def _failure_details(self):
        return (
            f"daemon command failed ({self.command!r}, "
            f"return code {self.process.poll()}):\n{self._finish_output()}"
        )

    def wait_for_port(self, timeout=STARTUP_TIMEOUT):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self._finish_output()
                raise AssertionError(self._failure_details())
            try:
                with socket.create_connection(
                    (self.connect_host, self.port), timeout=REQUEST_TIMEOUT
                ):
                    return
            except OSError as error:
                last_error = error
                time.sleep(0.05)
        raise AssertionError(
            f"daemon did not open {self.connect_host}:{self.port} within {timeout}s; "
            f"last error: {last_error}\n{self._failure_details()}"
        )

    def wait_for_metrics(self, timeout=STARTUP_TIMEOUT):
        self.wait_for_port(timeout=timeout)
        deadline = time.monotonic() + timeout
        last_error = None
        url = f"http://{self.connect_host}:{self.port}/api/metrics"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self._finish_output()
                raise AssertionError(self._failure_details())
            try:
                headers = {"Accept-Encoding": "identity"}
                if self.auth_token:
                    headers["Authorization"] = f"Bearer {self.auth_token}"
                request = Request(url, headers=headers)
                with DIRECT_OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
                    payload = response.read()
                    if response.status == 200:
                        return json.loads(payload.decode("utf-8"))
                    last_error = f"HTTP {response.status}: {payload!r}"
            except HTTPError as error:
                try:
                    body = error.read()
                finally:
                    error.close()
                last_error = f"HTTP {error.code}: {body!r}"
            except (URLError, OSError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
            time.sleep(0.05)
        raise AssertionError(
            f"daemon did not provide metrics within {timeout}s; last error: "
            f"{last_error}\n{self._failure_details()}"
        )

    def stop(self, *, expected_returncode=None):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            self.output = self.process.communicate(timeout=SHUTDOWN_TIMEOUT)[0]
        except subprocess.TimeoutExpired as error:
            self.process.kill()
            self.output = self.process.communicate(timeout=2.0)[0]
            raise AssertionError(
                f"daemon did not exit after SIGTERM within {SHUTDOWN_TIMEOUT}s;\n"
                f"{self.output}"
            ) from error
        if expected_returncode is not None:
            self.assert_returncode(expected_returncode)
        return self.process.returncode, self.output

    def assert_returncode(self, expected):
        if self.process.returncode != expected:
            raise AssertionError(
                f"expected daemon return code {expected}, got "
                f"{self.process.returncode};\n{self.output}"
            )

    def __enter__(self):
        try:
            self.wait_for_metrics()
        except Exception:
            if self.process.poll() is None:
                try:
                    self.stop()
                except AssertionError:
                    # Preserve the startup failure as the useful test error;
                    # the bounded stop/kill path has still reclaimed the child.
                    pass
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.process.poll() is None:
            try:
                self.stop()
            except AssertionError:
                if exc_value is None:
                    raise


def _http_get(host, port, path, timeout=REQUEST_TIMEOUT, headers=None):
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    response = None
    try:
        request_headers = {"Accept-Encoding": "identity"}
        request_headers.update(headers or {})
        connection.request("GET", path, headers=request_headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        if response is not None:
            response.close()
        connection.close()


def _read_sse_frame(response):
    frame = bytearray()
    deadline = time.monotonic() + STREAM_TIMEOUT
    while time.monotonic() < deadline:
        line = response.readline()
        if not line:
            break
        frame.extend(line)
        if line in (b"\n", b"\r\n"):
            return bytes(frame)
    raise AssertionError(f"no complete SSE frame arrived within {STREAM_TIMEOUT}s: {frame!r}")


class TestRealDaemonLifecycle(unittest.TestCase):
    def test_metrics_and_stream_are_served_by_real_daemon(self):
        port = _port_is_available("127.0.0.1")
        with DaemonProcess("127.0.0.1", port, interval=0.2) as daemon:
            metrics = daemon.wait_for_metrics(timeout=STARTUP_TIMEOUT)
            self.assertEqual(metrics["interval"], 0.2)
            self.assertIn("cpu", metrics)
            self.assertIn("mem", metrics)
            self.assertIn("sys", metrics)

            connection = http.client.HTTPConnection(
                daemon.host, daemon.port, timeout=STREAM_TIMEOUT
            )
            response = None
            try:
                connection.request("GET", "/api/metrics/stream")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    response.getheader("Content-Type"), "text/event-stream"
                )
                frame = _read_sse_frame(response)
            finally:
                if response is not None:
                    response.close()
                connection.close()

            self.assertTrue(frame.startswith(b"data: "), frame)
            self.assertTrue(frame.endswith(b"\n\n"), frame)
            streamed_metrics = json.loads(frame[len(b"data: ") : -2])
            self.assertEqual(streamed_metrics["interval"], 0.2)
            status, _, body = _http_get(daemon.host, daemon.port, "/api/metrics")
            self.assertEqual(status, 200)
            latest_metrics = json.loads(body)
            self.assertEqual(latest_metrics["interval"], metrics["interval"])
            self.assertEqual(set(latest_metrics), set(metrics))

    def test_sigterm_exits_cleanly_and_leaves_readable_history(self):
        with tempfile.TemporaryDirectory(prefix="cooler-btop-integration-") as temp_dir:
            db_path = Path(temp_dir) / "metrics.sqlite"
            port = _port_is_available("127.0.0.1")
            daemon = DaemonProcess(
                "127.0.0.1", port, db_path=db_path, interval=0.2
            )
            try:
                daemon.wait_for_metrics()
                returncode, output = daemon.stop(expected_returncode=0)
                self.assertEqual(returncode, 0, output)
                self.assertIn("Server shutdown complete", output)
                with sqlite3.connect(db_path) as connection:
                    table = connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'metrics'"
                    ).fetchone()
                    count = connection.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
                self.assertIsNotNone(table)
                self.assertGreaterEqual(count, 1)
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(("127.0.0.1", port))
            finally:
                if daemon.process.poll() is None:
                    daemon.stop()

    def test_port_conflict_is_reported_without_leaving_second_daemon_running(self):
        port = _port_is_available("127.0.0.1")
        with DaemonProcess("127.0.0.1", port, interval=0.2):
            conflict = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "cooler_btop",
                    "--daemon",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--interval",
                    "0.2",
                ],
                cwd=str(ROOT),
                env=_subprocess_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                output = conflict.communicate(timeout=SHUTDOWN_TIMEOUT)[0]
            except subprocess.TimeoutExpired as error:
                conflict.kill()
                output = conflict.communicate(timeout=2.0)[0]
                self.fail(f"port-conflict process did not exit: {output}")
            self.assertNotEqual(conflict.returncode, 0, output)
            diagnostic = output.lower()
            self.assertTrue(
                any(
                    marker in diagnostic
                    for marker in (
                        "address already in use",
                        "already in use",
                        "eaddrinuse",
                        "errno 98",
                    )
                ),
                output,
            )

    def test_status_uses_the_requested_host(self):
        # Linux normally exposes 127.0.0.0/8, but some restricted CI
        # environments provide only 127.0.0.1.  In that case there is no
        # meaningful second local bind address with which to test --host.
        port = _paired_loopback_port()
        with DaemonProcess("127.0.0.2", port, interval=0.2) as daemon:
            daemon.wait_for_metrics()
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "cooler_btop",
                    "--status",
                    "--host",
                    daemon.host,
                    "--port",
                    str(daemon.port),
                ],
                cwd=str(ROOT),
                env=_subprocess_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=SHUTDOWN_TIMEOUT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("Cooler Btop Daemon: ONLINE", result.stdout)
            self.assertIn(f"Port {daemon.port}", result.stdout)

    def test_non_loopback_daemon_requires_token_and_masks_process_arguments(self):
        port = _port_is_available("127.0.0.1")
        token = "integration-secret"
        with DaemonProcess(
            "0.0.0.0",
            port,
            interval=0.2,
            auth_token=token,
            privacy_mode=True,
            connect_host="127.0.0.1",
        ) as daemon:
            status, headers, body = _http_get(
                daemon.connect_host, daemon.port, "/api/metrics",
            )
            self.assertEqual(status, 401)
            self.assertEqual(headers["WWW-Authenticate"], "Bearer")
            self.assertIn(b"Authentication required", body)

            status, _, body = _http_get(
                daemon.connect_host,
                daemon.port,
                "/api/metrics",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(status, 200)
            metrics = json.loads(body)
            self.assertTrue(metrics["procs"])
            for process in metrics["procs"]:
                self.assertEqual(process["cmdline"], process["name"])


if __name__ == "__main__":
    unittest.main()
