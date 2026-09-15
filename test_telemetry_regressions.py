import io
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


from cooler_btop import data, fast_telemetry


class TelemetryFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.proc = os.path.join(self.temp_dir.name, 'proc')
        self.sys = os.path.join(self.temp_dir.name, 'sys')
        self.root = os.path.join(self.temp_dir.name, 'root')
        os.makedirs(os.path.join(self.proc, 'sys', 'kernel'))
        os.makedirs(os.path.join(self.sys, 'class', 'thermal'))
        os.makedirs(os.path.join(self.root, 'etc'))
        self.write_proc('stat', self.cpu_stat(100, 50, 25, 200, 10, 5, 5, 5) +
                        'procs_running 2\nprocesses 99\n')
        self.write_proc('meminfo', 'MemTotal:       1024 kB\nMemAvailable:    512 kB\n')
        self.write_proc('uptime', '123.0 0.0\n')
        self.write_proc(os.path.join('sys', 'kernel', 'hostname'), 'fixture-host\n')
        self.write_proc(os.path.join('sys', 'kernel', 'osrelease'), 'fixture-kernel\n')
        self.proc_patch = mock.patch.object(fast_telemetry, 'PROC_PATH', self.proc)
        self.sys_patch = mock.patch.object(fast_telemetry, 'SYS_PATH', self.sys)
        self.root_patch = mock.patch.object(fast_telemetry, 'HOST_ROOT', self.root, create=True)
        self.proc_patch.start()
        self.sys_patch.start()
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.sys_patch.stop)
        self.addCleanup(self.proc_patch.stop)
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

    def write_root(self, relative_path, content):
        path = os.path.join(self.root, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as stream:
            stream.write(content)

    @staticmethod
    def cpu_stat(user, nice, system, idle, iowait, irq, softirq, steal,
                 guest=0, guest_nice=0, cores=0):
        lines = [f'cpu  {user} {nice} {system} {idle} {iowait} {irq} {softirq} '
                 f'{steal} {guest} {guest_nice}\n']
        for core in range(cores):
            lines.append(f'cpu{core} {user} {nice} {system} {idle} {iowait} '
                         f'{irq} {softirq} {steal} {guest} {guest_nice}\n')
        return ''.join(lines)

    @staticmethod
    def process_stat(pid, start_time, utime=10, stime=5, ppid=1):
        rest = ['S', str(ppid)] + ['0'] * 50
        rest[11] = str(utime)
        rest[12] = str(stime)
        rest[19] = str(start_time)
        return f'{pid} (worker {pid}) ' + ' '.join(rest) + '\n'

    def add_process(self, pid, start_time, uid=None, cmdline=None, utime=10, stime=5):
        directory = os.path.join(self.proc, str(pid))
        os.makedirs(directory)
        self.write_proc(os.path.join(str(pid), 'stat'),
                        self.process_stat(pid, start_time, utime, stime))
        self.write_proc(os.path.join(str(pid), 'statm'), '0 2 0\n')
        if uid is not None:
            self.write_proc(os.path.join(str(pid), 'status'), f'Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
        if cmdline is not None:
            self.write_proc(os.path.join(str(pid), 'cmdline'), cmdline, binary=True)

    def make_fast(self):
        return fast_telemetry.FastTelemetry()

    def test_cpu_percent_excludes_guest_and_guest_nice_from_total(self):
        self.write_proc('stat', self.cpu_stat(200, 100, 25, 200, 10, 5, 5, 5, 80, 20))
        telemetry = self.make_fast()
        telemetry.get_cpu_percent()
        self.write_proc('stat', self.cpu_stat(220, 120, 35, 220, 20, 15, 15, 15, 100, 30))
        total, _ = telemetry.get_cpu_percent()
        self.assertEqual(total, 62.5)

    def test_cpu_parser_reads_all_lines_beyond_fixed_buffer(self):
        telemetry = self.make_fast()
        initial = self.cpu_stat(100, 50, 25, 200, 10, 5, 5, 5, cores=150)
        self.write_proc('stat', initial)
        telemetry.get_cpu_percent()
        self.write_proc('stat', self.cpu_stat(110, 60, 35, 220, 20, 15, 15, 15, cores=150))
        _, cores = telemetry.get_cpu_percent()
        self.assertEqual(len(cores), 150)

    def test_cpu_core_baselines_follow_cpu_ids_when_lines_reorder(self):
        initial = ('cpu  0 0 0 100 0 0 0 0\n'
                   'cpu0 100 0 0 100 0 0 0 0\n'
                   'cpu2 0 100 0 300 0 0 0 0\n')
        updated = ('cpu  0 0 0 100 0 0 0 0\n'
                   'cpu2 0 110 0 300 0 0 0 0\n'
                   'cpu0 100 0 0 110 0 0 0 0\n')
        self.write_proc('stat', initial)
        telemetry = self.make_fast()
        telemetry.get_cpu_percent()
        self.write_proc('stat', updated)
        _, cores = telemetry.get_cpu_percent()
        self.assertEqual(cores, [100.0, 0.0])

    def test_hotplugged_cpu_warms_at_zero_and_ids_reach_collector(self):
        self.write_proc('stat',
                        'cpu  100 0 0 100 0 0 0 0\n'
                        'cpu2 100 0 0 100 0 0 0 0\n')
        telemetry = self.make_fast()
        telemetry.get_cpu_percent()
        self.write_proc('stat',
                        'cpu  400 0 0 50 0 0 0 0\n'
                        'cpu2 400 0 0 50 0 0 0 0\n'
                        'cpu7 1000 0 0 1000 0 0 0 0\n')
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._fast = telemetry
        with mock.patch.object(data.psutil, 'cpu_freq', return_value=None):
            cpu = collector.get_cpu()
        self.assertEqual(cpu['total'], 100.0)
        self.assertEqual(cpu['per_core'], [100.0, 0.0])
        self.assertEqual(cpu['core_ids'], [2, 7])

    def test_cpu_frequency_is_sampled_once(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._fast = mock.Mock()
        collector._fast.get_cpu_percent.return_value = (25.0, [25.0])
        collector._fast._last_cpu_ids = [0]
        frequency = SimpleNamespace(current=3200.0)
        with mock.patch.object(data.psutil, 'cpu_freq', return_value=frequency) as cpu_freq:
            cpu = collector.get_cpu()
        self.assertEqual(cpu['freq'], 3200.0)
        cpu_freq.assert_called_once_with()

    def test_sys_info_uses_host_roots_and_separates_tasks_from_forks(self):
        self.write_proc(os.path.join('sys', 'fs', 'file-nr'), '12 0 100\n')
        self.write_proc('loadavg', '1.00 0.50 0.25 1/10 123\n')
        self.write_sys(os.path.join('class', 'power_supply', 'BAT0', 'capacity'), '88\n')
        self.write_sys(os.path.join('class', 'power_supply', 'BAT0', 'status'), 'Charging\n')
        self.add_process(101, 10)
        self.add_process(102, 20)
        self.add_process(103, 30)
        info = self.make_fast().get_sys_info()
        self.assertEqual(info['procs_total'], 3)
        self.assertEqual(info['forks'], 99)
        self.assertEqual(info['battery'], '88% (Charging)')
        self.assertEqual(info['open_fds'], 12)
        self.assertEqual(info['max_fds'], 100)
        self.assertEqual(info['load_avg'], '1.00 0.50 0.25')

    def test_sys_specs_are_privacy_safe_and_static_fields_are_cached(self):
        self.write_root('etc/os-release', 'NAME=Nobara\nPRETTY_NAME="Nobara Linux 44"\n')
        self.write_proc('cpuinfo',
                        'processor\t: 0\nphysical id\t: 0\ncore id\t\t: 0\nmodel name\t: Intel Test CPU\n\n'
                        'processor\t: 1\nphysical id\t: 0\ncore id\t\t: 0\nmodel name\t: Intel Test CPU\n')
        for name, value in {
                'sys_vendor': 'ASUSTeK COMPUTER INC.', 'product_name': 'ROG Test',
                'product_version': '1.0', 'bios_version': 'GX550-999',
                'bios_date': '01/02/2026', 'chassis_type': '10',
                'product_serial': 'DO-NOT-READ', 'product_uuid': 'DO-NOT-READ',
        }.items():
            self.write_sys(os.path.join('class', 'dmi', 'id', name), value + '\n')

        forbidden = {'machine-id', 'boot_id', 'product_serial', 'product_uuid'}
        real_open = open

        def privacy_open(path, *args, **kwargs):
            if os.path.basename(os.fspath(path)) in forbidden:
                raise AssertionError(f'private identifier read: {path}')
            return real_open(path, *args, **kwargs)

        with mock.patch('builtins.open', side_effect=privacy_open):
            telemetry = self.make_fast()
            first = telemetry.get_sys_info()
            self.write_root('etc/os-release', 'PRETTY_NAME="Changed OS"\n')
            second = telemetry.get_sys_info()

        self.assertEqual(first['specs'], {
            'os': 'Nobara Linux 44', 'architecture': mock.ANY, 'chassis': 'Notebook',
            'vendor': 'ASUSTeK COMPUTER INC.', 'product_model': 'ROG Test',
            'product_version': '1.0', 'bios_version': 'GX550-999',
            'bios_date': '01/02/2026', 'cpu_model': 'Intel Test CPU',
            'logical_cores': 2, 'physical_cores': 1,
        })
        self.assertEqual(second['specs']['os'], 'Nobara Linux 44')
        serialized = repr(first).casefold()
        for private_name in ('serial', 'uuid', 'machine-id', 'boot-id', 'mac', 'ip'):
            self.assertNotIn(private_name, serialized)

    def test_cpu_specs_use_portable_model_fields(self):
        self.write_proc('cpuinfo',
                        'processor\t: 0\ncpu\t\t: POWER9 revision 2.3\n\n'
                        'processor\t: 1\ncpu\t\t: POWER9 revision 2.3\n')
        with mock.patch.object(fast_telemetry.platform, 'machine', return_value='ppc64le'):
            specs = self.make_fast()._get_specs()
        self.assertEqual(specs['cpu_model'], 'POWER9 revision 2.3')

    def test_physical_core_count_falls_back_to_sysfs_topology(self):
        self.write_proc('cpuinfo', ''.join(
            f'processor\t: {processor}\n\n' for processor in range(4)
        ))
        for processor, core in enumerate((0, 0, 1, 1)):
            topology = os.path.join('devices', 'system', 'cpu', f'cpu{processor}', 'topology')
            self.write_sys(os.path.join(topology, 'physical_package_id'), '0\n')
            self.write_sys(os.path.join(topology, 'core_id'), f'{core}\n')
        specs = self.make_fast()._get_specs()
        self.assertEqual(specs['logical_cores'], 4)
        self.assertEqual(specs['physical_cores'], 2)

    def test_partial_proc_cpu_topology_falls_back_to_complete_sysfs_topology(self):
        self.write_proc('cpuinfo',
                        'processor\t: 0\nphysical id\t: 0\ncore id\t\t: 0\n\n'
                        'processor\t: 1\n\nprocessor\t: 2\n\nprocessor\t: 3\n')
        for processor, core in enumerate((0, 0, 1, 1)):
            topology = os.path.join('devices', 'system', 'cpu', f'cpu{processor}', 'topology')
            self.write_sys(os.path.join(topology, 'physical_package_id'), '0\n')
            self.write_sys(os.path.join(topology, 'core_id'), f'{core}\n')
        specs = self.make_fast()._get_specs()
        self.assertEqual(specs['physical_cores'], 2)

    def test_empty_cpuinfo_uses_sysfs_for_logical_and_physical_counts(self):
        self.write_proc('cpuinfo', '')
        for processor in range(2):
            topology = os.path.join('devices', 'system', 'cpu', f'cpu{processor}', 'topology')
            self.write_sys(os.path.join(topology, 'physical_package_id'), '0\n')
            self.write_sys(os.path.join(topology, 'core_id'), f'{processor}\n')
        with mock.patch.object(fast_telemetry.os, 'cpu_count', return_value=64):
            specs = self.make_fast()._get_specs()
        self.assertEqual(specs['logical_cores'], 2)
        self.assertEqual(specs['physical_cores'], 2)

    def test_cpu_summary_block_does_not_count_as_logical_processor(self):
        self.write_proc('cpuinfo',
                        'processor\t: 0\n\nprocessor\t: 1\n\n'
                        'Hardware\t: Example ARM Board\nRevision\t: 1\nSerial\t\t: private\n')
        for processor in range(2):
            topology = os.path.join('devices', 'system', 'cpu', f'cpu{processor}', 'topology')
            self.write_sys(os.path.join(topology, 'physical_package_id'), '0\n')
            self.write_sys(os.path.join(topology, 'core_id'), f'{processor}\n')
        specs = self.make_fast()._get_specs()
        self.assertEqual(specs['cpu_model'], 'Example ARM Board')
        self.assertEqual(specs['logical_cores'], 2)
        self.assertEqual(specs['physical_cores'], 2)

    def test_hwmon_sensors_classify_components_and_include_asus_fans(self):
        fixtures = {
            ('hwmon0', 'name'): 'coretemp\n',
            ('hwmon0', 'temp1_label'): 'Package id 0\n',
            ('hwmon0', 'temp1_input'): '55000\n',
            ('hwmon0', 'temp2_label'): 'Core 0\n',
            ('hwmon0', 'temp2_input'): '51000\n',
            ('hwmon0', 'temp3_label'): 'Core 1\n',
            ('hwmon0', 'temp3_input'): '53000\n',
            ('hwmon1', 'name'): 'nvme\n',
            ('hwmon1', 'temp1_label'): 'Composite\n',
            ('hwmon1', 'temp1_input'): '42000\n',
            ('hwmon2', 'name'): 'spd5118\n',
            ('hwmon2', 'temp1_input'): '41000\n',
            ('hwmon3', 'name'): 'iwlwifi_1\n',
            ('hwmon3', 'temp1_input'): '46000\n',
            ('hwmon4', 'name'): 'amdgpu\n',
            ('hwmon4', 'temp1_label'): 'edge\n',
            ('hwmon4', 'temp1_input'): '49000\n',
            ('hwmon5', 'name'): 'asus\n',
            ('hwmon5', 'fan1_label'): 'cpu_fan\n',
            ('hwmon5', 'fan1_input'): '3200\n',
            ('hwmon5', 'fan2_label'): 'gpu_fan\n',
            ('hwmon5', 'fan2_input'): '2800\n',
        }
        for (hwmon, name), value in fixtures.items():
            self.write_sys(os.path.join('class', 'hwmon', hwmon, name), value)
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp1_crit'), '100000\n')

        info = self.make_fast().get_sys_info()
        sensors = info['sensors']
        by_category = {sensor['category'] for sensor in sensors}
        self.assertTrue({'CPU', 'NVMe', 'DIMM', 'Wi-Fi', 'GPU', 'Fan'} <= by_category)
        self.assertIn(
            {'label': 'CPU Cores (max)', 'category': 'CPU', 'type': 'temperature',
             'value': 53.0, 'unit': 'C'}, sensors,
        )
        self.assertIn(
            {'label': 'CPU fan', 'category': 'Fan', 'type': 'fan',
             'value': 3200.0, 'unit': 'RPM'}, sensors,
        )
        self.assertEqual(info['cpu_temp'], 55.0)
        self.assertLessEqual(len(sensors), 24)
        self.assertNotIn('crit', repr(sensors).casefold())

    def test_battery_info_normalizes_units_and_estimates_time(self):
        base = os.path.join('class', 'power_supply', 'BAT0')
        for name, value in {
                'type': 'Battery\n', 'capacity': '75\n', 'status': 'Discharging\n',
                'energy_now': '45000000\n', 'energy_full': '60000000\n',
                'power_now': '15000000\n', 'voltage_now': '12000000\n',
        }.items():
            self.write_sys(os.path.join(base, name), value)
        self.write_sys(os.path.join('class', 'power_supply', 'BAT1', 'type'), 'Battery\n')
        info = self.make_fast().get_sys_info()
        self.assertEqual(info['battery'], '75% (Discharging)')
        self.assertEqual(info['battery_info'][0], {
            'name': 'BAT0', 'percentage': 75.0, 'status': 'Discharging',
            'power_w': 15.0, 'energy_now_wh': 45.0, 'energy_full_wh': 60.0,
            'voltage_v': 12.0, 'time_remaining_s': 10800,
        })
        self.assertEqual(info['battery_info'][1]['name'], 'BAT1')
        self.assertIsNone(info['battery_info'][1]['percentage'])

    def test_non_finite_sensor_and_gpu_values_are_rejected(self):
        battery = os.path.join('class', 'power_supply', 'BAT0')
        self.write_sys(os.path.join(battery, 'type'), 'Battery\n')
        self.write_sys(os.path.join(battery, 'capacity'), 'nan\n')
        hwmon = os.path.join('class', 'hwmon', 'hwmon0')
        self.write_sys(os.path.join(hwmon, 'name'), 'coretemp\n')
        self.write_sys(os.path.join(hwmon, 'temp1_label'), 'Package id 0\n')
        self.write_sys(os.path.join(hwmon, 'temp1_input'), 'nan\n')
        thermal = os.path.join('class', 'thermal', 'thermal_zone0')
        self.write_sys(os.path.join(thermal, 'type'), 'cpu-thermal\n')
        self.write_sys(os.path.join(thermal, 'temp'), 'inf\n')
        info = self.make_fast().get_sys_info()
        self.assertIsNone(info['battery_info'][0]['percentage'])
        self.assertEqual(info['cpu_temp'], 0.0)
        self.assertIsNone(data.DataCollector._optional_float('inf'))
        self.assertIsNone(data.DataCollector._optional_float('-inf'))
        self.write_sys(os.path.join('class', 'drm', 'card0', 'device', 'vendor'), '0x1002\n')
        self.write_sys(os.path.join('class', 'drm', 'card0', 'device',
                                    'gpu_busy_percent'), 'inf\n')
        collector = data.DataCollector.__new__(data.DataCollector)
        with mock.patch.object(data, 'SYS_PATH', self.sys):
            self.assertIsNone(collector._get_drm_gpus()[0]['load'])

    def test_zram_and_disk_inventory_use_only_non_unique_metadata(self):
        self.write_sys(os.path.join('block', 'zram0', 'mm_stat'),
                       '1048576 524288 600000 0 2097152 700000 3 4 0\n')
        block = os.path.join('class', 'block', 'nvme0n1')
        self.write_sys(os.path.join(block, 'device', 'model'), 'Fast NVMe\n')
        self.write_sys(os.path.join(block, 'device', 'vendor'), 'Example Vendor\n')
        self.write_sys(os.path.join(block, 'size'), '2000000\n')
        self.write_sys(os.path.join(block, 'queue', 'rotational'), '0\n')
        self.write_sys(os.path.join(block, 'device', 'serial'), 'DO-NOT-READ\n')
        self.write_sys(os.path.join(block, 'wwid'), 'DO-NOT-READ\n')
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._fast = mock.Mock()
        collector._fast.get_disk_io.return_value = (10, 20)
        usage = SimpleNamespace(total=10 ** 9, used=4 * 10 ** 8, free=6 * 10 ** 8,
                                percent=40.0)
        partition = SimpleNamespace(device='/dev/nvme0n1p3', mountpoint='/', fstype='btrfs')
        forbidden = {'serial', 'wwid'}
        real_open = open

        def privacy_open(path, *args, **kwargs):
            if os.path.basename(os.fspath(path)) in forbidden:
                raise AssertionError(f'private identifier read: {path}')
            return real_open(path, *args, **kwargs)

        with mock.patch.object(data, 'SYS_PATH', self.sys), \
                mock.patch.object(data.psutil, 'disk_partitions', return_value=[partition]), \
                mock.patch.object(data.psutil, 'disk_usage', return_value=usage), \
                mock.patch('builtins.open', side_effect=privacy_open):
            result = collector.get_disk()
            cached = collector.get_disk()

        self.assertEqual(result['zram'][0], {
            'name': 'zram0', 'original_bytes': 1048576, 'compressed_bytes': 524288,
            'memory_bytes': 600000, 'limit_bytes': None,
        })
        self.assertEqual(result['partitions'][0]['filesystem'], 'btrfs')
        self.assertEqual(result['partitions'][0]['model'], 'Fast NVMe')
        self.assertEqual(result['partitions'][0]['type'], 'NVMe SSD')
        self.assertEqual(result['devices'][0], {
            'name': 'nvme0n1', 'model': 'Fast NVMe', 'vendor': 'Example Vendor',
            'type': 'NVMe SSD', 'capacity': 1024000000,
        })
        self.assertEqual(cached['devices'], result['devices'])
        self.assertNotIn('serial', repr(result).casefold())
        self.assertNotIn('wwid', repr(result).casefold())

    def test_partition_output_redacts_uuid_and_remote_ip_sources(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._fast = mock.Mock()
        collector._fast.get_disk_io.return_value = (0, 0)
        uuid = '123e4567-e89b-12d3-a456-426614174000'
        partitions = [
            SimpleNamespace(device=f'/dev/disk/by-uuid/{uuid}',
                            mountpoint=f'/run/media/user/{uuid}', fstype='ext4'),
            SimpleNamespace(device='192.0.2.10:/export', mountpoint='/mnt/share', fstype='nfs'),
            SimpleNamespace(device='/dev/sda1', mountpoint='/run/media/user/ABCD-1234',
                            fstype='vfat'),
            SimpleNamespace(device='/dev/sdb1', mountpoint='/run/media/user/0123456789ABCDEF',
                            fstype='ntfs'),
            SimpleNamespace(device='/dev/sdc1', mountpoint='/run/media/user/AABBCCDDEEFF',
                            fstype='ext4'),
            SimpleNamespace(device='/dev/sdd1', mountpoint='/run/media/user/AA-BB-CC-DD-EE-FF',
                            fstype='ext4'),
            SimpleNamespace(device='/dev/sde1', mountpoint='/logs/12:30:00/foo::bar',
                            fstype='ext4'),
            SimpleNamespace(device='/dev/sdf1', mountpoint='/mnt/server=192.0.2.10:2049',
                            fstype='nfs'),
        ]
        usage = SimpleNamespace(total=100, used=40, free=60, percent=40.0)
        with mock.patch.object(data, 'SYS_PATH', self.sys), \
                mock.patch.object(data.psutil, 'disk_partitions', return_value=partitions), \
                mock.patch.object(data.psutil, 'disk_usage', return_value=usage):
            result = collector.get_disk()
        self.assertEqual(result['partitions'][0]['device'], 'Persistent device')
        self.assertEqual(result['partitions'][0]['mount'], '/run/media/user/[redacted]')
        self.assertEqual(result['partitions'][1]['device'], 'Network filesystem')
        for partition in result['partitions'][2:6]:
            self.assertEqual(partition['mount'], '/run/media/user/[redacted]')
        self.assertEqual(result['partitions'][6]['mount'], '/logs/12:30:00/foo::bar')
        self.assertEqual(result['partitions'][7]['mount'], '/mnt/server=[redacted]:2049')
        self.assertNotIn(uuid, repr(result))
        self.assertNotIn('192.0.2.10', repr(result))
        for private_value in ('ABCD-1234', '0123456789ABCDEF', 'AABBCCDDEEFF',
                              'AA-BB-CC-DD-EE-FF'):
            self.assertNotIn(private_value, repr(result))

    def test_hybrid_gpu_preserves_intel_and_nvidia_and_parses_na_per_field(self):
        self.write_sys(os.path.join('class', 'drm', 'card0', 'device', 'vendor'), '0x8086\n')
        self.write_sys(os.path.join('class', 'drm', 'card1', 'device', 'vendor'), '0x10de\n')
        collector = data.DataCollector.__new__(data.DataCollector)
        output = (b'NVIDIA Test, N/A, 1024, 8192, 61, N/A, 0, 210, N/A\n')
        with mock.patch.object(data, 'SYS_PATH', self.sys), \
                mock.patch.object(data.time, 'monotonic',
                                  side_effect=[10.0, 10.0, 11.0, 11.0, 12.1, 12.1]), \
                mock.patch.object(data.subprocess, 'check_output', return_value=output) as smi:
            first = collector.get_gpu()
            second = collector.get_gpu()
            third = collector.get_gpu()

        self.assertEqual([gpu['vendor'] for gpu in first], ['Intel', 'NVIDIA'])
        nvidia = first[1]
        self.assertIsNone(nvidia['load'])
        self.assertEqual(nvidia['mem_pct'], 12.5)
        self.assertEqual(nvidia['temperature'], 61.0)
        self.assertIsNone(nvidia['power_w'])
        self.assertEqual(nvidia['fan_pct'], 0.0)
        self.assertEqual(nvidia['clock_graphics_mhz'], 210.0)
        self.assertIsNone(nvidia['clock_memory_mhz'])
        self.assertEqual(nvidia['status'], 'sleeping or unavailable')
        self.assertEqual(second, first)
        self.assertEqual(third, first)
        self.assertEqual(smi.call_count, 2)
        for call in smi.call_args_list:
            self.assertLessEqual(call.kwargs['timeout'], 1.0)
        self.assertNotIn('uuid', repr(first).casefold())

    def test_transient_nvidia_failure_is_retried_without_negative_caching(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        output = b'NVIDIA Test, 25, 1024, 8192, 61, 40, N/A, 1200, 7000\n'
        failure = subprocess.TimeoutExpired('nvidia-smi', 0.8)
        with mock.patch.object(data.time, 'monotonic', side_effect=[10.0, 10.1]), \
                mock.patch.object(data.subprocess, 'check_output',
                                  side_effect=[failure, output]):
            self.assertEqual(collector._get_nvidia_gpus(), [])
            recovered = collector._get_nvidia_gpus()
        self.assertTrue(recovered)
        self.assertEqual(recovered[0]['name'], 'NVIDIA Test')
        self.assertEqual(recovered[0]['load'], 25.0)

    def test_persistent_nvidia_failures_use_bounded_backoff(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        failure = subprocess.TimeoutExpired('nvidia-smi', 0.8)
        with mock.patch.object(data.time, 'monotonic',
                              side_effect=[10.0, 10.1, 10.2, 12.2]), \
                mock.patch.object(data.subprocess, 'check_output',
                                  side_effect=failure) as smi:
            results = [collector._get_nvidia_gpus() for _ in range(4)]
        self.assertEqual(results, [[], [], [], []])
        self.assertEqual(smi.call_count, 3)

    def test_nvidia_failure_preserves_last_success_as_stale(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        output = b'NVIDIA Test, 25, 1024, 8192, 61, 40, N/A, 1200, 7000\n'
        failure = subprocess.TimeoutExpired('nvidia-smi', 0.8)
        with mock.patch.object(data.time, 'monotonic', side_effect=[0.0, 3.0]), \
                mock.patch.object(data.subprocess, 'check_output',
                                  side_effect=[output, failure]):
            available = collector._get_nvidia_gpus()
            stale = collector._get_nvidia_gpus()
        self.assertEqual(available[0]['status'], 'available')
        self.assertTrue(stale)
        self.assertEqual(stale[0]['name'], 'NVIDIA Test')
        self.assertEqual(stale[0]['status'], 'stale telemetry')

    def test_nvidia_stale_cache_expires_during_persistent_failure(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        output = b'NVIDIA Test, 25, 1024, 8192, 61, 40, N/A, 1200, 7000\n'
        failure = subprocess.TimeoutExpired('nvidia-smi', 0.8)
        with mock.patch.object(data.time, 'monotonic', side_effect=[0.0, 31.0]), \
                mock.patch.object(data.subprocess, 'check_output',
                                  side_effect=[output, failure]):
            collector._get_nvidia_gpus()
            expired = collector._get_nvidia_gpus()
        self.assertEqual(expired, [])

    def test_network_records_include_link_metadata_without_addresses(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._interface_index = lambda _name: 2
        collector._interface_addresses = lambda: {'wlan0': '00:11:22:33:44:55'}
        collector.last_time = 10.0
        collector._last_net_by_interface = {
            ('wlan0', 2, '00:11:22:33:44:55'):
                SimpleNamespace(bytes_recv=100, bytes_sent=200),
        }
        current = {'wlan0': SimpleNamespace(bytes_recv=120, bytes_sent=240)}
        stats = {'wlan0': SimpleNamespace(isup=True, speed=866, mtu=1500)}
        with mock.patch.object(data.time, 'monotonic', return_value=12.0), \
                mock.patch.object(data.psutil, 'net_io_counters', return_value=current), \
                mock.patch.object(data.psutil, 'net_if_stats', return_value=stats), \
                mock.patch.object(data.psutil, 'net_if_addrs', side_effect=AssertionError('must not read addresses')):
            net = collector.get_net()
        self.assertEqual(net['interfaces'], [{
            'name': 'wlan0', 'down': 10.0, 'up': 20.0,
            'total_down': 120, 'total_up': 240, 'is_up': True,
            'speed_mbps': 866, 'mtu': 1500,
        }])
        for private_name in ('address', 'mac', 'ip'):
            self.assertNotIn(private_name, repr(net).casefold())

    def test_import_does_not_attempt_to_load_native_telemetry(self):
        script = (
            "import ctypes; "
            "ctypes.CDLL = lambda *args: (_ for _ in ()).throw(SystemExit(9)); "
            "import cooler_btop.fast_telemetry"
        )
        result = subprocess.run([sys.executable, '-c', script], cwd=os.path.dirname(__file__))
        self.assertEqual(result.returncode, 0)

    def test_sys_info_always_reads_proc_uptime(self):
        real_open = open
        uptime_reads = []

        def tracked_open(path, *args, **kwargs):
            if os.fspath(path) == '/proc/uptime':
                uptime_reads.append(path)
                return io.BytesIO(b'120.0 0.0\n')
            return real_open(path, *args, **kwargs)

        with mock.patch.object(fast_telemetry, 'PROC_PATH', '/proc'), \
             mock.patch('builtins.open', side_effect=tracked_open):
            info = self.make_fast().get_sys_info()
        self.assertEqual(info['uptime'], '0d 0h 2m')
        self.assertEqual(uptime_reads, ['/proc/uptime'])

    def test_hwmon_package_temperature_wins_over_acpi_zone(self):
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'name'), 'coretemp\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp1_label'), 'Package id 0\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp1_input'), '55000\n')
        self.write_sys(os.path.join('class', 'thermal', 'thermal_zone0', 'type'), 'acpitz\n')
        self.write_sys(os.path.join('class', 'thermal', 'thermal_zone0', 'temp'), '70000\n')
        info = self.make_fast().get_sys_info()
        self.assertEqual(info['cpu_temp'], 55.0)

    def test_amd_k10temp_prefers_tdie_over_tctl(self):
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'name'), 'k10temp\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp1_label'), 'Tctl\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp1_input'), '75000\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp2_label'), 'Tdie\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp2_input'), '62000\n')
        self.assertEqual(self.make_fast().get_sys_info()['cpu_temp'], 62.0)

    def test_intel_hwmon_selects_lowest_package_id_deterministically(self):
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'name'), 'coretemp\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp1_label'), 'Package id 1\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp1_input'), '70000\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp2_label'), 'Package id 0\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp2_input'), '55000\n')
        self.assertEqual(self.make_fast().get_sys_info()['cpu_temp'], 55.0)

    def test_package_label_from_non_coretemp_driver_is_not_cpu_temperature(self):
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'name'), 'acpitz\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp1_label'), 'Package id 0\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp1_input'), '80000\n')
        self.write_sys(os.path.join('class', 'thermal', 'thermal_zone0', 'type'), 'cpu-thermal\n')
        self.write_sys(os.path.join('class', 'thermal', 'thermal_zone0', 'temp'), '50000\n')
        self.assertEqual(self.make_fast().get_sys_info()['cpu_temp'], 50.0)

    def test_process_metadata_reads_uid_username_and_cmdline(self):
        uid = os.getuid()
        self.add_process(123, 456, uid=uid, cmdline=b'python\0--worker\0')
        process = self.make_fast().get_procs()[0]
        self.assertEqual(process['uid'], uid)
        self.assertEqual(process['start_time'], 456)
        self.assertEqual(process['username'], pwd.getpwuid(uid).pw_name)
        self.assertEqual(process['cmdline'], 'python --worker')

    def test_process_without_readable_uid_reports_unavailable_username(self):
        self.add_process(123, 456, cmdline=b'worker\0')
        process = self.make_fast().get_procs()[0]
        self.assertIsNone(process['uid'])
        self.assertEqual(process['username'], 'Unavailable')

    def test_process_start_time_is_revalidated_after_metadata_reads(self):
        self.add_process(123, 100, uid=os.getuid(), cmdline=b'old-worker\0')
        telemetry = self.make_fast()
        stat_path = os.path.join(self.proc, '123', 'stat')
        cmdline_path = os.path.join(self.proc, '123', 'cmdline')
        real_open = open

        def racing_open(path, *args, **kwargs):
            if os.fspath(path) == cmdline_path:
                with real_open(stat_path, 'w') as stream:
                    stream.write(self.process_stat(123, 200, utime=100))
            return real_open(path, *args, **kwargs)

        with mock.patch('builtins.open', side_effect=racing_open):
            processes = telemetry.get_procs()
        self.assertEqual(processes, [])
        self.assertEqual(telemetry._prev_proc_cpu, {})

    def test_pid_reuse_does_not_inherit_cpu_delta(self):
        telemetry = self.make_fast()
        self.add_process(123, 100, utime=10)
        with mock.patch.object(fast_telemetry.time, 'monotonic', side_effect=[0.0, 1.0, 2.0]):
            telemetry.get_procs()
            self.write_proc(os.path.join('123', 'stat'), self.process_stat(123, 200, utime=100))
            process = telemetry.get_procs()[0]
        self.assertEqual(process['cpu_percent'], 0.0)

    def test_psutil_process_fallback_has_complete_safe_schema(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._fast = mock.Mock()
        collector._fast.get_procs.return_value = []
        collector.get_mem = mock.Mock(return_value={
            'mem': SimpleNamespace(total=1024),
        })
        process = SimpleNamespace(
            info={
                'pid': 123,
                'ppid': 1,
                'name': 'worker',
                'username': None,
                'cpu_percent': 2.5,
                'memory_percent': 1.5,
                'cmdline': ['python', '--worker'],
                'memory_info': SimpleNamespace(rss=4096),
            },
            uids=mock.Mock(return_value=SimpleNamespace(real=1000)),
        )
        with mock.patch.object(data.psutil, 'process_iter', return_value=[process]):
            result = collector.get_procs(tree=False)
        self.assertEqual(
            {key: result[0].get(key) for key in (
                'pid', 'ppid', 'name', 'uid', 'start_time', 'username', 'cmdline',
                'cpu_percent', 'memory_percent', 'rss',
            )},
            {
                'pid': 123, 'ppid': 1, 'name': 'worker', 'uid': 1000,
                'start_time': None, 'username': '1000', 'cmdline': 'python --worker',
                'cpu_percent': 2.5, 'memory_percent': 1.5, 'rss': 4096,
            },
        )

    def test_network_rate_uses_monotonic_and_resets_counter_decreases(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._interface_index = lambda name: {'eth0': 2}[name]
        collector._interface_addresses = lambda: {'eth0': '00:11:22:33:44:55'}
        collector.last_time = 10.0
        collector._last_net_by_interface = {
            ('eth0', 2, '00:11:22:33:44:55'):
                SimpleNamespace(bytes_recv=100, bytes_sent=200),
        }
        current = {'eth0': SimpleNamespace(bytes_recv=200, bytes_sent=300)}
        with mock.patch.object(data.time, 'monotonic', return_value=12.0), \
             mock.patch.object(data.psutil, 'net_io_counters', return_value=current):
            net = collector.get_net()
        self.assertEqual(net['down'], 50.0)
        self.assertEqual(net['up'], 50.0)

        reset = {'eth0': SimpleNamespace(bytes_recv=150, bytes_sent=250)}
        with mock.patch.object(data.time, 'monotonic', return_value=14.0), \
             mock.patch.object(data.psutil, 'net_io_counters', return_value=reset):
            net = collector.get_net()
        self.assertEqual(net['down'], 0)
        self.assertEqual(net['up'], 0)

    def test_network_rates_follow_interface_identity_across_topology_changes(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._interface_index = lambda name: {
            'eth0': 2, 'removed0': 3, 'new0': 4,
        }[name]
        collector._interface_addresses = lambda: {
            'eth0': '00:11:22:33:44:55',
            'removed0': '00:11:22:33:44:66',
            'new0': '00:11:22:33:44:77',
        }
        collector.last_time = 10.0
        collector._last_net_by_interface = {
            ('eth0', 2, '00:11:22:33:44:55'):
                SimpleNamespace(bytes_recv=100, bytes_sent=200),
            ('removed0', 3, '00:11:22:33:44:66'):
                SimpleNamespace(bytes_recv=900000, bytes_sent=800000),
        }
        current = {
            'eth0': SimpleNamespace(bytes_recv=120, bytes_sent=240),
            'new0': SimpleNamespace(bytes_recv=500000, bytes_sent=600000),
        }
        with mock.patch.object(data.time, 'monotonic', return_value=12.0), \
             mock.patch.object(data.psutil, 'net_io_counters', return_value=current):
            net = collector.get_net()

        self.assertEqual((net['down'], net['up']), (10.0, 20.0))
        self.assertEqual((net['total_down'], net['total_up']), (500120, 600240))
        self.assertEqual(net['interfaces'], [
            {'name': 'eth0', 'down': 10.0, 'up': 20.0,
             'total_down': 120, 'total_up': 240, 'is_up': None,
             'speed_mbps': None, 'mtu': None},
            {'name': 'new0', 'down': 0, 'up': 0,
             'total_down': 500000, 'total_up': 600000, 'is_up': None,
             'speed_mbps': None, 'mtu': None},
        ])

        reset = {'eth0': SimpleNamespace(bytes_recv=5, bytes_sent=7)}
        with mock.patch.object(data.time, 'monotonic', return_value=14.0), \
             mock.patch.object(data.psutil, 'net_io_counters', return_value=reset):
            net = collector.get_net()
        self.assertEqual((net['down'], net['up']), (0, 0))
        self.assertEqual(net['interfaces'][0]['name'], 'eth0')

    def test_recreated_interface_with_same_name_warms_at_zero(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        ifindices = {'eth0': 2}
        collector._interface_index = lambda name: ifindices[name]
        collector._interface_addresses = lambda: {}
        collector.last_time = 0.0
        collector._last_net_by_interface = {}
        initial = {'eth0': SimpleNamespace(bytes_recv=100, bytes_sent=200)}
        recreated = {'eth0': SimpleNamespace(bytes_recv=150, bytes_sent=260)}
        with mock.patch.object(data.time, 'monotonic', side_effect=[1.0, 2.0]), \
             mock.patch.object(data.psutil, 'net_io_counters', side_effect=[initial, recreated]):
            collector.get_net()
            ifindices['eth0'] = 9
            net = collector.get_net()
        self.assertEqual((net['down'], net['up']), (0, 0))

    def test_network_collection_does_not_read_interface_addresses(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._interface_index = lambda _name: 2
        collector.last_time = 0.0
        collector._last_net_by_interface = {}
        initial = {'eth0': SimpleNamespace(bytes_recv=100, bytes_sent=200)}
        with mock.patch.object(data.time, 'monotonic', return_value=1.0), \
             mock.patch.object(data.psutil, 'net_io_counters', return_value=initial), \
             mock.patch.object(data.psutil, 'net_if_stats', return_value={}), \
             mock.patch.object(data.psutil, 'net_if_addrs', side_effect=AssertionError('private')):
            net = collector.get_net()
        self.assertEqual(net['interfaces'][0]['name'], 'eth0')

    def test_recreated_interface_with_reused_name_and_index_warms_at_zero(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._interface_index = lambda _name: 2
        link_address = {'value': '00:11:22:33:44:55'}
        collector._interface_addresses = lambda: {
            'eth0': [SimpleNamespace(family=data.psutil.AF_LINK,
                                     address=link_address['value'])],
        }
        collector.last_time = 0.0
        collector._last_net_by_interface = {}
        samples = [
            {'eth0': SimpleNamespace(bytes_recv=100, bytes_sent=200)},
            {'eth0': SimpleNamespace(bytes_recv=500, bytes_sent=700)},
        ]
        with mock.patch.object(data.time, 'monotonic', side_effect=[1.0, 2.0]), \
                mock.patch.object(data.psutil, 'net_io_counters', side_effect=samples), \
                mock.patch.object(data.psutil, 'net_if_stats', return_value={}):
            collector.get_net()
            link_address['value'] = '66:77:88:99:aa:bb'
            net = collector.get_net()
        self.assertEqual((net['down'], net['up']), (0, 0))

    def test_mac_derived_interface_name_is_redacted(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._interface_index = lambda _name: 2
        collector._interface_addresses = lambda: {}
        collector.last_time = 0.0
        collector._last_net_by_interface = {}
        raw_name = 'enx001122aabbcc'
        counters = {raw_name: SimpleNamespace(bytes_recv=100, bytes_sent=200)}
        with mock.patch.object(data.time, 'monotonic', return_value=1.0), \
                mock.patch.object(data.psutil, 'net_io_counters', return_value=counters), \
                mock.patch.object(data.psutil, 'net_if_stats', return_value={}):
            net = collector.get_net()
        self.assertEqual(net['interfaces'][0]['name'], 'Ethernet')
        self.assertNotIn('001122aabbcc', repr(net))

    def test_mac_derived_wwan_interface_name_is_redacted(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._interface_index = lambda _name: 2
        collector._interface_addresses = lambda: {}
        collector.last_time = 0.0
        collector._last_net_by_interface = {}
        raw_name = 'wwxAABBCCDDEEFF'
        counters = {raw_name: SimpleNamespace(bytes_recv=100, bytes_sent=200)}
        with mock.patch.object(data.time, 'monotonic', return_value=1.0), \
                mock.patch.object(data.psutil, 'net_io_counters', return_value=counters), \
                mock.patch.object(data.psutil, 'net_if_stats', return_value={}):
            net = collector.get_net()
        self.assertEqual(net['interfaces'][0]['name'], 'WWAN')
        self.assertNotIn('AABBCCDDEEFF', repr(net))

    def test_missing_link_identity_never_inherits_network_rate(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._interface_index = lambda _name: 2
        collector._interface_addresses = lambda: {}
        collector.last_time = 10.0
        collector._last_net_by_interface = {
            ('eth0', 2, None): SimpleNamespace(bytes_recv=100, bytes_sent=200),
        }
        current = {'eth0': SimpleNamespace(bytes_recv=500, bytes_sent=700)}
        with mock.patch.object(data.time, 'monotonic', return_value=12.0), \
                mock.patch.object(data.psutil, 'net_io_counters', return_value=current), \
                mock.patch.object(data.psutil, 'net_if_stats', return_value={}):
            net = collector.get_net()
        self.assertEqual((net['down'], net['up']), (0, 0))

    def test_interface_identity_change_during_counter_read_discards_sample(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._interface_index = lambda _name: 2
        addresses = iter((
            {'eth0': '00:11:22:33:44:55'},
            {'eth0': '66:77:88:99:aa:bb'},
        ))
        collector._interface_addresses = lambda: next(addresses)
        collector.last_time = 10.0
        collector._last_net_by_interface = {
            ('eth0', 2, '00:11:22:33:44:55'):
                SimpleNamespace(bytes_recv=100, bytes_sent=200),
        }
        current = {'eth0': SimpleNamespace(bytes_recv=500, bytes_sent=700)}
        with mock.patch.object(data.time, 'monotonic', return_value=12.0), \
                mock.patch.object(data.psutil, 'net_io_counters', return_value=current), \
                mock.patch.object(data.psutil, 'net_if_stats', return_value={}):
            net = collector.get_net()
        self.assertEqual((net['down'], net['up']), (0, 0))

    def test_ifindex_change_during_counter_read_does_not_poison_next_sample(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        phase = {'new_interface': False}
        collector._interface_index = lambda _name: 9 if phase['new_interface'] else 2
        collector._interface_addresses = lambda: {'eth0': '00:11:22:33:44:55'}
        collector.last_time = 10.0
        collector._last_net_by_interface = {
            ('eth0', 2, '00:11:22:33:44:55'):
                SimpleNamespace(bytes_recv=100, bytes_sent=200),
        }
        samples = iter((
            {'eth0': SimpleNamespace(bytes_recv=500, bytes_sent=700)},
            {'eth0': SimpleNamespace(bytes_recv=600, bytes_sent=800)},
        ))

        def counters(**_kwargs):
            phase['new_interface'] = True
            return next(samples)

        with mock.patch.object(data.time, 'monotonic', side_effect=[12.0, 14.0]), \
                mock.patch.object(data.psutil, 'net_io_counters', side_effect=counters), \
                mock.patch.object(data.psutil, 'net_if_stats', return_value={}):
            collector.get_net()
            net = collector.get_net()
        self.assertEqual((net['down'], net['up']), (0, 0))

    def test_diskstats_selects_root_physical_devices_from_sysfs_topology(self):
        self.write_sys(os.path.join('class', 'block', 'sda1', 'partition'), '1\n')
        os.makedirs(os.path.join(self.sys, 'class', 'block', 'sda', 'slaves'))
        os.makedirs(os.path.join(self.sys, 'class', 'block', 'sda1', 'slaves'))
        self.write_sys(os.path.join('class', 'block', 'dm-0', 'slaves', 'sda1'), '')
        selected = fast_telemetry.select_root_block_devices(
            ['sda', 'sda1', 'dm-0'], self.sys,
        )
        self.assertEqual(selected, ['sda'])

    def test_partially_unreadable_sysfs_topology_fails_closed_and_resets_baselines(self):
        for device in ('sda', 'sdb'):
            os.makedirs(os.path.join(self.sys, 'class', 'block', device, 'slaves'))
            self.write_sys(os.path.join('class', 'block', device, 'diskseq'),
                           {'sda': '10\n', 'sdb': '11\n'}[device])
        unreadable = os.path.join(self.sys, 'class', 'block', 'sdb', 'slaves')
        self.write_proc('diskstats',
                        '8 0 sda 0 0 100 0 0 0 200 0 0 0 0\n'
                        '8 16 sdb 0 0 300 0 0 0 400 0 0 0 0\n')
        telemetry = self.make_fast()
        with mock.patch.object(fast_telemetry.time, 'monotonic', return_value=1.0):
            telemetry.get_disk_io()
        self.write_proc('diskstats',
                        '8 0 sda 0 0 120 0 0 0 240 0 0 0 0\n'
                        '8 16 sdb 0 0 330 0 0 0 450 0 0 0 0\n')
        real_listdir = os.listdir

        def listdir(path):
            if os.fspath(path) == unreadable:
                raise OSError('topology unavailable')
            return real_listdir(path)

        with mock.patch.object(fast_telemetry.os, 'listdir', side_effect=listdir), \
             mock.patch.object(fast_telemetry.time, 'monotonic', return_value=3.0):
            rates = telemetry.get_disk_io()
        self.assertEqual(rates, (0, 0))
        self.assertEqual(telemetry._prev_disk_rw, {})

    def test_diskstats_ignores_malformed_lines(self):
        os.makedirs(os.path.join(self.sys, 'class', 'block', 'sda', 'slaves'))
        self.write_sys(os.path.join('class', 'block', 'sda', 'diskseq'), '10\n')
        self.write_proc('diskstats',
                        'not diskstats\n'
                        '8 0 sda 0 bad 0 0 0 0 0 0 0 0 0\n'
                        '8 0 sda 0 0 4 0 0 0 6 0 0 0 0\n')
        telemetry = self.make_fast()
        self.assertEqual(telemetry.get_disk_io(), (0, 0))

    def test_disk_rates_follow_device_identity_across_hotplug(self):
        for device in ('sda', 'sdb', 'sdc'):
            os.makedirs(os.path.join(self.sys, 'class', 'block', device, 'slaves'))
            self.write_sys(os.path.join('class', 'block', device, 'diskseq'),
                           {'sda': '10\n', 'sdb': '11\n', 'sdc': '12\n'}[device])
        self.write_proc('diskstats',
                        '8 0 sda 0 0 100 0 0 0 200 0 0 0 0\n'
                        '8 16 sdb 0 0 10000 0 0 0 20000 0 0 0 0\n')
        telemetry = self.make_fast()
        with mock.patch.object(fast_telemetry.time, 'monotonic', side_effect=[1.0, 3.0]):
            telemetry.get_disk_io()
            self.write_proc('diskstats',
                            '8 0 sda 0 0 120 0 0 0 240 0 0 0 0\n'
                            '8 32 sdc 0 0 5000 0 0 0 6000 0 0 0 0\n')
            rates = telemetry.get_disk_io()
        self.assertEqual(rates, (5120.0, 10240.0))

    def test_replaced_disk_with_same_name_and_new_device_number_warms_at_zero(self):
        os.makedirs(os.path.join(self.sys, 'class', 'block', 'sda', 'slaves'))
        self.write_sys(os.path.join('class', 'block', 'sda', 'diskseq'), '10\n')
        self.write_proc('diskstats', '8 0 sda 0 0 100 0 0 0 200 0 0 0 0\n')
        telemetry = self.make_fast()
        with mock.patch.object(fast_telemetry.time, 'monotonic', side_effect=[1.0, 3.0]):
            telemetry.get_disk_io()
            self.write_proc('diskstats', '65 0 sda 0 0 500 0 0 0 700 0 0 0 0\n')
            rates = telemetry.get_disk_io()
        self.assertEqual(rates, (0, 0))

    def test_replaced_disk_with_reused_device_number_uses_disk_sequence(self):
        os.makedirs(os.path.join(self.sys, 'class', 'block', 'sda', 'slaves'))
        self.write_sys(os.path.join('class', 'block', 'sda', 'diskseq'), '10\n')
        self.write_proc('diskstats', '8 0 sda 0 0 100 0 0 0 200 0 0 0 0\n')
        telemetry = self.make_fast()
        with mock.patch.object(fast_telemetry.time, 'monotonic', side_effect=[1.0, 3.0]):
            telemetry.get_disk_io()
            self.write_sys(os.path.join('class', 'block', 'sda', 'diskseq'), '11\n')
            self.write_proc('diskstats', '8 0 sda 0 0 500 0 0 0 700 0 0 0 0\n')
            rates = telemetry.get_disk_io()
        self.assertEqual(rates, (0, 0))

    def test_missing_disk_sequence_never_inherits_disk_rate(self):
        os.makedirs(os.path.join(self.sys, 'class', 'block', 'sda', 'slaves'))
        self.write_proc('diskstats', '8 0 sda 0 0 100 0 0 0 200 0 0 0 0\n')
        telemetry = self.make_fast()
        with mock.patch.object(fast_telemetry.time, 'monotonic', side_effect=[1.0, 3.0]):
            telemetry.get_disk_io()
            self.write_proc('diskstats', '8 0 sda 0 0 500 0 0 0 700 0 0 0 0\n')
            rates = telemetry.get_disk_io()
        self.assertEqual(rates, (0, 0))

    def test_disk_identity_change_during_counter_read_discards_sample(self):
        os.makedirs(os.path.join(self.sys, 'class', 'block', 'sda', 'slaves'))
        diskseq = os.path.join('class', 'block', 'sda', 'diskseq')
        self.write_sys(diskseq, '10\n')
        self.write_proc('diskstats', '8 0 sda 0 0 100 0 0 0 200 0 0 0 0\n')
        telemetry = self.make_fast()
        real_open = open
        diskstats_path = os.path.join(self.proc, 'diskstats')

        with mock.patch.object(fast_telemetry.time, 'monotonic', return_value=1.0):
            telemetry.get_disk_io()
        self.write_proc('diskstats', '8 0 sda 0 0 500 0 0 0 700 0 0 0 0\n')

        def racing_open(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            if os.fspath(path) == diskstats_path:
                self.write_sys(diskseq, '11\n')
            return stream

        with mock.patch('builtins.open', side_effect=racing_open), \
                mock.patch.object(fast_telemetry.time, 'monotonic', return_value=2.0):
            telemetry.get_disk_io()
        self.write_proc('diskstats', '8 0 sda 0 0 700 0 0 0 900 0 0 0 0\n')
        with mock.patch.object(fast_telemetry.time, 'monotonic', return_value=3.0):
            rates = telemetry.get_disk_io()
        self.assertEqual(rates, (0, 0))

    def test_disk_inventory_refreshes_after_cache_interval(self):
        self.write_sys(os.path.join('class', 'block', 'sda', 'size'), '100\n')
        collector = data.DataCollector.__new__(data.DataCollector)
        with mock.patch.object(data, 'SYS_PATH', self.sys), \
                mock.patch.object(data.time, 'monotonic', side_effect=[1.0, 7.0]):
            first = collector._get_disk_devices()
            self.write_sys(os.path.join('class', 'block', 'sdb', 'size'), '200\n')
            second = collector._get_disk_devices()
        self.assertEqual([device['name'] for device in first], ['sda'])
        self.assertEqual([device['name'] for device in second], ['sda', 'sdb'])

    def test_drm_inventory_refreshes_after_cache_interval(self):
        self.write_sys(os.path.join('class', 'drm', 'card0', 'device', 'vendor'), '0x8086\n')
        collector = data.DataCollector.__new__(data.DataCollector)
        with mock.patch.object(data, 'SYS_PATH', self.sys), \
                mock.patch.object(data.time, 'monotonic', side_effect=[1.0, 7.0]):
            first = collector._get_drm_devices()
            self.write_sys(os.path.join('class', 'drm', 'card1', 'device', 'vendor'), '0x10de\n')
            second = collector._get_drm_devices()
        self.assertEqual([device[0] for device in first], ['card0'])
        self.assertEqual([device[0] for device in second], ['card0', 'card1'])

    def test_disk_rate_identity_does_not_read_serial_or_wwid(self):
        os.makedirs(os.path.join(self.sys, 'class', 'block', 'sda', 'slaves'))
        self.write_sys(os.path.join('class', 'block', 'sda', 'diskseq'), '10\n')
        self.write_sys(os.path.join('class', 'block', 'sda', 'device', 'serial'), 'private\n')
        self.write_sys(os.path.join('class', 'block', 'sda', 'wwid'), 'private\n')
        self.write_proc('diskstats', '8 0 sda 0 0 100 0 0 0 200 0 0 0 0\n')
        telemetry = self.make_fast()
        real_open = open

        def privacy_open(path, *args, **kwargs):
            if os.path.basename(os.fspath(path)) in {'serial', 'wwid'}:
                raise AssertionError(f'private identifier read: {path}')
            return real_open(path, *args, **kwargs)

        with mock.patch.object(fast_telemetry.time, 'monotonic', return_value=1.0), \
                mock.patch('builtins.open', side_effect=privacy_open):
            self.assertEqual(telemetry.get_disk_io(), (0, 0))

    def test_missing_sysfs_topology_fails_closed_and_resets_baselines(self):
        os.makedirs(os.path.join(self.sys, 'class', 'block', 'sda', 'slaves'))
        self.write_sys(os.path.join('class', 'block', 'sda', 'diskseq'), '10\n')
        self.write_proc('diskstats',
                        '8 0 sda 0 0 100 0 0 0 200 0 0 0 0\n')
        telemetry = self.make_fast()
        with mock.patch.object(fast_telemetry.time, 'monotonic', side_effect=[1.0, 3.0]):
            telemetry.get_disk_io()
            os.rmdir(os.path.join(self.sys, 'class', 'block', 'sda', 'slaves'))
            os.remove(os.path.join(self.sys, 'class', 'block', 'sda', 'diskseq'))
            os.rmdir(os.path.join(self.sys, 'class', 'block', 'sda'))
            os.rmdir(os.path.join(self.sys, 'class', 'block'))
            self.write_proc('diskstats',
                            '8 0 sda 0 0 120 0 0 0 240 0 0 0 0\n')
            rates = telemetry.get_disk_io()
        self.assertEqual(rates, (0, 0))
        self.assertEqual(telemetry._prev_disk_rw, {})

    def test_memfree_is_exposed_as_memory_free(self):
        self.write_proc('meminfo', 'MemTotal:       1024 kB\nMemAvailable:    512 kB\nMemFree:         256 kB\n')
        telemetry = self.make_fast()
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._fast = telemetry
        memory = collector.get_mem()['mem']
        self.assertEqual(memory.free, 256 * 1024)

    def test_amd_gpu_uses_host_sys_root(self):
        self.write_sys(os.path.join('class', 'drm', 'card0', 'device', 'vendor'), '0x1002\n')
        self.write_sys(os.path.join('class', 'drm', 'card0', 'device', 'gpu_busy_percent'), '37\n')
        collector = data.DataCollector.__new__(data.DataCollector)
        with mock.patch.object(data, 'SYS_PATH', self.sys, create=True):
            gpu = collector.get_gpu()
        self.assertEqual(gpu[0]['name'], 'AMD GPU')
        self.assertEqual(gpu[0]['load'], 37.0)

    def test_intel_gpu_reads_unprivileged_i915_frequency(self):
        self.write_sys(os.path.join('class', 'drm', 'card0', 'device', 'vendor'), '0x8086\n')
        self.write_sys(os.path.join('class', 'drm', 'card0', 'gt_cur_freq_mhz'), '850\n')
        collector = data.DataCollector.__new__(data.DataCollector)
        with mock.patch.object(data, 'SYS_PATH', self.sys):
            gpu = collector._get_drm_gpus()[0]
        self.assertEqual(gpu['vendor'], 'Intel')
        self.assertIsNone(gpu['load'])
        self.assertEqual(gpu['clock_graphics_mhz'], 850.0)
        self.assertEqual(gpu['status'], 'frequency only')

    def test_disk_partition_reports_psutil_free_bytes(self):
        collector = data.DataCollector.__new__(data.DataCollector)
        collector._fast = mock.Mock()
        collector._fast.get_disk_io.return_value = (0, 0)
        usage = SimpleNamespace(total=100, used=40, free=60, percent=40.0)
        partition = SimpleNamespace(device='/dev/test', mountpoint='/test')
        with mock.patch.object(data.psutil, 'disk_partitions', return_value=[partition]), \
             mock.patch.object(data.psutil, 'disk_usage', return_value=usage):
            result = collector.get_disk()
        self.assertEqual(result['partitions'][0]['free'], 60)

    def test_invalid_hwmon_entry_does_not_stop_later_package_scan(self):
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'name'), 'coretemp\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp1_label'), 'Package id 0\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon0', 'temp1_input'), 'not-a-number\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon1', 'name'), 'coretemp\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon1', 'temp1_label'), 'Package id 1\n')
        self.write_sys(os.path.join('class', 'hwmon', 'hwmon1', 'temp1_input'), '55000\n')
        info = self.make_fast().get_sys_info()
        self.assertEqual(info['cpu_temp'], 55.0)


if __name__ == '__main__':
    unittest.main()
