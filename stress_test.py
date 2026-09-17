import math
import os
import sys
import time
import subprocess
import requests
import threading


_REAL_POPEN = subprocess.Popen


def _stop_process(process):
    if process is None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def run_stress_test(port=8080, duration=None, stream_count=5, frames_per_connection=3):
    print("Starting Stress & Hardening Test (Stage D)...")

    if duration is None:
        try:
            duration = float(os.environ.get('COOLER_BTOP_STRESS_DURATION', '10'))
        except (TypeError, ValueError):
            duration = 10.0
    if not 1 <= stream_count <= 32:
        raise ValueError('stream_count must be between 1 and 32')
    if not 1 <= frames_per_connection <= 100:
        raise ValueError('frames_per_connection must be between 1 and 100')
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('duration must be a positive finite number')

    server_proc = None
    load_procs = []

    try:
        print("Launching daemon server...")
        sample_interval = max(0.05, min(1.0, duration / 10.0))
        server_proc = subprocess.Popen(
            [
                sys.executable, "-m", "cooler_btop", "--daemon",
                "--port", str(port), "--interval", str(sample_interval),
            ]
        )
        if isinstance(server_proc, _REAL_POPEN):
            deadline = time.monotonic() + min(duration, 15.0)
            while time.monotonic() < deadline:
                try:
                    response = requests.get(
                        f"http://127.0.0.1:{port}/api/metrics", timeout=2,
                    )
                    if response.status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("daemon did not become ready")
        else:
            # Keep the cleanup-focused unit tests independent of a live HTTP
            # endpoint when subprocess.Popen is replaced by a test double.
            time.sleep(2)

        print("Spawning dummy load processes to populate /proc...")
        for _ in range(4):
            process = subprocess.Popen([sys.executable, "-c", "while True: pass"])
            load_procs.append(process)

        print(f"Bombarding SSE endpoint for {duration:g} seconds...")
        url = f"http://127.0.0.1:{port}/api/metrics/stream"
        stream_errors = []

        def consume_stream():
            try:
                received = 0
                connection_deadline = time.monotonic() + duration + 5
                for connection_number in range(2):
                    if time.monotonic() >= connection_deadline:
                        stream_errors.append("stream reconnect deadline expired")
                        return
                    target = frames_per_connection if connection_number == 0 else 1
                    connection_received = 0
                    with requests.get(
                        url, stream=True,
                        timeout=(2, max(2, min(20, duration + 5))),
                    ) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if line.startswith(b'data: '):
                                connection_received += 1
                                received += 1
                                if connection_received >= target:
                                    break
                        if connection_received < target:
                            stream_errors.append(
                                f"stream {connection_number + 1} closed after "
                                f"{connection_received} frames"
                            )
                            return
                if received < frames_per_connection + 1:
                    stream_errors.append(f"stream reconnect delivered {received} frames")
            except requests.RequestException as error:
                stream_errors.append(str(error))

        threads = []
        for _ in range(stream_count):
            t = threading.Thread(target=consume_stream)
            t.start()
            threads.append(t)

        time.sleep(duration)

        for thread in threads:
            thread.join(timeout=max(5, duration + 5))
        assert all(not thread.is_alive() for thread in threads), "SSE consumers did not finish"
        assert not stream_errors, f"SSE requests failed: {stream_errors}"

        response = requests.get(f"http://127.0.0.1:{port}/api/metrics", timeout=2)
        response.raise_for_status()
        assert response.json()["procs"], "Metrics response had no process data"
        print("Concurrent SSE streams and metrics endpoint remained responsive.")

    finally:
        print("Cleaning up...")
        for process in load_procs:
            _stop_process(process)
        _stop_process(server_proc)

    print("Stress test completed successfully.")

if __name__ == "__main__":
    run_stress_test()
