"""Run with python -m unittest -v test_ui."""

import asyncio
from copy import deepcopy
import os
import signal
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Footer

from cooler_btop.data import DataCollector, build_process_tree, prepare_processes
from cooler_btop.main import BtopCloneApp
from cooler_btop.ui import HelpModal, ProcDetailsModal, TerminateModal


def sample_snapshot():
    gib = 1024 ** 3
    return {
        'sys': {
            'hostname': 'workstation', 'kernel': 'Linux 6.12.0', 'uptime': '2d 04h 12m',
            'load_avg': '0.32, 0.48, 0.56', 'procs_running': 4, 'cpu_temp': 47,
            'battery': '84% (charging)', 'open_fds': 1280, 'max_fds': 1048576,
            'specs': {'os': 'Nobara Linux 44', 'architecture': 'x86_64',
                      'chassis': 'Notebook', 'vendor': 'ASUS', 'product_model': 'ROG Test',
                      'product_version': '1.0', 'bios_version': '999', 'bios_date': '2026-01-02',
                      'cpu_model': 'Intel Test CPU', 'logical_cores': 16, 'physical_cores': 8},
            'sensors': [
                {'label': 'CPU Package', 'category': 'CPU', 'type': 'temperature',
                 'value': 47.0, 'unit': 'C'},
                {'label': 'CPU fan', 'category': 'Fan', 'type': 'fan',
                 'value': 3200.0, 'unit': 'RPM'},
            ],
            'battery_info': [{'name': 'BAT0', 'percentage': 84.0, 'status': 'Charging',
                              'power_w': None, 'energy_now_wh': 50.0, 'energy_full_wh': 60.0,
                              'voltage_v': 12.0, 'time_remaining_s': None}],
        },
        'cpu': {'total': 32.4, 'per_core': [10.0, 24.0, 56.0, 72.0] * 4, 'freq': 3600},
        'mem': {
            'mem': SimpleNamespace(total=32 * gib, used=12 * gib, available=20 * gib,
                                   percent=37.5, buffers=gib / 2, cached=4 * gib),
            'swap': SimpleNamespace(total=8 * gib, used=gib / 2, percent=6.25),
        },
        'gpu': [{'name': 'Example Graphics', 'vendor': 'NVIDIA', 'load': 18.4,
                  'mem_used': 1024, 'mem_total': 8192, 'mem_pct': 12.5,
                  'temperature': 55.0, 'power_w': None, 'fan_pct': 0.0,
                  'clock_graphics_mhz': 210.0, 'clock_memory_mhz': None,
                  'status': 'available'}],
        'net': {'down': 2.4 * 1024 ** 2, 'up': 125 * 1024, 'total_down': 14 * gib,
                 'total_up': 2 * gib, 'interfaces': [
                     {'name': 'wlan0', 'down': 2.4 * 1024 ** 2, 'up': 125 * 1024,
                      'total_down': 14 * gib, 'total_up': 2 * gib, 'is_up': True,
                      'speed_mbps': 866, 'mtu': 1500},
                 ]},
        'disk': {'io': {'read_bytes': 1024 ** 2, 'write_bytes': 32768}, 'partitions': [
            {'mount': '/', 'filesystem': 'btrfs', 'total': 512 * gib, 'used': 192 * gib,
             'free': 320 * gib, 'percent': 37.5},
            {'mount': '/home', 'filesystem': 'btrfs', 'total': 1024 * gib, 'used': 512 * gib,
             'free': 512 * gib, 'percent': 50.0},
        ], 'devices': [{'name': 'nvme0n1', 'model': 'Fast NVMe', 'vendor': 'Example',
                        'type': 'NVMe SSD', 'capacity': 1024 * gib}],
                 'zram': [{'name': 'zram0', 'original_bytes': 2 * gib,
                           'compressed_bytes': gib, 'memory_bytes': gib,
                           'limit_bytes': 8 * gib}]},
        'conns': [{'proto': 'TCP', 'port': 22}, {'proto': 'TCP6', 'port': 22},
                  {'proto': 'TCP', 'port': 8080}],
        'procs': [
            {'pid': 101, 'ppid': 1, 'name': 'editor', 'cmdline': 'editor project',
              'uid': os.getuid(), 'start_time': 1101, 'username': 'alex',
              'cpu_percent': 20.0, 'memory_percent': 2.0},
            {'pid': 202, 'ppid': 1, 'name': 'browser', 'cmdline': 'browser --profile work',
              'uid': os.getuid(), 'start_time': 1202, 'username': 'alex',
              'cpu_percent': 3.0, 'memory_percent': 12.0},
            {'pid': 303, 'ppid': 1, 'name': 'compiler', 'cmdline': 'compiler main.c',
              'uid': os.getuid(), 'start_time': 1303, 'username': 'build',
              'cpu_percent': 50.0, 'memory_percent': 3.0},
            {'pid': 404, 'ppid': 101, 'name': 'language-server', 'cmdline': 'language-server',
              'uid': os.getuid(), 'start_time': 1404, 'username': 'alex',
              'cpu_percent': 2.0, 'memory_percent': 15.0},
        ],
    }


