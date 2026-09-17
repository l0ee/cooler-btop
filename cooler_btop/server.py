import threading
import time
import json
import os
import sqlite3
import gzip
import hmac
import ipaddress
import logging
import math
import signal
import socket
from http.cookies import CookieError, SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, unquote, urlsplit
from .data import DataCollector


MIN_INTERVAL = 0.05
MAX_INTERVAL = 3600.0
DEFAULT_MAX_STREAM_CLIENTS = 32
DEFAULT_SAMPLER_SHUTDOWN_TIMEOUT = 5.0
DEFAULT_STALE_SNAPSHOT_MULTIPLIER = 3.0
MIN_STALE_SNAPSHOT_AGE = 1.0
DEFAULT_LOG_RETENTION_ROWS = 100_000
MAX_AUTH_TOKEN_LENGTH = 4096

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

class DBLogger:
    def __init__(self, db_path, max_rows=DEFAULT_LOG_RETENTION_ROWS):
        self.db_path = db_path
        try:
            max_rows = int(max_rows)
        except (TypeError, ValueError) as error:
            raise ValueError('max_rows must be a positive integer') from error
        if max_rows <= 0:
            raise ValueError('max_rows must be a positive integer')
        self.max_rows = max_rows
        self._prune_every = max(1, min(1000, max_rows // 10 or 1))
        self._writes_since_prune = 0
        self._lock = threading.RLock()
        self._closed = False

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
        c.execute('CREATE INDEX IF NOT EXISTS metrics_timestamp_idx ON metrics(timestamp)')
        self.conn.commit()

    def log(self, cpu, mem, net):
        with self._lock:
            if self._closed:
                return
            c = self.conn.cursor()
            c.execute('INSERT INTO metrics VALUES (?, ?, ?, ?, ?)',
                      (time.time(), cpu['total'], mem['mem']['used'],
                       net['down'], net['up']))
            self._writes_since_prune += 1
            if self._writes_since_prune >= self._prune_every:
                # rowid is used instead of timestamp so equal timestamps do
                # not accidentally delete more or fewer rows than intended.
                c.execute(
                    'DELETE FROM metrics WHERE rowid NOT IN '
                    '(SELECT rowid FROM metrics ORDER BY timestamp DESC, rowid DESC LIMIT ?)',
                    (self.max_rows,),
                )
                self._writes_since_prune = 0
            self.conn.commit()

    def close(self):
        with self._lock:
            if self._closed:
                return
            try:
                self.conn.close()
            finally:
                self._closed = True


def _host_is_loopback(host):
    """Return True only when *host* resolves entirely to loopback addresses."""
    value = str(host or '').strip().strip('[]')
    if not value:
        return False
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        try:
            results = socket.getaddrinfo(value, None, type=socket.SOCK_STREAM)
        except OSError:
            return False
        addresses = {result[4][0] for result in results if result[4]}
        if not addresses:
            return False
        try:
            return all(ipaddress.ip_address(address).is_loopback for address in addresses)
        except ValueError:
            return False

class MetricsServer(ThreadingHTTPServer):
    allow_reuse_address = True
    # A client that stops reading an SSE stream must not prevent the process
    # from shutting down.  Active handlers still observe _stop_event and exit
    # cleanly when possible; daemon threads are the bounded fallback.
    daemon_threads = True
    socket_timeout = 5.0
    heartbeat_interval = 15.0
    min_interval = MIN_INTERVAL
    max_interval = MAX_INTERVAL
    max_stream_clients = DEFAULT_MAX_STREAM_CLIENTS
    sampler_shutdown_timeout = DEFAULT_SAMPLER_SHUTDOWN_TIMEOUT
    stale_snapshot_multiplier = DEFAULT_STALE_SNAPSHOT_MULTIPLIER
    minimum_stale_snapshot_age = MIN_STALE_SNAPSHOT_AGE

    def __init__(
        self,
        server_address,
        RequestHandlerClass,
        collector,
        logger=None,
        interval=1.0,
        max_stream_clients=None,
        shutdown_timeout=None,
        stale_after=None,
        auth_token=None,
        privacy_mode=False,
    ):
        try:
            requested_interval = float(interval)
        except (TypeError, ValueError) as error:
            raise ValueError('interval must be a positive, finite number') from error
        if not math.isfinite(requested_interval) or requested_interval <= 0:
            raise ValueError('interval must be a positive, finite number')
        # Preserve acceptance of positive intervals while preventing accidental
        # busy loops and unreasonably long sleeps from untrusted CLI/config
        # input.  The published interval is the effective interval used by the
        # sampler, so clients see the real cadence.
        self.interval = min(max(requested_interval, self.min_interval), self.max_interval)

        if max_stream_clients is None:
            max_stream_clients = type(self).max_stream_clients
        try:
            max_stream_clients = int(max_stream_clients)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError('max_stream_clients must be a positive integer') from error
        if max_stream_clients <= 0:
            raise ValueError('max_stream_clients must be a positive integer')
        self.max_stream_clients = max_stream_clients

        if shutdown_timeout is None:
            shutdown_timeout = type(self).sampler_shutdown_timeout
        try:
            shutdown_timeout = float(shutdown_timeout)
        except (TypeError, ValueError) as error:
            raise ValueError('shutdown_timeout must be a non-negative, finite number') from error
        if not math.isfinite(shutdown_timeout) or shutdown_timeout < 0:
            raise ValueError('shutdown_timeout must be a non-negative, finite number')
        self.sampler_shutdown_timeout = shutdown_timeout

        if stale_after is None:
            stale_after = max(
                type(self).minimum_stale_snapshot_age,
                self.interval * type(self).stale_snapshot_multiplier,
            )
        try:
            stale_after = float(stale_after)
        except (TypeError, ValueError) as error:
            raise ValueError('stale_after must be a positive, finite number') from error
        if not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError('stale_after must be a positive, finite number')
        self.stale_after = stale_after

        if auth_token is not None:
            auth_token = str(auth_token).strip()
            if not auth_token or len(auth_token) > MAX_AUTH_TOKEN_LENGTH:
                raise ValueError(
                    f'auth_token must contain 1-{MAX_AUTH_TOKEN_LENGTH} characters'
                )
        if not _host_is_loopback(server_address[0]) and not auth_token:
            raise ValueError(
                'non-loopback daemon binds require --auth-token '
                '(or COOLER_BTOP_AUTH_TOKEN)'
            )
        self.auth_token = auth_token
        self.privacy_mode = bool(privacy_mode)

        self.collector = collector
        self.logger = logger
        self._collector_closed = False
        self._logger_closed = False
        self._logger_close_requested = False
        self._lifecycle_lock = threading.RLock()
        self._server_close_lock = threading.Lock()
        self._condition = threading.Condition()
        self._stop_event = threading.Event()
        self._sampler_done = threading.Event()
        self._snapshot = None
        self._snapshot_monotonic = None
        self._version = 0
        self._stream_clients = 0
        self._shutdown_started = None
        self._serve_forever_active = threading.Event()
        self._sampler_shutdown_warning_logged = False
        self._sampler = None
        super().__init__(server_address, RequestHandlerClass)
        # Collection code may be supplied by a plugin or host subsystem and
        # cannot be forcibly interrupted by Python.  Keep the thread daemonized
        # and use a bounded join during shutdown so a stuck collector cannot
        # hold the HTTP server or interpreter hostage.
        self._sampler = threading.Thread(
            target=self._sample_metrics,
            name='metrics-sampler',
            daemon=True,
        )
        self._sampler.start()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.socket_timeout)
        return connection, address

    def serve_forever(self, poll_interval=0.5):
        # BaseServer.shutdown() is specified for a running serve_forever loop.
        # Keep the stop event authoritative so an immediate shutdown before
        # the serving thread starts cannot leave a later loop running forever.
        if self._stop_event.is_set():
            return
        self._serve_forever_active.set()
        try:
            return super().serve_forever(poll_interval=poll_interval)
        finally:
            self._serve_forever_active.clear()

    def _sample_metrics(self):
        try:
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
                    if self.privacy_mode:
                        data['procs'] = [
                            {
                                **process,
                                'cmdline': str(process.get('name') or 'unknown'),
                            }
                            for process in data['procs']
                            if isinstance(process, dict)
                        ]
                    payload = json.dumps(data, allow_nan=False).encode('utf-8')
                except Exception:
                    logging.exception('Failed to sample metrics')
                    with self._condition:
                        # A failed collection must not be advertised as a fresh sample.
                        self._snapshot = None
                        self._snapshot_monotonic = None
                        self._condition.notify_all()
                else:
                    with self._condition:
                        if self._stop_event.is_set():
                            break
                        self._snapshot = payload
                        self._snapshot_monotonic = time.monotonic()
                        self._version += 1
                        self._condition.notify_all()
                    if self.logger is not None:
                        try:
                            self.logger.log(cpu, mem, data['net'])
                        except Exception:
                            logging.exception('Failed to log metrics to the database')
                self._stop_event.wait(self.interval)
        finally:
            self._sampler_done.set()
            self._close_collector_if_safe()
            self._close_logger_if_safe()

    def _stop_sampler(self):
        self._request_sampler_stop()
        sampler = self._sampler
        if sampler is None or sampler is threading.current_thread():
            return True
        if sampler.is_alive():
            sampler.join(timeout=self._remaining_shutdown_timeout())
        if sampler.is_alive():
            with self._lifecycle_lock:
                should_warn = not self._sampler_shutdown_warning_logged
                self._sampler_shutdown_warning_logged = True
            if should_warn:
                logging.warning(
                    'Metrics sampler did not stop within %.2f seconds; '
                    'leaving its daemon thread running',
                    self.sampler_shutdown_timeout,
                )
            return False
        return True

    def _request_sampler_stop(self):
        with self._lifecycle_lock:
            if self._shutdown_started is None:
                self._shutdown_started = time.monotonic()
        self._stop_event.set()
        with self._condition:
            self._condition.notify_all()

    def _remaining_shutdown_timeout(self):
        with self._lifecycle_lock:
            if self._shutdown_started is None:
                self._shutdown_started = time.monotonic()
            elapsed = max(0.0, time.monotonic() - self._shutdown_started)
            return max(0.0, self.sampler_shutdown_timeout - elapsed)

    def _snapshot_stale_after(self):
        try:
            stale_after = float(self.stale_after)
        except (TypeError, ValueError):
            stale_after = self.minimum_stale_snapshot_age
        if not math.isfinite(stale_after) or stale_after <= 0:
            stale_after = self.minimum_stale_snapshot_age
        return stale_after

    def _snapshot_is_fresh_locked(self, now=None):
        if self._snapshot is None or self._snapshot_monotonic is None:
            return False
        if now is None:
            now = time.monotonic()
        age = max(0.0, now - self._snapshot_monotonic)
        return age <= self._snapshot_stale_after()

    def _retry_after_header(self):
        return str(max(1, math.ceil(self.interval)))

    def _try_acquire_stream(self):
        with self._condition:
            if self._stop_event.is_set() or self._stream_clients >= self.max_stream_clients:
                return False
            self._stream_clients += 1
            return True

    def _release_stream(self):
        with self._condition:
            if self._stream_clients > 0:
                self._stream_clients -= 1
            self._condition.notify_all()

    def _close_logger_if_safe(self):
        if self.logger is None:
            return
        with self._lifecycle_lock:
            if (
                not self._logger_close_requested
                or self._logger_closed
                or not self._sampler_done.is_set()
            ):
                return
            # Mark before invoking arbitrary logger code so a logger callback
            # or a concurrent server_close cannot close it twice.
            self._logger_closed = True
            logger = self.logger
        try:
            logger.close()
        except Exception:
            logging.exception('Failed to close metrics logger')

    def _close_collector_if_safe(self):
        if self.collector is None:
            return
        with self._lifecycle_lock:
            if self._collector_closed or not self._sampler_done.is_set():
                return
            self._collector_closed = True
            collector = self.collector
        close = getattr(collector, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                logging.exception('Failed to close metrics collector')

    def shutdown(self):
        # Signal the sampler before asking serve_forever to stop.  This wakes
        # SSE waiters immediately and lets normal collectors exit at their next
        # cancellation point, while the join below remains time-bounded.
        self._request_sampler_stop()
        if self._serve_forever_active.is_set():
            super().shutdown()
        self._stop_sampler()

    def server_close(self):
        with self._server_close_lock:
            self._logger_close_requested = True
            self._request_sampler_stop()
            try:
                # Close the listening socket before waiting for a collector so
                # no new work can enter while shutdown is in progress.
                super().server_close()
            finally:
                self._stop_sampler()
                self._close_collector_if_safe()
                self._close_logger_if_safe()


def _accepts_gzip(accept_encoding):
    """Return whether Accept-Encoding permits gzip compression.

    Explicit gzip entries take precedence over a wildcard, including an
    explicit q=0.  Invalid gzip quality values are treated conservatively as
    not acceptable rather than accidentally enabling compression.
    """
    explicit_gzip = None
    wildcard = None
    for item in (accept_encoding or '').split(','):
        parts = item.split(';')
        encoding = parts[0].strip().lower()
        if not encoding:
            continue
        quality = 1.0
        invalid_quality = False
        for parameter in parts[1:]:
            name, separator, value = parameter.strip().partition('=')
            if name.strip().lower() != 'q':
                continue
            if not separator:
                invalid_quality = True
                break
            try:
                quality = float(value.strip())
            except (TypeError, ValueError):
                invalid_quality = True
                break
            if not math.isfinite(quality) or quality < 0 or quality > 1:
                invalid_quality = True
                break
        if encoding == 'gzip':
            quality = 0.0 if invalid_quality else quality
            explicit_gzip = (
                quality if explicit_gzip is None else min(explicit_gzip, quality)
            )
        elif encoding == '*' and not invalid_quality:
            wildcard = quality if wildcard is None else min(wildcard, quality)
    if explicit_gzip is not None:
        return explicit_gzip > 0
    return wildcard is not None and wildcard > 0

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

    def _query_token(self):
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=False)
        values = query.get('token') or []
        return values[0] if values else None

    def _cookie_token(self):
        raw_cookie = self.headers.get('Cookie', '')
        if not raw_cookie:
            return None
        try:
            cookies = SimpleCookie()
            cookies.load(raw_cookie)
            morsel = cookies.get('cooler_btop_token')
            return unquote(morsel.value) if morsel is not None else None
        except (CookieError, TypeError, ValueError):
            return None

    def _authorized(self):
        expected = getattr(self.server, 'auth_token', None)
        if not expected:
            return True
        provided = None
        authorization = self.headers.get('Authorization', '')
        if authorization.casefold().startswith('bearer '):
            provided = authorization[7:].strip()
        if not provided:
            provided = self.headers.get('X-Cooler-Btop-Token')
        if not provided:
            provided = self._query_token()
        if not provided:
            provided = self._cookie_token()
        return bool(provided) and hmac.compare_digest(str(provided), str(expected))

    def _authorization_failure(self):
        self._respond(
            401,
            b'{"status":"error","message":"Authentication required"}',
            headers={
                'Cache-Control': 'no-store',
                'WWW-Authenticate': 'Bearer',
            },
        )

    def do_POST(self):
        if not self._authorized():
            self._authorization_failure()
            return
        self._respond(404, b'{"status":"error","message":"Not found"}')

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self._authorized():
            self._authorization_failure()
            return
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
                headers = {}
                query_token = self._query_token()
                if getattr(self.server, 'auth_token', None) and query_token:
                    # Native EventSource cannot set Authorization headers. A
                    # valid dashboard URL token can therefore be exchanged for
                    # a same-origin HttpOnly cookie before the stream starts.
                    headers['Set-Cookie'] = (
                        'cooler_btop_token=' + quote(query_token, safe='')
                        + '; Path=/; HttpOnly; SameSite=Strict'
                    )
                self._respond(200, payload, content_type, headers=headers)

        elif path == '/api/metrics':
            with self.server._condition:
                payload = None
                if (
                    not self.server._stop_event.is_set()
                    and self.server._snapshot_is_fresh_locked()
                ):
                    payload = self.server._snapshot
            headers = {'Cache-Control': 'no-store', 'Vary': 'Accept-Encoding'}
            if payload is None:
                headers['Retry-After'] = self.server._retry_after_header()
                self._respond(503, b'{"status":"error","message":"Metrics are unavailable"}',
                              headers=headers)
                return
            if _accepts_gzip(self.headers.get('Accept-Encoding', '')):
                payload = gzip.compress(payload)
                headers['Content-Encoding'] = 'gzip'
            self._respond(200, payload, headers=headers)

        elif path == '/api/metrics/stream':
            if not self.server._try_acquire_stream():
                self._respond(
                    503,
                    b'{"status":"error","message":"Too many metric streams"}',
                    headers={
                        'Cache-Control': 'no-store',
                        'Retry-After': self.server._retry_after_header(),
                    },
                )
                return
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close')
                self.send_header('X-Accel-Buffering', 'no')
                self.send_header('Retry-After', self.server._retry_after_header())
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('X-Frame-Options', 'DENY')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                version = 0
                stale_deadline = time.monotonic() + self.server._snapshot_stale_after()
                while True:
                    with self.server._condition:
                        if self.server._stop_event.is_set():
                            break
                        now = time.monotonic()
                        if self.server._snapshot is not None:
                            snapshot_time = self.server._snapshot_monotonic
                            if snapshot_time is not None:
                                stale_deadline = snapshot_time + self.server._snapshot_stale_after()
                            snapshot_fresh = self.server._snapshot_is_fresh_locked(now)
                        else:
                            snapshot_fresh = False
                        if not snapshot_fresh and now >= stale_deadline:
                            stale = True
                            payload = None
                            next_version = version
                        else:
                            stale = False
                            payload = (
                                self.server._snapshot if snapshot_fresh else None
                            )
                            next_version = self.server._version
                            if payload is None or next_version == version:
                                heartbeat = self.server.heartbeat_interval
                                try:
                                    heartbeat = float(heartbeat)
                                except (TypeError, ValueError):
                                    heartbeat = 15.0
                                if not math.isfinite(heartbeat) or heartbeat <= 0:
                                    heartbeat = 15.0
                                wait_timeout = min(
                                    heartbeat,
                                    max(0.0, stale_deadline - now),
                                )
                                self.server._condition.wait_for(
                                    lambda: self.server._stop_event.is_set() or (
                                        self.server._snapshot is not None
                                        and self.server._snapshot_is_fresh_locked()
                                        and self.server._version != version
                                    ),
                                    timeout=wait_timeout,
                                )
                                if self.server._stop_event.is_set():
                                    break
                                now = time.monotonic()
                                snapshot_fresh = self.server._snapshot_is_fresh_locked(now)
                                if self.server._snapshot is not None and self.server._snapshot_monotonic is not None:
                                    stale_deadline = (
                                        self.server._snapshot_monotonic
                                        + self.server._snapshot_stale_after()
                                    )
                                if not snapshot_fresh and now >= stale_deadline:
                                    stale = True
                                    payload = None
                                    next_version = version
                                else:
                                    stale = False
                                    payload = self.server._snapshot if snapshot_fresh else None
                                    next_version = self.server._version
                    if stale:
                        stale_payload = (
                            b'event: stale\n'
                            b'retry: 1000\n'
                            b'data: {"status":"error","message":"Metrics snapshot is stale"}\n\n'
                        )
                        self.wfile.write(stale_payload)
                        self.wfile.flush()
                        break
                    if payload is not None and next_version != version:
                        self.wfile.write(b'data: ' + payload + b'\n\n')
                        version = next_version
                    else:
                        self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
            except OSError:
                self.close_connection = True
            finally:
                self.server._release_stream()
        else:
            self._respond(404, b'{"status":"error","message":"Not found"}')

def run_server(
    host='127.0.0.1', port=None, db_path=None, interval=1.0,
    auth_token=None, privacy_mode=False, log_retention=None,
):
    collector = None
    logger = None
    server = None
    previous_handlers = {}
    exit_code = 0

    try:
        if port is None:
            port = int(os.environ.get("PORT", 8080))
        else:
            port = int(port)
        collector = DataCollector()
        if auth_token is None:
            auth_token = os.environ.get('COOLER_BTOP_AUTH_TOKEN')
        if db_path:
            logger = DBLogger(
                db_path,
                max_rows=(DEFAULT_LOG_RETENTION_ROWS if log_retention is None
                          else log_retention),
            )
        server = MetricsServer(
            (host, port), MetricsHandler, collector, logger, interval=interval,
            auth_token=auth_token, privacy_mode=privacy_mode,
        )

        # Graceful shutdown handler.  signal.signal is only valid in the main
        # thread; callers embedding run_server in a worker thread can still
        # stop the returned HTTP server through their own lifecycle control.
        shutdown_event = threading.Event()

        def signal_handler(signum, frame):
            if not shutdown_event.is_set():
                logging.info("Received signal %s, gracefully shutting down...", signum)
                shutdown_event.set()
                threading.Thread(target=server.shutdown, daemon=True).start()

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, signal_handler)

        logging.info("Server started on http://%s:%s", host, port)
        if logger:
            logging.info("Logging metrics to %s", db_path)
        server.serve_forever()
    except Exception:
        logging.exception('Metrics server failed')
        exit_code = 1
    finally:
        for signum, previous_handler in previous_handlers.items():
            try:
                signal.signal(signum, previous_handler)
            except Exception:
                logging.exception('Failed to restore signal handler for %s', signum)
        if server is not None:
            try:
                server.server_close()
            except Exception:
                logging.exception('Failed to close metrics server')
                exit_code = 1
        else:
            # MetricsServer can reject a non-loopback bind before ownership of
            # the collector/logger is transferred to it. Close both objects
            # on that partial-startup path as well.
            if collector is not None:
                close = getattr(collector, 'close', None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        logging.exception('Failed to close metrics collector')
                        exit_code = 1
            if logger is not None:
                try:
                    logger.close()
                except Exception:
                    logging.exception('Failed to close metrics logger')
                    exit_code = 1
        logging.info("Server shutdown complete.")
    return exit_code

if __name__ == '__main__':
    raise SystemExit(run_server())
