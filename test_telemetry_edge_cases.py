import os
import tempfile
import unittest
from unittest import mock

from cooler_btop import data, fast_telemetry


class TelemetryEdgeCaseTests(unittest.TestCase):
    """Deterministic coverage for the remaining proc/sys telemetry hazards."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.proc = os.path.join(self.temp_dir.name, 'proc')
        self.sys = os.path.join(self.temp_dir.name, 'sys')
        self.root = os.path.join(self.temp_dir.name, 'root')
        os.makedirs(os.path.join(self.proc, 'sys', 'kernel'))
        os.makedirs(os.path.join(self.sys, 'class', 'thermal'))
        os.makedirs(os.path.join(self.sys, 'class', 'hwmon'))
        os.makedirs(os.path.join(self.root, 'etc'))
        self.write_proc(
            'stat',
            'cpu  100 50 25 200 10 5 5 5 0 0\n'
            'procs_running 2\nprocesses 99\n',
        )
        self.write_proc('meminfo', 'MemTotal:       1024 kB\nMemAvailable:    512 kB\n')
        self.write_proc('uptime', '123.0 0.0\n')
        self.write_proc(os.path.join('sys', 'kernel', 'hostname'), 'fixture-host\n')
        self.write_proc(os.path.join('sys', 'kernel', 'osrelease'), 'fixture-kernel\n')

        proc_patch = mock.patch.object(fast_telemetry, 'PROC_PATH', self.proc)
        sys_patch = mock.patch.object(fast_telemetry, 'SYS_PATH', self.sys)
        root_patch = mock.patch.object(fast_telemetry, 'HOST_ROOT', self.root)
        proc_patch.start()
        sys_patch.start()
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.addCleanup(sys_patch.stop)
        self.addCleanup(proc_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def write_proc(self, relative_path, content, binary=False):
        path = os.path.join(self.proc, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = 'wb' if binary else 'w'
        with open(path, mode) as stream:
            stream.write(content)

    def write_sys(self, relative_path, content, binary=False):
        path = os.path.join(self.sys, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = 'wb' if binary else 'w'
        with open(path, mode) as stream:
            stream.write(content)

    def add_process(self, pid, start_time, cmdline):
        directory = os.path.join(self.proc, str(pid))
        os.makedirs(directory)
        rest = ['S', '1'] + ['0'] * 50
        rest[11] = '10'
        rest[12] = '5'
        rest[19] = str(start_time)
        self.write_proc(
            os.path.join(str(pid), 'stat'),
            f'{pid} (worker {pid}) ' + ' '.join(rest) + '\n',
        )
        self.write_proc(os.path.join(str(pid), 'statm'), '0 2 0\n')
        self.write_proc(os.path.join(str(pid), 'cmdline'), cmdline, binary=True)

    def make_fast(self):
        return fast_telemetry.FastTelemetry()

    def configure_disk(self, name='sda', disk_sequence='10'):
        os.makedirs(os.path.join(self.sys, 'class', 'block', name, 'slaves'))
        self.write_sys(os.path.join('class', 'block', name, 'diskseq'), f'{disk_sequence}\n')

    def write_diskstats(self, name='sda', read_sectors=100, write_sectors=200,
                        major=8, minor=0):
        self.write_proc(
            'diskstats',
            f'{major} {minor} {name} 0 0 {read_sectors} 0 0 0 '
            f'{write_sectors} 0 0 0 0\n',
        )

    def test_meminfo_reads_fields_after_fixed_buffer_boundary(self):
        padding = 'Ignored: 0 kB\n' * 300
        self.write_proc(
            'meminfo',
            'MemTotal:       4096 kB\n'
            'MemFree:         256 kB\n'
            'Buffers:          128 kB\n'
            'Cached:           256 kB\n' +
            padding +
            'MemAvailable:    2048 kB\n'
            'SwapTotal:       1024 kB\n'
            'SwapFree:         512 kB\n',
        )
        self.assertGreater(os.path.getsize(os.path.join(self.proc, 'meminfo')), 2048)

        telemetry = self.make_fast()
        total, available, swap_total, swap_free, buffers, cached = telemetry.get_meminfo()

        self.assertEqual(total, 4096 * 1024)
        self.assertEqual(available, 2048 * 1024)
        self.assertEqual(swap_total, 1024 * 1024)
        self.assertEqual(swap_free, 512 * 1024)
        self.assertEqual(buffers, 128 * 1024)
        self.assertEqual(cached, 256 * 1024)

    def test_meminfo_falls_back_to_free_buffers_and_cached_without_memavailable(self):
        self.write_proc(
            'meminfo',
            'MemTotal:       1000 kB\n'
            'MemFree:         100 kB\n'
            'Buffers:          50 kB\n'
            'Cached:           150 kB\n',
        )
        telemetry = self.make_fast()
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._fast = telemetry

        memory = collector.get_mem()['mem']

        self.assertEqual(memory.available, 300 * 1024)
        self.assertEqual(memory.used, 700 * 1024)
        self.assertEqual(memory.percent, 70.0)

    def test_partial_fast_telemetry_initialization_closes_open_stat_file(self):
        stat_handle = mock.Mock()

        def open_side_effect(path, *args, **kwargs):
            if os.fspath(path) == os.path.join(self.proc, 'stat'):
                return stat_handle
            raise OSError('meminfo unavailable')

        with mock.patch('builtins.open', side_effect=open_side_effect):
            telemetry = self.make_fast()

        self.assertIsNone(telemetry._stat_file)
        self.assertIsNone(telemetry._mem_file)
        stat_handle.close.assert_called_once_with()

    def test_hwmon_finite_but_implausible_temperatures_fall_back_to_thermal_zone(self):
        for index, value in enumerate(('500000\n', '-200000\n')):
            base = os.path.join('class', 'hwmon', f'hwmon{index}')
            self.write_sys(os.path.join(base, 'name'), 'coretemp\n')
            self.write_sys(os.path.join(base, 'temp1_label'), f'Package id {index}\n')
            self.write_sys(os.path.join(base, 'temp1_input'), value)
        thermal = os.path.join('class', 'thermal', 'thermal_zone0')
        self.write_sys(os.path.join(thermal, 'type'), 'cpu-thermal\n')
        self.write_sys(os.path.join(thermal, 'temp'), '42000\n')

        telemetry = self.make_fast()

        self.assertEqual(telemetry._get_cpu_temperature(), 42.0)

    def test_missing_diskstats_invalidates_previous_io_baseline(self):
        self.configure_disk()
        self.write_diskstats(read_sectors=100, write_sectors=200)
        telemetry = self.make_fast()

        with mock.patch.object(fast_telemetry.time, 'monotonic',
                               side_effect=[1.0, 3.0, 5.0]):
            telemetry.get_disk_io()
            os.remove(os.path.join(self.proc, 'diskstats'))
            self.assertEqual(telemetry.get_disk_io(), (0, 0))
            self.write_diskstats(read_sectors=500, write_sectors=700)
            recovered = telemetry.get_disk_io()

        self.assertEqual(recovered, (0, 0))

    def test_zram_counters_are_not_reported_as_physical_disk_io(self):
        self.configure_disk(name='zram0')
        self.write_diskstats(name='zram0', read_sectors=100, write_sectors=200,
                             major=253)
        telemetry = self.make_fast()

        with mock.patch.object(fast_telemetry.time, 'monotonic',
                               side_effect=[1.0, 3.0]):
            telemetry.get_disk_io()
            self.write_diskstats(name='zram0', read_sectors=500, write_sectors=700,
                                 major=253)
            rates = telemetry.get_disk_io()

        self.assertEqual(rates, (0, 0))

    def test_proc_enumeration_permission_error_returns_empty_snapshot(self):
        telemetry = self.make_fast()

        with mock.patch.object(fast_telemetry.os, 'listdir',
                               side_effect=PermissionError('proc denied')):
            processes = telemetry.get_procs(total_mem_bytes=1024 * 1024)

        self.assertEqual(processes, [])

    def test_data_collector_falls_back_when_fast_proc_enumeration_is_denied(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._fast = mock.Mock()
        collector._fast.get_meminfo.return_value = (
            1024 * 1024, 512 * 1024, 0, 0, 0, 0,
        )
        collector._fast.get_procs.side_effect = PermissionError('proc denied')

        with mock.patch.object(data.psutil, 'process_iter', return_value=[]) as process_iter:
            processes = collector.get_procs()

        self.assertEqual(processes, [])
        process_iter.assert_called_once()

    def test_process_cmdline_is_bounded_before_it_enters_snapshot(self):
        long_cmdline = b'worker\0' + (b'argument-' * 2000)
        self.add_process(123, 456, long_cmdline)
        telemetry = self.make_fast()

        processes = telemetry.get_procs(total_mem_bytes=1024 * 1024)

        self.assertEqual(len(processes), 1)
        command = processes[0]['cmdline']
        self.assertTrue(command.startswith('worker'))
        self.assertLessEqual(len(command.encode('utf-8')), 4096)

    def test_malformed_tcp_row_does_not_drop_valid_listeners(self):
        def tcp_row(index, port):
            return (
                f'{index}: 0100007F:{port:04X} 00000000:0000 0A '
                '00000000:00000000 00:00000000 00000000 1000 0 12345 1 '
                '0000000000000000 100 0 0 10 0\n'
            )

        self.write_proc(
            os.path.join('net', 'tcp'),
            '  sl  local_address rem_address   st tx_queue rx_queue tr '
            'tm->when retrnsmt   uid  timeout inode\n' +
            tcp_row(0, 22) +
            '1: malformed-local-address 00000000:0000 0A '
            '00000000:00000000 00:00000000 00000000 1000 0 12346 1 '
            '0000000000000000 100 0 0 10 0\n' +
            tcp_row(2, 80),
        )
        telemetry = self.make_fast()

        connections = telemetry.get_connections()

        self.assertEqual([connection['port'] for connection in connections], [22, 80])
        self.assertTrue(all(connection['proto'] == 'TCP' for connection in connections))

    def test_privacy_redaction_masks_scoped_ipv6_zone_and_mac_derived_suffix(self):
        mountpoint = '/mnt/fe80::1%enx001122aabbcc'

        redacted = data._redact_private_tokens(mountpoint)

        self.assertEqual(redacted, '/mnt/[redacted]')
        self.assertNotIn('fe80::1', redacted)
        self.assertNotIn('001122aabbcc', redacted)


if __name__ == '__main__':
    unittest.main()
