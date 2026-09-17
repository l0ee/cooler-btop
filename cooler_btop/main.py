import datetime
import math
import os
import signal
import sys
import threading
from copy import deepcopy

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Input, Static

from .ui import (
    AMBER, MINT, MUTED, AnimePetWidget, ConnsWidget, CPUWidget, DiskWidget,
    GPUWidget, HelpModal, MemWidget, ProcDetailsModal, ProcWidget, SysInfoWidget,
    NetWidget, TerminateModal,
)
from .data import DataCollector, prepare_processes


# A lower bound prevents a command-line typo from turning the sampler into a
# busy loop.  A day is long enough for a paused-looking monitor while still
# keeping the interval in a range Textual can represent predictably.
MIN_REFRESH_INTERVAL = 0.1
MAX_REFRESH_INTERVAL = 86400.0
MAX_LOG_RETENTION_ROWS = 10_000_000


def _validate_refresh_interval(value):
    """Return a usable refresh interval or raise a user-facing ValueError."""
    try:
        interval = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("interval must be a finite number") from error
    if not math.isfinite(interval):
        raise ValueError("interval must be a finite number")
    if not MIN_REFRESH_INTERVAL <= interval <= MAX_REFRESH_INTERVAL:
        raise ValueError(
            f"interval must be between {MIN_REFRESH_INTERVAL:g} and "
            f"{MAX_REFRESH_INTERVAL:g} seconds"
        )
    return interval


