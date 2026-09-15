import gzip
import http.client
import io
import json
import os
import sqlite3
import threading
import time
import unittest
from collections import namedtuple
from unittest import mock


import stress_test
from cooler_btop import server


class TestMetricsServer(unittest.TestCase):
    def setUp(self):
        self.collector = mock.Mock(spec=server.DataCollector)
        self.collector.get_cpu.return_value = {'total': 12.5, 'per_core': [10.0, 15.0], 'freq': 3200.0}
        memory = namedtuple('Memory', 'total available percent used free buffers cached')
        swap = namedtuple('Swap', 'total used free percent')
        self.collector.get_mem.return_value = {
            'mem': memory(8192, 6144, 25.0, 2048, 4096, 512, 1536),
            'swap': swap(4096, 1024, 3072, 25.0),
        }
        self.collector.get_gpu.return_value = [
            {'name': 'Test GPU', 'load': 2.0, 'mem_used': 32, 'mem_total': 1024, 'mem_pct': 3.125},
        ]
        self.collector.get_disk.return_value = {
            'partitions': [{'mount': '/', 'total': 16384, 'used': 8192, 'percent': 50.0}],
            'io': {'read_bytes': 1024.0, 'write_bytes': 512.0},
        }
        self.collector.get_net.return_value = {
            'down': 123.5, 'up': 45.0, 'total_down': 8192, 'total_up': 4096,
        }
        self.collector.get_procs.return_value = [
            {'pid': 1000 + i, 'ppid': 1, 'name': f'worker-{i}', 'username': 'tester',
             'cmdline': f'worker --id {i}', 'cpu_percent': float(50 - i), 'memory_percent': 0.25}
            for i in range(50)
        ]
        self.collector.get_connections.return_value = [{'port': 8080, 'proto': 'TCP'}]
        self.collector.get_sys_info.return_value = {
            'hostname': 'test-host', 'uptime': 1234.5, 'load_avg': (0.1, 0.2, 0.3),
        }
        kill_patcher = mock.patch('cooler_btop.server.os.kill')
        self.kill = kill_patcher.start()
        self.addCleanup(kill_patcher.stop)

        log_patcher = mock.patch.object(server.MetricsHandler, 'log_message')
        log_patcher.start()
        self.addCleanup(log_patcher.stop)

    def start_server(self, interval=60.0, logger=None, wait=True):
        kwargs = {} if interval is None else {'interval': interval}
        self.server = server.MetricsServer(
            ('127.0.0.1', 0), server.MetricsHandler, self.collector, logger, **kwargs,
        )
        self.server.socket_timeout = 0.5
        self.server.heartbeat_interval = 0.03
        self.http_thread = threading.Thread(
            target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True,
        )
        self.http_thread.start()
        self.addCleanup(self.close_server)
        if wait:
            self.wait_for(lambda: self.server._snapshot is not None)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.http_thread.join(timeout=2)
        self.assertFalse(self.http_thread.is_alive())
        self.assertFalse(self.server._sampler.is_alive())

    def wait_for(self, predicate):
        with self.server._condition:
            self.assertTrue(self.server._condition.wait_for(predicate, timeout=3))

    def request(self, path, method='GET', headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(method, path, headers=headers or {})
            with connection.getresponse() as response:
                return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def open_stream(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        self.addCleanup(connection.close)
        connection.request('GET', '/api/metrics/stream?client=test')
        response = connection.getresponse()
        self.addCleanup(response.close)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Content-Type'), 'text/event-stream')
        return response

    def read_frame(self, response):
        lines = []
        while True:
            line = response.readline()
            if not line:
                return b''.join(lines)
            lines.append(line)
            if line == b'\n':
                return b''.join(lines)

    def test_two_streams_leave_api_and_dashboard_usable_with_one_sampler(self):
        sampler_threads = []

        def get_cpu():
            sampler_threads.append(threading.current_thread())
            return self.collector.get_cpu.return_value

        self.collector.get_cpu.side_effect = get_cpu
        logger = mock.Mock(spec=server.DBLogger)
        self.start_server(logger=logger)
        streams = [self.open_stream(), self.open_stream()]
        frames = [self.read_frame(stream) for stream in streams]
        status, _, payload = self.request('/api/metrics?cache=test')
        self.assertEqual(status, 200)
        self.assertEqual(frames, [b'data: ' + payload + b'\n\n'] * 2)

        assets = {
            '/': ('index.html', 'text/html; charset=utf-8', b'<!doctype html><title>Local</title>'),
        }
        for route, (filename, content_type, content) in assets.items():
            with self.subTest(route=route):
                with mock.patch('cooler_btop.server.open', return_value=io.BytesIO(content), create=True) as opened:
                    status, headers, body = self.request(route + '?v=123')
                self.assertEqual(status, 200)
                self.assertEqual(headers['Content-Type'], content_type)
                self.assertEqual(int(headers['Content-Length']), len(content))
                self.assertEqual(body, content)
                opened.assert_called_once_with(
                    os.path.join(os.path.dirname(server.__file__), 'web', filename), 'rb',
                )

        for _ in range(3):
            self.assertEqual(self.request('/api/metrics')[2], payload)
        self.assertEqual(self.request('/api/kill/123?source=cli', method='POST')[0], 404)
        self.kill.assert_not_called()
        for stream in streams:
            self.assertEqual(self.read_frame(stream), b': heartbeat\n\n')
        self.assertEqual(sampler_threads, [self.server._sampler])
        for method in ('get_cpu', 'get_mem', 'get_gpu', 'get_disk', 'get_net',
                       'get_procs', 'get_connections', 'get_sys_info'):
            getattr(self.collector, method).assert_called_once()
        self.collector.get_procs.assert_called_once_with(tree=False)
        logger.log.assert_called_once_with(
            self.collector.get_cpu.return_value,
            json.loads(payload)['mem'],
            self.collector.get_net.return_value,
        )

    def test_snapshot_types_metadata_and_gzip(self):
        before = time.time()
        self.start_server(interval=17.5)
        status, headers, payload = self.request('/api/metrics')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/json')
        data = json.loads(payload)
        self.assertEqual(set(data), {'cpu', 'mem', 'gpu', 'disk', 'net', 'procs', 'conns',
                                     'sys', 'timestamp', 'interval'})
        self.assertIsInstance(data['timestamp'], float)
        self.assertGreaterEqual(data['timestamp'], before)
        self.assertLessEqual(data['timestamp'], time.time())
        self.assertIsInstance(data['interval'], float)
        self.assertEqual(data['interval'], 17.5)
        self.assertIsInstance(data['cpu']['total'], float)
        self.assertIsInstance(data['cpu']['per_core'], list)
        self.assertIsInstance(data['mem']['mem'], dict)
        self.assertIsInstance(data['mem']['mem']['used'], int)
        self.assertIsInstance(data['mem']['swap']['percent'], float)
        self.assertIsInstance(data['net']['down'], float)
        self.assertIsInstance(data['gpu'], list)
        self.assertIsInstance(data['disk']['partitions'], list)
        self.assertIsInstance(data['conns'][0]['port'], int)
        self.assertIsInstance(data['sys']['load_avg'], list)
        self.assertEqual(data['procs'], self.collector.get_procs.return_value)
        self.assertEqual(len(data['procs']), 50)
        self.assertIsNone(self.server.logger)
        status, headers, compressed = self.request(
            '/api/metrics?compressed=1', headers={'Accept-Encoding': 'deflate, gzip'},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        self.assertEqual(headers['Vary'], 'Accept-Encoding')
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(int(headers['Content-Length']), len(compressed))
        self.assertEqual(gzip.decompress(compressed), payload)

    def test_http_process_control_is_not_exposed(self):
        self.start_server()
        status, _, body = self.request('/api/kill/1000', method='POST')
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {
            'status': 'error', 'message': 'Not found',
        })
        self.kill.assert_not_called()

    def test_default_and_invalid_intervals(self):
        for interval in (0, -1, 'invalid', float('nan'), float('inf')):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                server.MetricsServer(('127.0.0.1', 0), server.MetricsHandler,
                                     self.collector, interval=interval)
        self.start_server(interval=None)
        self.assertEqual(json.loads(self.request('/api/metrics')[2])['interval'], 1.0)

    def test_asset_allowlist_and_missing_file_status(self):
        self.start_server()
        with mock.patch('cooler_btop.server.open', create=True) as opened:
            for path in ('/index.html', '/web/index.html', '/web/app.js', '/server.py',
                          '/../server.py', '/%2e%2e/server.py', '/styles.css', '/app.js',
                          '/styles.css/extra', '/missing'):
                with self.subTest(path=path):
                    self.assertEqual(self.request(path + '?v=1')[0], 404)
            opened.assert_not_called()
        with mock.patch('cooler_btop.server.open', side_effect=FileNotFoundError, create=True):
            self.assertEqual(self.request('/?v=1')[0], 404)
        with mock.patch('cooler_btop.server.open', side_effect=PermissionError, create=True):
            with self.assertLogs(level='ERROR'):
                self.assertEqual(self.request('/')[0], 500)

    def test_invalid_pids_never_call_kill(self):
        self.start_server()
        for pid in ('', '0', '-1', '-123', '+1', 'abc', '1.5', '1_000', '123/456',
                    '123/', '%20123', '%2b123', '2147483648', '9' * 5000):
            with self.subTest(pid=pid):
                status, _, body = self.request('/api/kill/' + pid, method='POST')
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(body)['status'], 'error')
        self.assertEqual(self.request('/api/kill/123')[0], 404)
        self.assertEqual(self.request('/api/unknown', method='POST')[0], 404)
        self.kill.assert_not_called()

    def test_sampler_failure_is_logged_not_replayed_and_recovers(self):
        allow_failure = threading.Event()
        recover = threading.Event()
        repeated_failure = threading.Event()
        failures = []

        def get_cpu():
            if self.collector.get_cpu.call_count > 1:
                allow_failure.wait(timeout=3)
                if not recover.is_set():
                    failures.append(time.monotonic())
                    if len(failures) >= 2:
                        repeated_failure.set()
                    raise RuntimeError('collector failed')
            return self.collector.get_cpu.return_value

        self.collector.get_cpu.side_effect = get_cpu
        with self.assertLogs(level='ERROR') as errors:
            self.start_server(interval=0.1)
            self.addCleanup(allow_failure.set)
            stream = self.open_stream()
            first = json.loads(self.read_frame(stream)[len(b'data: '):])
            allow_failure.set()
            self.wait_for(lambda: self.server._snapshot is None)
            status, headers, _ = self.request('/api/metrics')
            self.assertEqual(status, 503)
            self.assertEqual(headers['Retry-After'], '1')
            self.assertEqual(self.server._version, 1)
            self.assertEqual(self.read_frame(stream), b': heartbeat\n\n')
            new_stream = self.open_stream()
            self.assertEqual(self.read_frame(new_stream), b': heartbeat\n\n')
            self.assertTrue(repeated_failure.wait(timeout=2))
            self.assertGreaterEqual(failures[1] - failures[0], 0.09)
            recover.set()
            self.wait_for(lambda: self.server._version >= 2)
            for active_stream in (stream, new_stream):
                for _ in range(100):
                    frame = self.read_frame(active_stream)
                    if frame.startswith(b'data: '):
                        break
                else:
                    self.fail('Stream did not receive the recovered snapshot')
                self.assertGreater(json.loads(frame[len(b'data: '):])['timestamp'], first['timestamp'])
            self.assertEqual(self.request('/api/metrics')[0], 200)
            self.server.shutdown()
        self.assertTrue(any('Failed to sample metrics' in line for line in errors.output))

    def test_pending_first_sample_returns_503_then_publishes(self):
        release_collector = threading.Event()

        def get_cpu():
            release_collector.wait(timeout=3)
            return self.collector.get_cpu.return_value

        self.collector.get_cpu.side_effect = get_cpu
        self.start_server(wait=False)
        self.addCleanup(release_collector.set)
        self.assertEqual(self.request('/api/metrics')[0], 503)
        stream = self.open_stream()
        self.assertEqual(self.read_frame(stream), b': heartbeat\n\n')
        release_collector.set()
        self.wait_for(lambda: self.server._snapshot is not None)
        status, _, payload = self.request('/api/metrics')
        self.assertEqual(status, 200)
        self.assertEqual(self.read_frame(stream), b'data: ' + payload + b'\n\n')
        self.collector.get_cpu.assert_called_once()

    def test_database_logging_runs_without_clients(self):
        logger = server.DBLogger(':memory:')
        self.start_server(interval=0.02, logger=logger)
        self.wait_for(lambda: self.server._version >= 3)
        self.server.shutdown()
        rows = logger.conn.execute('SELECT * FROM metrics ORDER BY timestamp').fetchall()
        self.assertEqual(len(rows), self.server._version)
        self.assertGreaterEqual(len(rows), 3)
        for timestamp, cpu, mem, down, up in rows:
            self.assertIsInstance(timestamp, float)
            self.assertEqual((cpu, mem, down, up), (12.5, 2048.0, 123.5, 45.0))
        self.server.server_close()
        with self.assertRaises(sqlite3.ProgrammingError):
            logger.conn.execute('SELECT 1')

    def test_database_failure_does_not_stop_sampling(self):
        logger = mock.Mock(spec=server.DBLogger)
        logger.log.side_effect = sqlite3.OperationalError('database unavailable')
        with self.assertLogs(level='ERROR') as errors:
            self.start_server(interval=0.03, logger=logger)
            self.wait_for(lambda: self.server._version >= 2)
            self.assertEqual(self.request('/api/metrics')[0], 200)
            self.server.shutdown()
        self.assertEqual(logger.log.call_count, self.server._version)
        self.assertTrue(any('Failed to log metrics' in line for line in errors.output))

    def test_shutdown_wakes_streams_and_joins_sampler_before_closing_logger(self):
        logging_started = threading.Event()
        release_logger = threading.Event()
        logger = mock.Mock(spec=server.DBLogger)

        def log(*args):
            logging_started.set()
            release_logger.wait(timeout=3)

        logger.log.side_effect = log
        self.start_server(logger=logger)
        self.addCleanup(release_logger.set)
        self.assertTrue(logging_started.wait(timeout=2))
        streams = [self.open_stream(), self.open_stream()]
        for stream in streams:
            self.assertTrue(self.read_frame(stream).startswith(b'data: '))
        request_threads = list(self.server._threads)
        sampler_alive_at_close = []
        logger.close.side_effect = lambda: sampler_alive_at_close.append(self.server._sampler.is_alive())

        def shutdown():
            self.server.shutdown()
            self.server.server_close()

        shutdown_thread = threading.Thread(target=shutdown, daemon=True)
        shutdown_thread.start()
        try:
            self.assertTrue(self.server._stop_event.wait(timeout=2))
            for stream in streams:
                self.assertEqual(self.read_frame(stream), b'')
            logger.close.assert_not_called()
            self.assertTrue(self.server._sampler.is_alive())
        finally:
            release_logger.set()
            shutdown_thread.join(timeout=3)
        self.assertFalse(shutdown_thread.is_alive())
        self.assertFalse(self.server._sampler.is_alive())
        self.assertTrue(all(not thread.is_alive() for thread in request_threads))
        self.assertEqual(sampler_alive_at_close, [False])
        logger.close.assert_called_once()
        self.server.server_close()
        logger.close.assert_called_once()

    def test_socket_timeout_and_stream_write_failure(self):
        timeouts = []
        setup = server.MetricsHandler.setup

        def record_timeout(handler):
            setup(handler)
            timeouts.append(handler.connection.gettimeout())

        self.start_server()
        with mock.patch.object(server.MetricsHandler, 'setup', record_timeout):
            self.assertEqual(self.request('/api/metrics')[0], 200)
        self.assertEqual(timeouts, [self.server.socket_timeout])
        handler = object.__new__(server.MetricsHandler)
        handler.server = self.server
        handler.path = '/api/metrics/stream'
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = mock.Mock()
        for error in (TimeoutError, BrokenPipeError, ConnectionResetError):
            with self.subTest(error=error):
                handler.wfile.reset_mock()
                handler.wfile.write.side_effect = error
                handler.do_GET()
                handler.wfile.write.assert_called_once()
                self.assertTrue(handler.close_connection)

    def test_run_server_returns_nonzero_when_serve_fails(self):
        fake_server = mock.Mock()
        fake_server.serve_forever.side_effect = RuntimeError('serve failed')
        with mock.patch('cooler_btop.server.DataCollector'), \
                mock.patch('cooler_btop.server.MetricsServer', return_value=fake_server), \
                mock.patch('cooler_btop.server.signal.signal'), \
                self.assertLogs(level='ERROR'):
            result = server.run_server()
        self.assertEqual(result, 1)
        fake_server.server_close.assert_called_once_with()


class TestStressTestCleanup(unittest.TestCase):
    def test_partial_startup_cleans_up_started_processes(self):
        daemon = mock.Mock()
        load = mock.Mock()
        with mock.patch.object(stress_test.time, 'sleep'), \
                mock.patch.object(
                    stress_test.subprocess,
                    'Popen',
                    side_effect=[daemon, load, OSError('spawn failed')],
                ):
            with self.assertRaisesRegex(OSError, 'spawn failed'):
                stress_test.run_stress_test()

        daemon.terminate.assert_called_once_with()
        daemon.wait.assert_called_once_with(timeout=2)
        load.terminate.assert_called_once_with()
        load.wait.assert_called_once_with(timeout=2)

    def test_cleanup_kills_process_that_does_not_terminate(self):
        daemon = mock.Mock()
        daemon.wait.side_effect = [
            stress_test.subprocess.TimeoutExpired('cooler-btop', 2),
            None,
        ]
        with mock.patch.object(stress_test.time, 'sleep'), \
                mock.patch.object(
                    stress_test.subprocess,
                    'Popen',
                    side_effect=[daemon, OSError('spawn failed')],
                ):
            with self.assertRaisesRegex(OSError, 'spawn failed'):
                stress_test.run_stress_test()

        daemon.terminate.assert_called_once_with()
        daemon.kill.assert_called_once_with()
        self.assertEqual(
            daemon.wait.call_args_list,
            [mock.call(timeout=2), mock.call(timeout=2)],
        )

    def test_cleanup_error_does_not_mask_startup_failure_or_skip_processes(self):
        daemon = mock.Mock()
        first_load = mock.Mock()
        first_load.wait.side_effect = [OSError('wait failed'), None]
        second_load = mock.Mock()
        with mock.patch.object(stress_test.time, 'sleep'), \
                mock.patch.object(
                    stress_test.subprocess,
                    'Popen',
                    side_effect=[
                        daemon,
                        first_load,
                        second_load,
                        OSError('spawn failed'),
                    ],
                ):
            with self.assertRaisesRegex(OSError, 'spawn failed'):
                stress_test.run_stress_test()

        first_load.kill.assert_called_once_with()
        second_load.terminate.assert_called_once_with()
        daemon.terminate.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
