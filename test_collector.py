import unittest
from cooler_btop.data import DataCollector
from cooler_btop.fast_telemetry import FastTelemetry

class TestCollectorAndTelemetry(unittest.TestCase):
    def setUp(self):
        self.collector = DataCollector()
        self.fast = FastTelemetry()
        # Initialize
        self.fast.get_cpu_percent()

    def test_sys_info(self):
        sys_info = self.fast.get_sys_info()
        self.assertIn('hostname', sys_info)
        self.assertIn('load_avg', sys_info)
        self.assertIn('cpu_temp', sys_info)
        self.assertIn('open_fds', sys_info)
        self.assertIn('max_fds', sys_info)

    def test_fast_cpu(self):
        total, cores = self.fast.get_cpu_percent()
        self.assertTrue(isinstance(total, float))
        self.assertTrue(isinstance(cores, list))

    def test_fast_mem(self):
        mt, ma, st, sf, buf, cache = self.fast.get_meminfo()
        self.assertTrue(mt > 0)
        self.assertTrue(ma > 0)

    def test_get_cpu(self):
        cpu = self.collector.get_cpu()
        self.assertIn('total', cpu)
        self.assertIn('per_core', cpu)

    def test_get_mem(self):
        mem = self.collector.get_mem()
        self.assertIn('mem', mem)
        self.assertIn('swap', mem)
        self.assertTrue(hasattr(mem['mem'], 'total'))
        self.assertTrue(hasattr(mem['mem'], 'buffers'))
        self.assertTrue(hasattr(mem['mem'], 'cached'))

    def test_get_disk(self):
        disk_data = self.collector.get_disk()
        self.assertIn('partitions', disk_data)
        self.assertIn('io', disk_data)
        partitions = disk_data['partitions']
        self.assertTrue(isinstance(partitions, list))
        if partitions:
            self.assertIn('mount', partitions[0])
            self.assertIn('total', partitions[0])

    def test_get_gpu(self):
        gpu = self.collector.get_gpu()
        self.assertTrue(isinstance(gpu, list))
        if gpu:
            self.assertIn('name', gpu[0])
            self.assertIn('load', gpu[0])

    def test_get_net(self):
        net = self.collector.get_net()
        self.assertIn('down', net)
        self.assertIn('total_up', net)

    def test_get_procs(self):
        procs = self.collector.get_procs()
        self.assertTrue(isinstance(procs, list))
        if procs:
            self.assertIn('pid', procs[0])
            self.assertIn('name', procs[0])
            self.assertIn('cpu_percent', procs[0])

if __name__ == '__main__':
    unittest.main()