def _validate_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("port must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def _validate_log_retention(value):
    try:
        rows = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("log retention must be an integer") from error
    if not 1 <= rows <= MAX_LOG_RETENTION_ROWS:
        raise ValueError(
            f"log retention must be between 1 and {MAX_LOG_RETENTION_ROWS} rows"
        )
    return rows


def _parse_interval(value):
    """argparse adapter with a readable diagnostic for invalid intervals."""
    try:
        return _validate_refresh_interval(value)
    except ValueError as error:
        import argparse
        raise argparse.ArgumentTypeError(str(error)) from error


def _status_url(host, port):
    """Build a status URL, including brackets when *host* is IPv6."""
    host = str(host)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{int(port)}/api/metrics"


def _read_process_identity(pid):
    """Return the process owner and kernel start tick for PID reuse checks."""
    proc_dir = os.path.join(os.environ.get('HOST_PROC', '/proc'), str(pid))
    try:
        uid = os.stat(proc_dir).st_uid
        with open(os.path.join(proc_dir, 'stat'), 'rb') as stat_file:
            stat_data = stat_file.read()
        rparen = stat_data.rfind(b')')
        if rparen < 0:
            return None
        fields = stat_data[rparen + 2:].split()
        if len(fields) <= 19:
            return None
        return uid, int(fields[19])
    except (OSError, ValueError):
        return None


def _proc_root_is_current_namespace():
    """Allow PID signaling only when identity reads use this PID namespace."""
    configured = os.environ.get('HOST_PROC')
    if configured is None:
        return True
    if not configured:
        return False
    try:
        return os.path.realpath(configured) == os.path.realpath('/proc')
    except OSError:
        return False


def _resolve_auth_token(explicit=None, file_path=None):
    """Resolve CLI, file, and environment token sources without printing them."""
    if explicit is not None:
        return explicit
    from .server import AUTH_FILE_ENV, read_auth_token_file

    if file_path:
        return read_auth_token_file(file_path)
    token = os.environ.get('COOLER_BTOP_AUTH_TOKEN')
    if token is not None:
        return token
    environment_path = os.environ.get(AUTH_FILE_ENV)
    return read_auth_token_file(environment_path) if environment_path else None

CSS = """
$bg: #0b1014;
$surface: #121a20;
$panel: #1a252b;
$edge: #29363d;
$text: #e8efec;
$muted: #8a9b9f;
$primary: #a3e6ba;
$secondary: #82c9d7;

Screen {
    layout: vertical;
    background: $bg;
    color: $text;
    scrollbar-size: 1 1;
    scrollbar-background: $surface;
    scrollbar-color: $edge;
    scrollbar-color-hover: $muted;
    scrollbar-color-active: $primary;
}
#masthead { height: 1; padding: 0 2; background: $surface; }
#brand { width: 1fr; height: 1; text-overflow: ellipsis; text-wrap: nowrap; }
#pet { width: 10; height: 1; }
#context {
    height: 1; padding: 0 2; color: $muted;
    text-overflow: ellipsis; text-wrap: nowrap;
}
#dashboard { height: 1fr; padding: 0 1; scrollbar-gutter: stable; }
#overview {
    grid-size: 2; grid-columns: 1fr 1fr; grid-rows: 9;
    grid-gutter: 0 1; height: 9; margin-bottom: 1;
}
.box {
    border: round $edge; border-title-color: $muted;
    border-title-style: bold; border-subtitle-color: $muted;
    padding: 0 1; min-width: 0; width: 1fr; height: 1fr;
    background: $surface; color: $text;
}
.box:focus-within { border: round $primary; border-title-color: $primary; }
MetricWidget { scrollbar-gutter: stable; }
#cpu { border-title-color: #efb078; }
#mem, #net { border-title-color: $secondary; }
#proc { height: 12; margin-bottom: 1; }
#diagnostics {
    grid-size: 3; grid-columns: 1fr 1fr 1fr; grid-rows: 10 7;
    grid-gutter: 1; height: 18;
}
#conns { column-span: 2; }
.compact #proc { height: 10; }
.compact #diagnostics { grid-size: 2; grid-columns: 1fr 1fr; grid-rows: 10 9 7; height: 28; }
.compact #disk { column-span: 2; }
.compact #conns { column-span: 1; }
.narrow #overview { grid-size: 1; grid-columns: 1fr; grid-rows: 4 4; grid-gutter: 1; height: 9; }
.narrow #proc { height: 10; }
.narrow #diagnostics { grid-size: 1; grid-columns: 1fr; grid-rows: 10 10 9 7 7; height: 47; }
.narrow #disk, .narrow #conns { column-span: 1; }
DataTable { height: 1fr; background: $surface; color: $text; }
DataTable > .datatable--header { background: $panel; color: $muted; text-style: bold; }
DataTable > .datatable--odd-row { background: #151f25; }
DataTable > .datatable--even-row { background: $surface; }
DataTable > .datatable--cursor {
    background: #293e35; color: $primary; text-style: bold;
}
DataTable > .datatable--hover { background: #213039; }
.empty { height: auto; color: $muted; padding: 0 1; }
.io-readout { height: auto; margin-bottom: 1; }
#filter-input {
    dock: bottom; display: none; height: 3; margin: 0 1 1 1;
    border: solid $secondary; background: $panel; color: $text;
}
#filter-input.-active { display: block; }
Footer { background: $surface; color: $muted; }
ModalScreen { align: center middle; background: $bg 80%; }
.dialog {
    width: 76; max-width: 94%; height: 24; max-height: 90%;
    padding: 1 2; border: round $primary; background: $surface;
}
.dialog-title { height: auto; color: $primary; text-style: bold; margin-bottom: 1; }
.dialog-body { height: 1fr; margin-bottom: 1; }
.dialog-actions { height: 3; }
.dialog Button { min-width: 12; margin-right: 2; }
.dialog Button:focus { text-style: bold; border: tall $primary; }
#terminate-dialog { height: 14; border: round #efb078; }
#terminate-dialog .dialog-title { color: #efb078; }
"""


class BtopCloneApp(App):
    CSS = CSS
    AUTO_FOCUS = None
    BINDINGS = [
        Binding("p", "toggle_pause", "Pause/Run"),
        Binding("r", "toggle_record", "Record", show=False),
        Binding("e", "replay", "Replay", show=False),
        Binding("left_square_bracket", "replay_step(-1)", "Previous sample", show=False),
        Binding("right_square_bracket", "replay_step(1)", "Next sample", show=False),
        Binding("l", "live", "Live", show=False),
        Binding("space", "toggle_pause", "Pause", show=False),
        Binding("f", "filter_procs", "Filter"),
        Binding("slash", "filter_procs", "Filter", show=False),
        Binding("t", "toggle_tree", "Tree/Flat"),
        Binding("c", "sort_cpu", "CPU"),
        Binding("m", "sort_mem", "Mem"),
        Binding("k", "kill_proc", "Term"),
        Binding("tab", "focus_next", "Panels"),
        Binding("g", "focus_processes", "Processes", show=False),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("h", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, *, collector=None, refresh_interval=1.0, show_pet=True, **kwargs):
        super().__init__(**kwargs)
        self.register_theme(Theme(
            name="cooler", primary="#a3e6ba", secondary="#82c9d7", accent="#a3e6ba",
            foreground="#e8efec", background="#0b1014", surface="#121a20", panel="#1a252b",
            success="#a3e6ba", warning="#efb078", error="#de938a", dark=True,
        ))
        self.theme = "cooler"
        self.collector = collector
        self.refresh_interval = _validate_refresh_interval(refresh_interval)
        self.show_pet = show_pet
        self.proc_filter = ""
        self.sort_key = "cpu_percent"
        self.tree_view = False
        self.paused = False
        self._sampling = False
        self._generation = 0
        self._sample_cancel = threading.Event()
        self._sample_thread = None
        self._closed = False
        self._collector_closed = False
        self._collector_lock = threading.Lock()
        self._process_snapshot = []
        self.last_sample = None
        self.sample_error = ""
        self.capture_limit = 300
        self.recording = False
        self.capture = []
        self.replay_index = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="masthead"):
            self.brand = Static(Text("COOLER BTOP / SYSTEM MONITOR", style=f"bold {MINT}"), id="brand")
            yield self.brand
            if self.show_pet:
                self.pet = AnimePetWidget(id="pet")
                yield self.pet
        self.context = Static(id="context", markup=False)
        yield self.context
        with VerticalScroll(id="dashboard", can_focus=False):
            with Grid(id="overview"):
                self.cpu = CPUWidget(id="cpu", classes="box")
                self.cpu.border_title = "CPU / COMPUTE"
                yield self.cpu
                self.mem = MemWidget(id="mem", classes="box")
                self.mem.border_title = "MEMORY / RAM + SWAP"
                yield self.mem
            self.proc = ProcWidget(id="proc", classes="box")
            self.proc.border_title = "PROCESSES"
            self.proc.border_subtitle = "Enter inspect / k terminate / Tab more panels"
            yield self.proc
            with Grid(id="diagnostics"):
                self.sysinfo = SysInfoWidget(id="sysinfo", classes="box")
                self.sysinfo.border_title = "SYSTEM / HOST"
                yield self.sysinfo
                self.net = NetWidget(id="net", classes="box")
                self.net.border_title = "NETWORK / TRAFFIC"
                yield self.net
                self.disk = DiskWidget(id="disk", classes="box")
                self.disk.border_title = "STORAGE / DISKS"
                yield self.disk
                self.gpu = GPUWidget(id="gpu", classes="box")
                self.gpu.border_title = "GPU / ACCELERATOR"
                yield self.gpu
                self.conns = ConnsWidget(id="conns", classes="box")
                self.conns.border_title = "NETWORK / LISTENING PORTS"
                yield self.conns
        self.filter_input = Input(placeholder="Filter name / command / user / PID   Enter apply / Esc clear", id="filter-input")
        self.filter_input.border_title = "PROCESS FILTER / Enter apply / Esc clear"
        yield self.filter_input
        footer = Footer(show_command_palette=False)
        footer.compact = True
        yield footer

    def on_mount(self) -> None:
        self._set_layout(self.size.width)
        self.proc.dt.focus(scroll_visible=False)
        self._update_context()
        self.update_stats()
        self._stats_timer = self.set_interval(self.refresh_interval, self.update_stats)

    def on_unmount(self) -> None:
        """Stop UI scheduling and release telemetry resources on every exit."""
        self._closed = True
        self._generation += 1
        self._sample_cancel.set()
        timer = getattr(self, '_stats_timer', None)
        if timer is not None:
            timer.stop()
        sampler = self._sample_thread
        # Do not close a collector while its worker may still be inside a
        # backend call. A blocked worker is daemonized and will release OS
        # resources with the process; a completed worker closes it below.
        if sampler is None or not sampler.is_alive():
            self._close_collector()

    def _close_collector(self):
        with self._collector_lock:
            if self._collector_closed or self.collector is None:
                return
            self._collector_closed = True
            collector = self.collector
        close = getattr(collector, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                # UI shutdown must remain best-effort even if a platform
                # telemetry backend has already disappeared.
                pass  # nosec B110

    def on_resize(self, event):
        if hasattr(self, "context"):
            self._set_layout(event.size.width)
            self.call_after_refresh(self._update_context)

    def _set_layout(self, width):
        screen = self.screen_stack[0]
        screen.set_class(width < 110, "compact")
        screen.set_class(width < 70, "narrow")

    def update_stats(self):
        if self.replay_index is not None:
            if not self.paused:
                self._show_replay(self.replay_index + 1)
            return
        # Admit only one sample; presentation changes never launch another
        # worker. The daemon thread is deliberately independent of Textual's
        # executor so a blocked collector cannot keep interpreter shutdown
        # alive.
        if self._closed or self.paused or self._sampling:
            return
        self._sampling = True
        self._sample_cancel.clear()
        self._sample_thread = threading.Thread(
            target=self._sample_stats,
            args=(self._generation,),
            name='tui-telemetry-sampler',
            daemon=True,
        )
        self._sample_thread.start()

    def _sample_cancelled(self, generation):
        return (
            self._closed
            or self._sample_cancel.is_set()
            or generation != self._generation
        )

    def _sample_stats(self, generation):
        snapshot, error = None, ""

        def finish_sample():
            try:
                self.call_from_thread(self._finish_sample, generation, snapshot, error)
            except RuntimeError:
                pass  # nosec B110

        try:
            if self._sample_cancelled(generation):
                if self._closed:
                    self._close_collector()
                finish_sample()
                return
            with self._collector_lock:
                if self.collector is None:
                    self.collector = DataCollector()
                collector = self.collector
            if self._sample_cancelled(generation):
                if self._closed:
                    self._close_collector()
                finish_sample()
                return
            snapshot = {
                "sys": collector.get_sys_info(),
                "cpu": collector.get_cpu(),
                "mem": collector.get_mem(),
                "gpu": collector.get_gpu(),
                "disk": collector.get_disk(),
                "net": collector.get_net(),
                "conns": collector.get_connections(),
                "procs": collector.get_procs(tree=False, limit=None),
            }
            if self._sample_cancelled(generation):
                snapshot = None
        except Exception as exc:
            snapshot = None
            error = f"{type(exc).__name__}: {exc}"
        if self._closed:
            self._close_collector()
        finish_sample()

    def _finish_sample(self, generation, snapshot, error):
        self._sampling = False
        if self._closed:
            return
        if generation != self._generation or self.paused or self.replay_index is not None:
            if not self.paused and self.replay_index is None:
                self.call_later(self.update_stats)
            return
        self.sample_error = error
        if snapshot is not None:
            sampled = datetime.datetime.now()
            try:
                if self.recording:
                    self.capture.append((sampled, deepcopy(snapshot)))
                    if len(self.capture) >= self.capture_limit:
                        self.recording = False
                        self.notify("Recording full. Press e to replay.")
                self._display_snapshot(snapshot, sampled)
            except Exception as exc:
                # Collector calls happen in a worker, but widgets render on the
                # app thread.  Keep a bad/partial snapshot from terminating the
                # whole TUI and make the next timer tick retry it.
                self.sample_error = f"RenderError: {type(exc).__name__}: {exc}"
                self._update_context()
        else:
            self._update_context()

    def _display_snapshot(self, snapshot, sampled):
        if not isinstance(snapshot, dict):
            raise TypeError("sample must be a mapping")
        self.sysinfo.sys_data = snapshot['sys']
        self.cpu.cpu_data = snapshot['cpu']
        self.mem.mem_data = snapshot['mem']
        self.gpu.gpu_data = snapshot['gpu']
        self.disk.disk_data = snapshot['disk']
        self.net.net_data = snapshot['net']
        self.conns.conns_data = snapshot['conns']
        self._process_snapshot = snapshot['procs']
        if self.show_pet:
            self.pet.cpu_speed = snapshot['cpu']['total']
            self.pet.paused = self.paused
        brand = Text("COOLER BTOP", style=f"bold {MINT}")
        brand.append(f" / {snapshot['sys']['hostname']}", style=MUTED)
        self.brand.update(brand)
        self._refresh_processes()
        # Commit the sample timestamp only after every widget has accepted the
        # frame.  A partially rendered frame must not enable process actions.
        self.last_sample = sampled
        self._update_context()

    def action_toggle_record(self):
        if self.replay_index is not None:
            self.notify("Press l to return to live monitoring before recording.")
            return
        self.recording = not self.recording
        if self.recording:
            self.capture = []
            # Samples already in flight belong to the preceding time window.
            self._generation += 1
            self._sample_cancel.set()
            self.notify("Recording started (up to 300 samples). r stops; e replays.")
        self._update_context()

    def action_replay(self):
        if not self.capture:
            self.notify("No recorded samples. Press r to start recording.")
            return
        self.recording = False
        self._generation += 1
        self._sample_cancel.set()
        self.paused = False
        self.sample_error = ""
        self._show_replay(0)
        self._restart_stats_timer()

    def _restart_stats_timer(self):
        self._stats_timer.stop()
        self._stats_timer = self.set_interval(self.refresh_interval, self.update_stats)

    def _clear_graphs(self):
        self.cpu.history.clear()
        self.net.down_hist.clear()
        self.net.up_hist.clear()
        self.net.max_speed = 1024

    def _show_replay(self, index):
        self.replay_index = max(0, min(index, len(self.capture) - 1))
        if self.replay_index == len(self.capture) - 1:
            self.paused = True
        # Rebuild the graph prefix so stepping backward never mixes future samples.
        self._clear_graphs()
        for _, snapshot in self.capture[max(0, self.replay_index - 239):self.replay_index]:
            self.cpu.history.append(snapshot['cpu']['total'])
            self.net.down_hist.append(snapshot['net']['down'])
            self.net.up_hist.append(snapshot['net']['up'])
        sampled, snapshot = self.capture[self.replay_index]
        self._display_snapshot(deepcopy(snapshot), sampled)

    def action_replay_step(self, direction):
        if self.replay_index is not None:
            self.paused = True
            self._show_replay(self.replay_index + direction)

    def action_live(self):
        if self.replay_index is None:
            return
        self.replay_index = None
        self.paused = False
        self._generation += 1
        self._sample_cancel.set()
        self._clear_graphs()
        self.last_sample = None
        if self.show_pet:
            self.pet.paused = False
        self._update_context()
        self.update_stats()
        self._restart_stats_timer()

    def _refresh_processes(self):
        procs = prepare_processes(self._process_snapshot, self.sort_key, self.proc_filter, self.tree_view)
        self.proc.empty.update(Text(
            "No processes match. Esc clears the filter."
            if self.proc_filter else "No processes in this sample.", style=MUTED,
        ))
        self.proc.proc_data = procs
        self._update_context()

    def _update_context(self):
        sort = "CPU" if self.sort_key == "cpu_percent" else "MEM"
        mode = "TREE branches" if self.tree_view and not self.proc_filter else "FLAT"
        if self.tree_view and self.proc_filter:
            mode = "FLAT (filtered tree)"
        count = len(self.proc.proc_data or [])
        total = len(self._process_snapshot)
        retry = f"RETRY {self.sample_error.partition(':')[0]}" if self.sample_error else ""
        state = "PAUSED" if self.paused else retry if self.sample_error else "LIVE" if self.last_sample else "STARTING"
        if self.replay_index is not None:
            sampled = self.last_sample.strftime('%H:%M:%S') if self.last_sample else 'waiting'
            state = f"REPLAY {'PAUSED ' if self.paused else ''}{self.replay_index + 1}/{len(self.capture)} @ {sampled}"
        elif self.recording:
            state += f" / REC {len(self.capture)}/{self.capture_limit}"
        text = Text(f"{state} {self.refresh_interval:g}s", style=f"bold {AMBER if self.paused or self.sample_error else MINT}")
        text.append(f"  |  {sort} high  |  {mode}  |  {count}/{total} PIDs", style=MUTED)
        text.append(f"  |  filter: {self.proc_filter or 'off'}", style=MINT if self.proc_filter else MUTED)
        if getattr(self.size, "width", 0) >= 110:
            sampled = self.last_sample.strftime('%H:%M:%S') if self.last_sample else 'waiting'
            text.append(f"  |  sampled {sampled}", style=MUTED)
        self.context.update(text)
        self.context.tooltip = self.sample_error or "p pause/resume / r record / e replay / [ ] step / l live / h help"
        self.proc.border_title = f"PROCESSES / {count} of {total} / {sort} high / {mode}"

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-input":
            self.proc_filter = event.value.strip()
            self._refresh_processes()

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "filter-input":
            self.filter_input.remove_class("-active")
            self.action_focus_processes()

    def action_sort_cpu(self):
        self.sort_key = "cpu_percent"
        self._refresh_processes()

    def action_sort_mem(self):
        self.sort_key = "memory_percent"
        self._refresh_processes()

    def action_toggle_tree(self):
        self.tree_view = not self.tree_view
        self._refresh_processes()

    def action_toggle_pause(self):
        self.paused = not self.paused
        self._generation += 1
        if self.paused:
            self._sample_cancel.set()
        if self.show_pet:
            self.pet.paused = self.paused
        self._update_context()
        if not self.paused:
            if self.replay_index is None:
                self.update_stats()
            else:
                self._restart_stats_timer()

    def action_filter_procs(self):
        if self.filter_input.has_class("-active"):
            self.action_clear_filter()
        else:
            self.filter_input.add_class("-active")
            self.proc.scroll_visible(animate=False, top=True)
            self.filter_input.focus()

    def action_clear_filter(self):
        self.filter_input.value = ""
        self.filter_input.remove_class("-active")
        self.proc_filter = ""
        self._refresh_processes()
        self.action_focus_processes()

    def action_focus_processes(self):
        self.proc.dt.focus()
        self.proc.scroll_visible(animate=False)

    def action_help(self):
        self.push_screen(HelpModal())

    def action_kill_proc(self):
        if self.replay_index is not None or self.last_sample is None:
            self.notify("Process termination requires a live sample.", severity="warning")
            return
        pid = self.proc.selected_pid
        if not self.proc.dt.has_focus or pid is None or pid <= 0:
            self.notify("Select a process with a positive PID first.", severity="warning")
            return
        process = next((p for p in self._process_snapshot if p['pid'] == pid), None)
        if process is None:
            return
        uid = process.get('uid')
        start_time = process.get('start_time')
        if pid == 1 or pid == os.getpid():
            self.notify("Cooler btop will not terminate PID 1 or itself.", severity="error")
            return
        if uid != os.getuid() or start_time is None:
            self.notify("Only verified processes owned by your user can be terminated.", severity="error")
            return
        self.push_screen(
            TerminateModal(pid, str(process.get('name') or '')),
            lambda confirmed: self._terminate_process(pid, uid, start_time, confirmed),
        )

    def _terminate_process(self, pid, expected_uid, expected_start_time, confirmed):
        self.proc.dt.focus()
        if confirmed is not True or pid <= 0 or self.replay_index is not None or self.last_sample is None:
            return
        if pid == 1 or pid == os.getpid() or expected_uid != os.getuid() or expected_start_time is None:
            self.notify("Process identity is not safe to terminate.", severity="error")
            return
        if not _proc_root_is_current_namespace():
            self.notify(
                "Process termination is disabled when HOST_PROC is outside this PID namespace.",
                severity="error",
            )
            return
        pidfd_open = getattr(os, 'pidfd_open', None)
        pidfd_send_signal = getattr(signal, 'pidfd_send_signal', None)
        if not callable(pidfd_open) or not callable(pidfd_send_signal):
            self.notify(
                "Process termination is unavailable on this Python/Linux build; no signal was sent.",
                severity="error",
            )
            return
        pidfd = None
        try:
            pidfd = pidfd_open(pid)
            if _read_process_identity(pid) != (expected_uid, expected_start_time):
                self.notify("Process changed or exited; no signal was sent.", severity="error")
                return
            pidfd_send_signal(pidfd, signal.SIGTERM)
            self.notify(f"Sent SIGTERM to PID {pid}")
        except (AttributeError, OSError, ValueError) as error:
            self.notify(f"Could not terminate PID {pid}: {error}", severity="error")
        finally:
            if pidfd is not None:
                try:
                    os.close(pidfd)
                except OSError:
                    pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table is self.proc.dt:
            if self.replay_index is not None or self.last_sample is None:
                self.notify("Live process inspection is unavailable during replay.")
                return
            pid = int(event.row_key.value)
            if pid > 0:
                process = next(
                    (item for item in self._process_snapshot if item.get('pid') == pid),
                    None,
                )
                if process is None:
                    self.notify("Process changed or exited; details are unavailable.")
                    return
                self.push_screen(ProcDetailsModal(
                    pid,
                    expected_uid=process.get('uid'),
                    expected_start_time=process.get('start_time'),
                    identity_reader=_read_process_identity,
                ))

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected):
        if event.data_table is self.proc.dt:
            if event.column_key.value == "CPU%":
                self.action_sort_cpu()
            elif event.column_key.value == "MEM%":
                self.action_sort_mem()

    def check_action(self, action, parameters):
        if isinstance(self.screen, ModalScreen) and action in {
            "toggle_pause", "filter_procs", "toggle_tree", "sort_cpu", "sort_mem",
            "kill_proc", "focus_processes", "clear_filter", "help",
            "toggle_record", "replay", "replay_step", "live",
        }:
            return False
        return True

def _run_tui(args):
    try:
        app = BtopCloneApp(refresh_interval=args.interval, show_pet=not args.no_pet)
        app.run()
        if app.return_code:
            # Textual may consume lifecycle errors; it retains their details here.
            error = getattr(app, "_exception", None)
            if isinstance(error, Exception):
                raise error
            raise RuntimeError(f"Textual exited with status {app.return_code}")
    except Exception as error:
        print(
            f"Cooler btop could not start.\n{type(error).__name__}: {error}\n"
            "Report reproducible failures at "
            "https://github.com/l0ee/cooler-btop/issues",
            file=sys.stderr,
        )
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        if args.desktop and sys.stdin.isatty():
            print("Press Enter to close this window.", file=sys.stderr)
            sys.stdin.readline()
        return 1
    return 0


def _run_daemon(args):
    """Start the daemon and turn startup failures into a normal CLI result."""
    try:
        from .server import run_server

        server_args = dict(
            host=args.host,
            port=args.port,
            db_path=args.log_db,
            interval=args.interval,
        )
        if args.auth_token is not None:
            server_args['auth_token'] = args.auth_token
        if args.privacy_mode:
            server_args['privacy_mode'] = True
        if args.log_retention is not None:
            server_args['log_retention'] = args.log_retention
        result = run_server(**server_args)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(
            f"Cooler btop daemon could not start.\n{type(error).__name__}: {error}\n"
            "Check the bind address, port, permissions, and interval, then retry.\n"
            "Report reproducible failures at "
            "https://github.com/l0ee/cooler-btop/issues",
            file=sys.stderr,
        )
        return 1
    return 0 if result is None else result


def run_cli():
    import argparse
    parser = argparse.ArgumentParser(description="Cooler btop v2")
    parser.add_argument("--desktop", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--daemon", "--serve", action="store_true", help="Run in headless web server mode")
    parser.add_argument("--host", default="127.0.0.1", help="Daemon bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=_validate_port, default=8080, help="Port for the web server")
    parser.add_argument("--log-db", type=str, default=None, help="Path to SQLite DB for historical metrics logging (Daemon mode only)")
    parser.add_argument(
        "--log-retention", type=_validate_log_retention, default=None,
        help="Maximum number of SQLite metric rows to retain",
    )
    token_options = parser.add_mutually_exclusive_group()
    token_options.add_argument(
        "--auth-token", default=None,
        help=(
            "Bearer token for the daemon API; required for non-loopback binds "
            "(visible in process listings and shell history)"
        ),
    )
    token_options.add_argument(
        "--auth-token-file", default=None, metavar="PATH",
        help="Read the daemon token from an owner-readable-only file (mode 0600)",
    )
    parser.add_argument(
        "--privacy-mode", "--mask-process-args", action="store_true",
        help="Replace process command arguments with process names in daemon API data",
    )
    parser.add_argument(
        "--interval", type=_parse_interval, default=1.0,
        help=f"Update interval in seconds ({MIN_REFRESH_INTERVAL:g}-{MAX_REFRESH_INTERVAL:g}; default: 1.0)",
    )
    parser.add_argument("--no-pet", action="store_true", help="Disable the animated ASCII pet in the TUI")
    parser.add_argument("--version", action="version", version="Cooler btop v2.0.0", help="Print version and exit")
    parser.add_argument("--status", action="store_true", help="Query the local daemon and print a CLI status summary")
    args = parser.parse_args()

    if args.daemon or args.status:
        try:
            args.auth_token = _resolve_auth_token(
                args.auth_token, args.auth_token_file,
            )
        except ValueError as error:
            parser.error(str(error))

    if args.status:
        try:
            import requests
            request_options = {'timeout': 2}
            token = args.auth_token
            if token:
                request_options['headers'] = {'Authorization': f'Bearer {token}'}
            session = requests.Session()
            session.trust_env = False
            try:
                r = session.get(
                    _status_url(args.host, args.port),
                    allow_redirects=False,
                    **request_options,
                )
            finally:
                session.close()
            if r.status_code == 200:
                data = r.json()
                print(f"Cooler Btop Daemon: ONLINE (Port {args.port})")
                print(f"Host: {data['sys']['hostname']} | Uptime: {data['sys']['uptime']}")
                print(f"CPU Load: {data['cpu']['total']}% | RAM: {data['mem']['mem']['percent']}%")
                return 0
            else:
                print(f"Daemon returned status code {r.status_code}")
                return 1
        except Exception:
            print("Daemon is offline or unreachable. Start it with `cooler-btop --daemon`.")
            return 1

    if args.daemon:
        return _run_daemon(args)
    else:
        return _run_tui(args)

if __name__ == "__main__":
    raise SystemExit(run_cli())
