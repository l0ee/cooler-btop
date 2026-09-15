import sys
import time
import subprocess
import requests
import threading

def run_stress_test(port=8080):
    print("Starting Stress & Hardening Test (Stage D)...")

    # 1. Spawn a background daemon server
    print("Launching daemon server...")
    server_proc = subprocess.Popen([sys.executable, "-m", "cooler_btop", "--daemon", "--port", str(port)])
    time.sleep(2) # let it boot

    # 2. Spawn dummy load processes
    print("Spawning dummy load processes to populate /proc...")
    load_procs = []
    for _ in range(4):
        p = subprocess.Popen([sys.executable, "-c", "while True: pass"])
        load_procs.append(p)

    try:
        # 3. Bombard the SSE endpoint and measure memory stability
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

        # Simulate multiple concurrent browser dashboard connections
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
        for p in load_procs:
            try:
                p.terminate()
            except Exception:
                pass
        server_proc.terminate()
        server_proc.wait()

    print("Stress test completed successfully.")

if __name__ == "__main__":
    run_stress_test()
