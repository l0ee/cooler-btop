import tracemalloc
import time
from cooler_btop.data import DataCollector

def trace_memory_leaks(duration=10):
    print("Starting tracemalloc leak test...")
    tracemalloc.start()

    collector = DataCollector()

    # Take baseline snapshot
    snapshot1 = tracemalloc.take_snapshot()

    for _ in range(duration):
        collector.get_cpu()
        collector.get_mem()
        collector.get_disk()
        collector.get_net()
        collector.get_gpu()
        collector.get_procs()
        time.sleep(0.5)

    snapshot2 = tracemalloc.take_snapshot()

    top_stats = snapshot2.compare_to(snapshot1, 'lineno')

    print("[ Top 5 memory differences ]")
    for stat in top_stats[:5]:
        print(stat)

    total_diff = sum(stat.size_diff for stat in top_stats)
    print(f"\nTotal allocated size difference over {duration} iterations: {total_diff / 1024:.2f} KiB")

if __name__ == '__main__':
    trace_memory_leaks()
