import threading
import time
import json
import os
import sqlite3
import gzip
import logging
import math
import signal
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit
from .data import DataCollector

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

class DBLogger:
    def __init__(self, db_path):
        self.db_path = db_path

        # Ensure parent directory exists
        parent_dir = os.path.dirname(os.path.abspath(db_path))
        if parent_dir and not os.path.exists(parent_dir):
            try:
                os.makedirs(parent_dir, exist_ok=True)
            except Exception as e:
                print(f"Warning: Could not create directory for SQLite DB: {e}")

        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        c = self.conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS metrics
                     (timestamp REAL, cpu_total REAL, mem_used REAL,
                      net_down REAL, net_up REAL)''')
        self.conn.commit()

    def log(self, cpu, mem, net):
        c = self.conn.cursor()
        c.execute('INSERT INTO metrics VALUES (?, ?, ?, ?, ?)',
                  (time.time(), cpu['total'], mem['mem']['used'],
                   net['down'], net['up']))
        self.conn.commit()

    def close(self):
        self.conn.close()

class MetricsServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = False
    socket_timeout = 5.0
    heartbeat_interval = 15.0

    def __init__(self, server_address, RequestHandlerClass, collector, logger=None, interval=1.0):
        self.interval = float(interval)
        if not math.isfinite(self.interval) or self.interval <= 0:
            raise ValueError('interval must be a positive, finite number')
        self.collector = collector
        self.logger = logger
        self._logger_closed = False
        self._condition = threading.Condition()
        self._stop_event = threading.Event()
        self._snapshot = None
        self._version = 0
        self._sampler = None
        super().__init__(server_address, RequestHandlerClass)
        self._sampler = threading.Thread(target=self._sample_metrics, name='metrics-sampler')
        self._sampler.start()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.socket_timeout)
        return connection, address

    def _sample_metrics(self):
        while not self._stop_event.is_set():
            try:
                cpu = self.collector.get_cpu()
                mem_raw = self.collector.get_mem()
                mem = {
                    'mem': mem_raw['mem']._asdict(),
                    'swap': mem_raw['swap']._asdict(),
                }
                data = {
                    'cpu': cpu,
                    'mem': mem,
                    'gpu': self.collector.get_gpu(),
                    'disk': self.collector.get_disk(),
                    'net': self.collector.get_net(),
                    'procs': self.collector.get_procs(tree=False),
                    'conns': self.collector.get_connections(),
                    'sys': self.collector.get_sys_info(),
                    'timestamp': time.time(),
                    'interval': self.interval,
                }
                payload = json.dumps(data, allow_nan=False).encode('utf-8')
            except Exception:
                logging.exception('Failed to sample metrics')
                with self._condition:
                    # A failed collection must not be advertised as a fresh sample.
                    self._snapshot = None
                    self._condition.notify_all()
            else:
                with self._condition:
                    if self._stop_event.is_set():
                        break
                    self._snapshot = payload
                    self._version += 1
                    self._condition.notify_all()
                if self.logger is not None:
                    try:
                        self.logger.log(cpu, mem, data['net'])
                    except Exception:
                        logging.exception('Failed to log metrics to the database')
            self._stop_event.wait(self.interval)

    def _stop_sampler(self):
        self._stop_event.set()
        with self._condition:
            self._condition.notify_all()
        if self._sampler is not None:
            self._sampler.join()

    def shutdown(self):
        self._stop_sampler()
        super().shutdown()

    def server_close(self):
        self._stop_sampler()
        try:
            super().server_close()
        finally:
            if self.logger is not None and not self._logger_closed:
                self.logger.close()
                self._logger_closed = True

class MetricsHandler(BaseHTTPRequestHandler):
    assets = {
        '/': ('index.html', 'text/html; charset=utf-8'),
    }

    def _respond(self, status, payload, content_type='application/json', headers=None):
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            self.close_connection = True

    def do_POST(self):
        self._respond(404, b'{"status":"error","message":"Not found"}')

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in self.assets:
            filename, content_type = self.assets[path]
            asset_path = os.path.join(os.path.dirname(__file__), 'web', filename)
            try:
                with open(asset_path, 'rb') as asset:
                    payload = asset.read()
            except FileNotFoundError:
                self._respond(404, b'{"status":"error","message":"Asset not found"}')
            except OSError:
                logging.exception('Failed to read asset %s', filename)
                self._respond(500, b'{"status":"error","message":"Could not read asset"}')
            else:
                self._respond(200, payload, content_type)

        elif path == '/api/metrics':
            with self.server._condition:
                payload = None if self.server._stop_event.is_set() else self.server._snapshot
            headers = {'Cache-Control': 'no-store', 'Vary': 'Accept-Encoding'}
            if payload is None:
                headers['Retry-After'] = str(max(1, math.ceil(self.server.interval)))
                self._respond(503, b'{"status":"error","message":"Metrics are unavailable"}',
                              headers=headers)
                return
            if 'gzip' in self.headers.get('Accept-Encoding', '').lower():
                payload = gzip.compress(payload)
                headers['Content-Encoding'] = 'gzip'
            self._respond(200, payload, headers=headers)

        elif path == '/api/metrics/stream':
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('X-Frame-Options', 'DENY')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                version = 0
                while True:
                    with self.server._condition:
                        self.server._condition.wait_for(
                            lambda: self.server._stop_event.is_set() or (
                                self.server._snapshot is not None and self.server._version != version
                            ),
                            timeout=self.server.heartbeat_interval,
                        )
                        if self.server._stop_event.is_set():
                            break
                        payload = self.server._snapshot
                        next_version = self.server._version
                    if payload is not None and next_version != version:
                        self.wfile.write(b'data: ' + payload + b'\n\n')
                        version = next_version
                    else:
                        self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
            except OSError:
                self.close_connection = True
        else:
            self._respond(404, b'{"status":"error","message":"Not found"}')

def run_server(host='127.0.0.1', port=None, db_path=None, interval=1.0):
    if port is None:
        port = int(os.environ.get("PORT", 8080))

    collector = DataCollector()
    logger = DBLogger(db_path) if db_path else None
    server = MetricsServer(
        (host, port), MetricsHandler, collector, logger, interval=interval,
    )

    # Graceful shutdown handler
    shutdown_event = threading.Event()

    def signal_handler(signum, frame):
        if not shutdown_event.is_set():
            logging.info(f"Received signal {signum}, gracefully shutting down...")
            shutdown_event.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logging.info("Server started on http://%s:%s", host, port)
    if logger:
        logging.info(f"Logging metrics to {db_path}")

    exit_code = 0
    try:
        server.serve_forever()
    except Exception:
        logging.exception('Metrics server failed')
        exit_code = 1
    finally:
        server.server_close()
        logging.info("Server shutdown complete.")
    return exit_code

if __name__ == '__main__':
    raise SystemExit(run_server())
