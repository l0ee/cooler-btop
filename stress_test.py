import sys
import time
import subprocess
import requests
import threading


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


def run_stress_test(port=8080):
    print("Starting Stress & Hardening Test (Stage D)...")

    server_proc = None
    load_procs = []

    try:
        print("Launching daemon server...")
        server_proc = subprocess.Popen(
            [sys.executable, "-m", "cooler_btop", "--daemon", "--port", str(port)]
        )
        time.sleep(2)

        print("Spawning dummy load processes to populate /proc...")
        for _ in range(4):
            process = subprocess.Popen([sys.executable, "-c", "while True: pass"])
            load_procs.append(process)

        print("Bombarding SSE endpoint for 10 seconds...")
        url = f"http://localhost:{port}/api/metrics/stream"
        stream_errors = []

        def consume_stream():
            try:
                with requests.get(url, stream=True, timeout=(2, 12)) as r:
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if line.startswith(b'data: '):
                            return
                stream_errors.append("stream closed before a metrics event")
            except Exception as error:
                stream_errors.append(str(error))

        threads = []
        for _ in range(5):
            t = threading.Thread(target=consume_stream)
            t.start()
            threads.append(t)

        time.sleep(10)

        for thread in threads:
            thread.join(timeout=5)
        assert all(not thread.is_alive() for thread in threads), "SSE consumers did not finish"
        assert not stream_errors, f"SSE requests failed: {stream_errors}"

        response = requests.get(f"http://localhost:{port}/api/metrics", timeout=2)
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
