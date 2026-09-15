import time
import psutil
from cooler_btop.data import DataCollector

def run_benchmark(iterations=50):
    collector = DataCollector()

    start_time = time.time()
    cpu_usages = []

    # Let psutil calibrate
    collector.get_cpu()
    time.sleep(0.5)

    my_proc = psutil.Process()

    allocs = []

    print(f"Starting {iterations} iterations of DataCollector...")

    for _ in range(iterations):
        t1 = time.time()

        collector.get_cpu()
        collector.get_mem()
        collector.get_disk()
        collector.get_net()
        collector.get_procs()

        t2 = time.time()

        cpu_usages.append((t2 - t1) * 1000) # ms taken

    avg_ms = sum(cpu_usages) / len(cpu_usages)
    print(f"Average time per data collection tick: {avg_ms:.2f} ms")

if __name__ == '__main__':
    run_benchmark()