class SnapshotCollector:
    def __init__(self):
        self.snapshot = sample_snapshot()
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.started = threading.Event()
        self.release = None

    def get_sys_info(self):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        if self.release is not None and not self.release.wait(5):
            raise TimeoutError('Test did not release the sample')
        return deepcopy(self.snapshot['sys'])

    def get_cpu(self):
        return deepcopy(self.snapshot['cpu'])

    def get_mem(self):
        return deepcopy(self.snapshot['mem'])

    def get_gpu(self):
        return deepcopy(self.snapshot['gpu'])

    def get_disk(self):
        return deepcopy(self.snapshot['disk'])

    def get_net(self):
        return deepcopy(self.snapshot['net'])

    def get_connections(self):
        return deepcopy(self.snapshot['conns'])

    def get_procs(self, **kwargs):
        self.active -= 1
        return deepcopy(self.snapshot['procs'])


class ProcessOrderingTests(unittest.TestCase):
    def test_flat_sort_and_filter_do_not_mutate_snapshot(self):
        procs = sample_snapshot()['procs']
        original = deepcopy(procs)
        self.assertEqual([p['pid'] for p in prepare_processes(procs)], [303, 101, 202, 404])
        self.assertEqual([p['pid'] for p in prepare_processes(procs, 'memory_percent')], [404, 202, 303, 101])
        for query in ('  COMPILER  ', 'main.c', 'build', '303'):
            self.assertEqual([p['pid'] for p in prepare_processes(procs, filter_str=query)], [303])
        self.assertEqual(prepare_processes(procs, filter_str='no such process'), [])
        self.assertEqual(procs, original)

    def test_tree_branch_sort_and_indentation(self):
        procs = sample_snapshot()['procs']
        procs.extend([
            dict(procs[0], pid=505, ppid=101, name='helper', cpu_percent=1, memory_percent=1),
            dict(procs[0], pid=606, ppid=404, name='child', cpu_percent=1, memory_percent=1),
        ])
        original = deepcopy(procs)
        cpu = build_process_tree(procs)
        mem = build_process_tree(procs, 'memory_percent')
        self.assertEqual([p['pid'] for p in cpu], [303, 101, 404, 606, 505, 202])
        self.assertEqual([p['pid'] for p in mem], [101, 404, 606, 505, 202, 303])
        self.assertEqual([p['name'] for p in mem[:4]], [
            'editor', '\u251c\u2500 language-server', '\u2502  \u2514\u2500 child', '\u2514\u2500 helper',
        ])
        self.assertEqual(mem[0]['cumulative_memory'], 19)
        self.assertEqual(mem[0]['memory_percent'], 2)
        self.assertEqual(procs, original)
        filtered = prepare_processes(procs, tree=True, filter_str='language')
        self.assertEqual(filtered[0]['name'], 'language-server')

    def test_tree_handles_deep_chains_orphans_and_cycles(self):
        base = sample_snapshot()['procs'][0]
        procs = [dict(base, pid=pid, ppid=pid - 1) for pid in range(1, 1100)]
        procs += [dict(base, pid=2000, ppid=2001), dict(base, pid=2001, ppid=2000),
                  dict(base, pid=3000, ppid=3000)]
        tree = build_process_tree(procs)
        self.assertEqual(len(tree), len(procs))
        self.assertEqual(len({p['pid'] for p in tree}), len(procs))

    def test_collector_preserves_default_limit_and_can_return_all_for_ui(self):
        collector = DataCollector.__new__(DataCollector)
        collector.get_mem = Mock(return_value=sample_snapshot()['mem'])
        base = sample_snapshot()['procs'][0]
        procs = [dict(base, pid=pid, ppid=0, memory_percent=pid) for pid in range(1, 66)]
        collector._fast = Mock()
        collector._fast.get_procs.return_value = procs
        self.assertEqual(len(collector.get_procs()), 50)
        result = collector.get_procs(sort_by='memory_percent', tree=True, limit=None)
        self.assertEqual([p['pid'] for p in result], list(range(65, 0, -1)))


class TerminalPilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_footer_stays_compact_without_command_palette(self):
        app = self.make_app(show_pet=False)
        async with app.run_test(size=(80, 24)):
            footer = app.query_one(Footer)
            self.assertTrue(footer.compact)
            self.assertFalse(footer.show_command_palette)

    async def test_cpu_widget_labels_cores_with_collector_ids(self):
        app = self.make_app(show_pet=False)
        app.collector.snapshot['cpu'].update(
            per_core=[10.0, 20.0], core_ids=[2, 7],
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            rendered = app.cpu.body.render().plain
            self.assertIn('02  10.0%', rendered)
            self.assertIn('07  20.0%', rendered)

    async def test_hardware_details_render_compactly_and_missing_values_show_na(self):
        app = self.make_app(show_pet=False)
        app.collector.snapshot['gpu'][0]['load'] = None
        async with app.run_test(size=(80, 30)) as pilot:
            await self.settle(app, pilot)
            system = app.sysinfo.body.render().plain
            gpu = app.gpu.body.render().plain
            network = app.net.body.render().plain
            self.assertIn('Nobara Linux 44', system)
            self.assertIn('ROG Test', system)
            self.assertIn('CPU fan', system)
            self.assertIn('3200 RPM', system)
            self.assertIn('Load N/A', gpu)
            self.assertIn('Temp 55 C', gpu)
            self.assertIn('Power N/A', gpu)
            self.assertIn('wlan0 UP', network)
            self.assertIn('866 Mbps', network)
            columns = [column.label.plain for column in app.disk.dt.ordered_columns]
            self.assertIn('FS', columns)
            filesystem = app.disk.dt.get_cell('/', 'FS')
            self.assertEqual(getattr(filesystem, 'plain', filesystem), 'btrfs')
            self.assertIn('zram0', app.disk.io_static.render().plain)

    async def test_replay_start_and_resume_allow_a_full_frame_interval(self):
        app = self.make_app(show_pet=False)
        async with app.run_test(size=(100, 35)) as pilot:
            await self.settle(app, pilot)
            app.action_toggle_record()
            snapshot = sample_snapshot()
            for value in (10, 20, 30):
                snapshot['cpu']['total'] = value
                app._finish_sample(app._generation, snapshot, '')
            app.action_toggle_record()
            app.refresh_interval = 0.3
            # Put the live timer partway through its interval before replay starts.
            app._stats_timer = app.set_interval(0.3, app.update_stats)
            await asyncio.sleep(0.2)
            app.action_replay()
            await asyncio.sleep(0.18)
            self.assertEqual(app.cpu.cpu_data['total'], 10)
            await asyncio.sleep(0.2)
            self.assertEqual(app.cpu.cpu_data['total'], 20)
            app.action_toggle_pause()
            await asyncio.sleep(0.13)
            app.action_toggle_pause()
            await asyncio.sleep(0.18)
            self.assertEqual(app.cpu.cpu_data['total'], 20)
            await asyncio.sleep(0.2)
            self.assertEqual(app.cpu.cpu_data['total'], 30)

    async def test_replay_discards_inflight_collection_and_live_waits_for_fresh_sample(self):
        app = self.make_app()
        async with app.run_test(size=(100, 35)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('r')
            app.update_stats()
            await self.settle(app, pilot)
            await pilot.press('r')
            self.collector.release = threading.Event()
            self.collector.started.clear()
            self.collector.snapshot['cpu']['total'] = 99
            app.update_stats()
            self.assertTrue(await asyncio.to_thread(self.collector.started.wait, 2))
            try:
                await pilot.press('e')
                self.assertEqual(app.cpu.cpu_data['total'], 32.4)
                self.assertTrue(app.pet.paused)
                await pilot.press('l')
                with patch('cooler_btop.main.os.kill') as kill:
                    app._terminate_process(101, os.getuid(), 1101, True)
                    kill.assert_not_called()
                self.assertEqual(self.collector.max_active, 1)
            finally:
                self.collector.release.set()
            await self.settle(app, pilot)
            await self.settle(app, pilot)
            self.assertEqual(app.cpu.cpu_data['total'], 99)
            self.assertEqual(self.collector.max_active, 1)
            self.assertIn('LIVE', app.context.render().plain)
            await pilot.press('e')
            self.assertEqual(app.cpu.cpu_data['total'], 32.4)
            self.assertEqual(list(app.cpu.history), [32.4])

    async def test_capture_replay_steps_and_returns_to_live(self):
        app = self.make_app(show_pet=False)
        async with app.run_test(size=(100, 35)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('r')
            self.assertIn('REC', app.context.render().plain)
            for value in (11, 22, 33):
                self.collector.snapshot['cpu']['total'] = value
                self.collector.snapshot['net']['down'] = value * 1024
                app.update_stats()
                await self.settle(app, pilot)
            recorded_time = app.last_sample
            await pilot.press('r', 'e')
            self.assertIn('REPLAY', app.context.render().plain)
            self.assertEqual(app.cpu.cpu_data['total'], 11)
            self.assertEqual(list(app.cpu.history), [11])
            calls = self.collector.calls
            await pilot.press(']')
            self.assertEqual(app.cpu.cpu_data['total'], 22)
            self.assertEqual(list(app.cpu.history), [11, 22])
            self.assertEqual(list(app.net.down_hist), [11264, 22528])
            await pilot.press('[')
            self.assertEqual(list(app.cpu.history), [11])
            self.assertEqual(list(app.net.down_hist), [11264])
            await pilot.press('p')
            app.update_stats()
            self.assertEqual(app.cpu.cpu_data['total'], 22)
            app.update_stats()
            app.update_stats()
            self.assertEqual(app.cpu.cpu_data['total'], 33)
            self.assertEqual(app.last_sample, recorded_time)
            self.assertIn(recorded_time.strftime('%H:%M:%S'), app.context.render().plain)
            self.assertTrue(app.paused)
            self.assertEqual(self.collector.calls, calls)
            self.collector.snapshot['cpu']['total'] = 77
            await pilot.press('l')
            await self.settle(app, pilot)
            self.assertEqual(app.cpu.cpu_data['total'], 77)
            self.assertIn('LIVE', app.context.render().plain)
            self.assertEqual(list(app.cpu.history), [77])

    async def test_recording_is_bounded_and_replay_blocks_live_pid_actions(self):
        app = self.make_app(show_pet=False)
        app.capture_limit = 2
        async with app.run_test(size=(100, 35)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('e')
            self.assertIn('LIVE', app.context.render().plain)
            await pilot.press('r')
            snapshot = sample_snapshot()
            for value in (10, 20, 30):
                snapshot['cpu']['total'] = value
                app._finish_sample(app._generation, snapshot, '')
            await pilot.press('e')
            self.assertEqual(app.cpu.cpu_data['total'], 10)
            with patch('cooler_btop.main.os.kill') as kill:
                await pilot.press('k', 'enter')
                self.assertNotIsInstance(app.screen, (TerminateModal, ProcDetailsModal))
                app._terminate_process(101, os.getuid(), 1101, True)
                kill.assert_not_called()
            await pilot.press(']')
            self.assertEqual(app.cpu.cpu_data['total'], 20)
            await pilot.press(']')
            self.assertEqual(app.cpu.cpu_data['total'], 20)
            await pilot.press('r')
            self.assertIn('REPLAY', app.context.render().plain)

    def make_app(self, **kwargs):
        self.collector = SnapshotCollector()
        return BtopCloneApp(collector=self.collector, refresh_interval=3600, **kwargs)

    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()

    def row_pids(self, app):
        return [int(row.key.value) for row in app.proc.dt.ordered_rows]

    async def test_responsive_render_and_keyboard_reachability(self):
        app = self.make_app()
        self.collector.snapshot['procs'][0]['cmdline'] = 'a-long-command --with-arguments ' * 12
        async with app.run_test(size=(140, 45)) as pilot:
            await self.settle(app, pilot)
            for width, height in [(140, 45), (80, 24), (60, 24), (140, 45)]:
                with self.subTest(size=(width, height)):
                    await pilot.resize_terminal(width, height)
                    await pilot.pause()
                    dashboard = app.query_one('#dashboard')
                    self.assertEqual(dashboard.max_scroll_x, 0)
                    self.assertEqual(app.brand.region.y, 0)
                    self.assertEqual(app.context.region.y, 1)
                    self.assertEqual(dashboard.region.y, 2)
                    rendered = [strip.text for strip in app.screen._compositor.render_strips()]
                    self.assertIn('COOLER BTOP', rendered[0])
                    self.assertIn('LIVE', rendered[1])
                    self.assertEqual('sampled' in app.context.render().plain, width >= 110)
                    if width >= 80:
                        self.assertEqual(app.proc.dt.max_scroll_x, 0)
                    for panel in [app.cpu, app.mem, app.proc, app.sysinfo, app.net, app.disk, app.gpu, app.conns]:
                        self.assertGreater(panel.region.width, 0)
                        self.assertGreaterEqual(panel.region.x, 0)
                        self.assertLessEqual(panel.region.right, width)
                    for panel in (app.cpu, app.net):
                        graphs = [line for line in panel.body.render().plain.splitlines()
                                  if line and all('\u2800' <= char <= '\u28ff' for char in line)]
                        self.assertTrue(graphs)
                        self.assertTrue(all(len(line) <= panel.body.size.width for line in graphs))
                    targets = {app.cpu, app.mem, app.proc.dt, app.sysinfo, app.net, app.disk.dt, app.gpu, app.conns.dt}
                    reached = set()
                    app.proc.dt.focus()
                    for _ in range(8):
                        await pilot.press('tab')
                        reached.add(app.focused)
                        self.assertTrue(dashboard.region.overlaps(app.focused.region))
                    self.assertEqual(reached, targets)
                    self.assertEqual(app.pet.size.height, 1)
                    dashboard.scroll_end(animate=False)
                    await pilot.pause()
                    await pilot.press('g')
                    self.assertGreaterEqual(app.proc.region.y, dashboard.region.y)
                    self.assertLessEqual(app.proc.region.bottom, dashboard.region.bottom)
            self.assertEqual(self.collector.calls, 1)
            self.assertEqual(len(app.cpu.history), 1)

    async def test_filter_empty_clear_apply_and_restore_focus(self):
        app = self.make_app(show_pet=False)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            app.query_one('#dashboard').scroll_end(animate=False)
            await pilot.pause()
            await pilot.press('f')
            self.assertIs(app.focused, app.filter_input)
            self.assertLessEqual(app.filter_input.region.bottom, 23)
            await pilot.press(*'not-found')
            self.assertEqual(app.proc.dt.row_count, 0)
            self.assertIsNone(app.proc.selected_pid)
            self.assertTrue(app.proc.empty.display)
            self.assertIn('No processes match', app.proc.empty.render().plain)
            self.assertIn('No processes match', '\n'.join(strip.text for strip in app.screen._compositor.render_strips()))
            with patch('cooler_btop.main.os.kill') as kill:
                app.action_kill_proc()
                kill.assert_not_called()
                self.assertNotIsInstance(app.screen, TerminateModal)
            await pilot.press('escape')
            self.assertEqual(app.filter_input.value, '')
            self.assertEqual(app.proc_filter, '')
            self.assertFalse(app.filter_input.display)
            self.assertIs(app.focused, app.proc.dt)
            self.assertEqual(app.proc.dt.row_count, 4)
            await pilot.press('slash', *'browser', 'enter')
            self.assertEqual(self.row_pids(app), [202])
            self.assertIs(app.focused, app.proc.dt)
            self.assertFalse(app.filter_input.display)
            self.assertIn('filter: browser', app.context.render().plain)
            await pilot.press('f')
            self.assertEqual(app.filter_input.value, 'browser')
            await pilot.press('escape')
            self.assertEqual(self.row_pids(app), [303, 101, 202, 404])
            self.assertEqual(self.collector.calls, 1)

    async def test_sort_reorders_rows_retains_pid_and_updates_all_cells(self):
        app = self.make_app(show_pet=False)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            self.assertEqual(app.proc.selected_pid, 303)
            await pilot.click('#process-table', offset=(32, 0))
            self.assertEqual(app.sort_key, 'memory_percent')
            self.assertEqual(self.row_pids(app), [404, 202, 303, 101])
            self.assertEqual(app.proc.selected_pid, 303)
            await pilot.press('c', 'm')
            await pilot.press('t')
            self.assertEqual(self.row_pids(app), [101, 404, 202, 303])
            self.assertEqual(app.proc.selected_pid, 303)
            self.assertIn('TREE branches', app.context.render().plain)
            await pilot.press('t', 'c')
            changed = next(p for p in app._process_snapshot if p['pid'] == 303)
            changed.update(name='renamed', cmdline='new-command --flag', username='other', cpu_percent=0)
            app._refresh_processes()
            await pilot.pause()
            self.assertEqual(self.row_pids(app), [101, 202, 404, 303])
            self.assertEqual(app.proc.selected_pid, 303)
            self.assertEqual(app.proc.dt.get_cell('303', 'USER').plain, 'other')
            command = app.proc.dt.get_cell('303', 'COMMAND').plain
            self.assertIn('renamed', command)
            self.assertIn('new-command', command)
            changed.update(name='fresh', cmdline='newer --option')
            app._refresh_processes()
            self.assertIn('fresh | newer', app.proc.dt.get_cell('303', 'COMMAND').plain)
            app._process_snapshot = [p for p in app._process_snapshot if p['pid'] != 303]
            app._refresh_processes()
            self.assertEqual(app.proc.selected_pid, 404)
            app._process_snapshot = []
            app._refresh_processes()
            self.assertEqual(app.proc.dt.row_count, 0)
            self.assertEqual(app.proc._cell_cache, {})
            self.assertEqual(self.collector.calls, 1)

    async def test_pause_serializes_workers_and_discards_inflight_result(self):
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            history = list(app.cpu.history)
            self.collector.release = threading.Event()
            self.collector.started.clear()
            self.collector.snapshot['cpu']['total'] = 99
            app.update_stats()
            self.assertTrue(await asyncio.to_thread(self.collector.started.wait, 2))
            try:
                for _ in range(20):
                    app.update_stats()
                await pilot.press('f', *'rapid-filter', 'escape', 'm', 't', 'p')
                self.assertTrue(app.paused)
                self.assertIn('PAUSED', app.context.render().plain)
                self.assertEqual(self.collector.calls, 2)
                self.assertEqual(self.collector.max_active, 1)
            finally:
                self.collector.release.set()
            await self.settle(app, pilot)
            self.assertEqual(list(app.cpu.history), history)
            for _ in range(10):
                app.update_stats()
            await pilot.press('c')
            self.assertEqual(self.collector.calls, 2)
            self.assertTrue(app.pet.paused)
            await pilot.press('f', *'browser', 'enter')
            self.assertEqual(self.row_pids(app), [202])
            self.assertIn('PAUSED', app.context.render().plain)
            self.assertIn('FLAT (filtered tree)', app.context.render().plain)
            self.assertEqual(list(app.cpu.history), history)
            await pilot.press('escape', 'space')
            await self.settle(app, pilot)
            self.assertFalse(app.paused)
            self.assertEqual(app.cpu.history[-1], 99)
            self.assertEqual(self.collector.calls, 3)
            self.assertEqual(self.collector.max_active, 1)

    async def test_failed_sample_retains_snapshot_and_retries(self):
        app = self.make_app(show_pet=False)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            history = list(app.cpu.history)
            with patch.object(self.collector, 'get_cpu', side_effect=OSError('sample unavailable')):
                app.update_stats()
                await self.settle(app, pilot)
            self.assertFalse(app._sampling)
            self.assertEqual(list(app.cpu.history), history)
            self.assertEqual(self.row_pids(app), [303, 101, 202, 404])
            self.assertIn('RETRY OSError', app.context.render().plain)
            self.assertIn('sample unavailable', app.context.tooltip)
            app.update_stats()
            await self.settle(app, pilot)
            self.assertEqual(app.sample_error, '')
            self.assertIn('LIVE', app.context.render().plain)

    async def test_q_exits_application(self):
        app = self.make_app(show_pet=False)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            await pilot.press('q')
            self.assertFalse(app.is_running)

    async def test_empty_devices_clear_old_rows_and_multiple_gpus_remain_reachable(self):
        app = self.make_app(show_pet=False)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            self.assertEqual(app.conns.dt.row_count, 3)
            self.assertEqual(app.disk.dt.row_count, 2)
            app.conns.conns_data = []
            app.disk.disk_data = {'partitions': [], 'io': {}}
            app.gpu.gpu_data = []
            self.assertEqual(app.conns.dt.row_count, 0)
            self.assertEqual(app.disk.dt.row_count, 0)
            self.assertIn('No GPU telemetry', app.gpu.body.render().plain)
            gpu = sample_snapshot()['gpu'][0]
            app.gpu.gpu_data = [gpu, dict(gpu, name='Second Graphics')]
            app.gpu.focus()
            await pilot.pause()
            self.assertIn('Second Graphics', app.gpu.body.render().plain)
            self.assertGreater(app.gpu.max_scroll_y, 0)

    async def test_termination_defaults_to_cancel_and_requires_positive_pid(self):
        app = self.make_app(show_pet=False)
        with patch('cooler_btop.main.os.pidfd_open', return_value=55, create=True) as pidfd_open, \
                patch('cooler_btop.main.signal.pidfd_send_signal', create=True) as pidfd_send, \
                patch('cooler_btop.main.os.close'), \
                patch('cooler_btop.main._read_process_identity', return_value=(os.getuid(), 1303)):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.settle(app, pilot)
                await pilot.press('k')
                self.assertIsInstance(app.screen, TerminateModal)
                self.assertEqual(app.focused.id, 'cancel-terminate')
                pidfd_send.assert_not_called()
                await pilot.press('enter')
                await pilot.pause()
                self.assertNotIsInstance(app.screen, TerminateModal)
                self.assertIs(app.focused, app.proc.dt)
                pidfd_send.assert_not_called()
                await pilot.press('k', 'escape')
                self.assertIs(app.focused, app.proc.dt)
                pidfd_send.assert_not_called()
                await pilot.press('k', 'tab', 'enter')
                await pilot.pause()
                pidfd_open.assert_called_once_with(303)
                pidfd_send.assert_called_once_with(55, signal.SIGTERM)
                pidfd_send.reset_mock()
                app._terminate_process(0, os.getuid(), 1, True)
                app._terminate_process(-1, os.getuid(), 1, True)
                pidfd_send.assert_not_called()
                with self.assertRaises(ValueError):
                    TerminateModal(0, 'invalid')

    async def test_termination_revalidates_owner_and_start_time(self):
        app = self.make_app(show_pet=False)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(app, pilot)
            with patch('cooler_btop.main.os.pidfd_open', return_value=56, create=True) as pidfd_open, \
                    patch('cooler_btop.main.signal.pidfd_send_signal', create=True) as pidfd_send, \
                    patch('cooler_btop.main.os.close'):
                for identity in ((os.getuid() + 1, 1303), (os.getuid(), 9999), None):
                    with self.subTest(identity=identity), \
                            patch('cooler_btop.main._read_process_identity', return_value=identity):
                        app._terminate_process(303, os.getuid(), 1303, True)
                        pidfd_send.assert_not_called()
                pidfd_open.reset_mock()
                app._terminate_process(1, os.getuid(), 1, True)
                app._terminate_process(os.getpid(), os.getuid(), 1, True)
                pidfd_open.assert_not_called()
                pidfd_send.assert_not_called()

    async def test_help_and_details_escape_restore_focus(self):
        app = self.make_app(show_pet=False)
        process = Mock()
        process.oneshot.return_value.__enter__ = Mock()
        process.oneshot.return_value.__exit__ = Mock()
        process.name.return_value = '[red]literal name'
        process.cmdline.return_value = ['compiler', '--flag']
        process.create_time.return_value = 1700000000
        process.memory_percent.return_value = 2.0
        process.memory_info.return_value = SimpleNamespace(rss=1024 ** 2)
        process.net_connections.return_value = []
        with patch('cooler_btop.ui.psutil.Process', return_value=process):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.settle(app, pilot)
                await pilot.press('h')
                self.assertIsInstance(app.screen, HelpModal)
                await pilot.press('escape')
                self.assertIs(app.focused, app.proc.dt)
                await pilot.press('h', 'h')
                self.assertNotIsInstance(app.screen, HelpModal)
                await pilot.press('enter')
                await self.settle(app, pilot)
                self.assertIsInstance(app.screen, ProcDetailsModal)
                self.assertIn('[red]literal name', app.screen.details.render().plain)
                self.assertIn('Connections', app.screen.details.render().plain)
                await pilot.press('escape')
                self.assertIs(app.focused, app.proc.dt)


if __name__ == '__main__':
    unittest.main()
