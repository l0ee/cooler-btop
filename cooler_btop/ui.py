from collections import deque
import datetime

import psutil
from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from .braille import make_braille_graph


TEXT = "#e8efec"
MUTED = "#8a9b9f"
MINT = "#a3e6ba"
AMBER = "#efb078"
CYAN = "#82c9d7"


def format_bytes(value):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(value) < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}PB"


def make_bar(percent, width=15):
    blocks = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]
    width = max(0, width)
    filled = max(0, min(100, percent)) / 100 * width
    full = int(filled)
    return "█" * full + (blocks[int((filled - full) * 8)] + " " * (width - full - 1) if full < width else "")


def display_value(value, suffix=""):
    return "N/A" if value is None else f"{value:g}{suffix}" if isinstance(value, (int, float)) else f"{value}{suffix}"


class MetricWidget(VerticalScroll):
    """A focusable, scrollable readout; resize redraws never add history samples."""

    def compose(self):
        self.body = Static(markup=False)
        yield self.body

    @property
    def graph_width(self):
        return max(1, self.body.size.width or self.content_size.width)

    def refresh_content(self):
        if hasattr(self, "body"):
            self.body.update(self.metric_text())

    def on_resize(self):
        self.refresh_content()


class SysInfoWidget(MetricWidget):
    sys_data = reactive(None)

    def watch_sys_data(self, data):
        self.refresh_content()

    def metric_text(self):
        data = self.sys_data
        if data is None:
            return Text("Waiting for system telemetry", style=MUTED)
        specs = data.get('specs') or {}
        cpu_temp = data.get('cpu_temp')
        if not isinstance(cpu_temp, (int, float)) or cpu_temp <= 0:
            cpu_temp = None
        hardware = ' '.join(
            str(value) for value in (specs.get('vendor'), specs.get('product_model')) if value
        ) or 'N/A'
        cores = ' / '.join(
            display_value(value) for value in (
                specs.get('physical_cores'), specs.get('logical_cores'),
            )
        ) + ' physical/logical'
        text = Text(style=TEXT)
        for label, value in [
            ("Host", data.get('hostname', 'N/A')),
            ("OS", specs.get('os') or 'N/A'),
            ("Kernel", data.get('kernel', 'N/A')),
            ("Hardware", hardware),
            ("Chassis", f"{specs.get('chassis') or 'N/A'} / {specs.get('architecture') or 'N/A'}"),
            ("CPU", specs.get('cpu_model') or 'N/A'),
            ("Cores", cores),
            ("BIOS", f"{specs.get('bios_version') or 'N/A'} / {specs.get('bios_date') or 'N/A'}"),
            ("Uptime", data.get('uptime', 'N/A')),
            ("Load", data.get('load_avg') or 'N/A'),
            ("Tasks", f"{data.get('procs_running', 0)} running"),
            ("CPU temp", display_value(cpu_temp, " C")),
            ("Battery", data.get('battery') or 'N/A'),
            ("FDs", f"{data.get('open_fds', 0)} / {data.get('max_fds', 0)}"),
        ]:
            if text:
                text.append("\n")
            text.append(f"{label:<9}", style=MUTED)
            text.append(str(value), style=AMBER if label == "CPU temp" else TEXT)
        for sensor in data.get('sensors') or []:
            if sensor.get('category') == 'CPU' and sensor.get('type') == 'temperature':
                continue
            text.append("\n")
            text.append(f"{str(sensor.get('label') or 'Sensor'):<9}", style=MUTED)
            value = sensor.get('value')
            if sensor.get('unit') == 'RPM' and isinstance(value, (int, float)):
                rendered = f"{value:g} RPM"
            else:
                rendered = display_value(value, f" {sensor.get('unit') or ''}".rstrip())
            text.append(rendered, style=AMBER)
        battery_info = data.get('battery_info') or []
        if battery_info:
            battery = battery_info[0]
            text.append("\n")
            text.append("Battery W", style=MUTED)
            text.append(display_value(battery.get('power_w'), " W"))
            text.append("  Energy ", style=MUTED)
            text.append(
                f"{display_value(battery.get('energy_now_wh'))} / "
                f"{display_value(battery.get('energy_full_wh'))} Wh"
            )
        return text


class CPUWidget(MetricWidget):
    cpu_data = reactive(None, always_update=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.history = deque(maxlen=240)

    def watch_cpu_data(self, data):
        if data is not None:
            self.history.append(data['total'])
        self.refresh_content()

    def metric_text(self):
        data = self.cpu_data
        if data is None:
            return Text("Waiting for CPU telemetry", style=MUTED)
        cores = data.get('per_core', [])
        core_ids = data.get('core_ids', [])
        width = self.graph_width
        text = Text(f"{data['total']:5.1f}%", style=f"bold {AMBER}")
        text.append(f"  {data.get('freq', 0) / 1000:.2f} GHz  /  {len(cores)} cores\n", style=MUTED)
        text.append(make_braille_graph(list(self.history), width, 2), style=AMBER)
        text.append("\nCORE LOAD", style=MUTED)
        columns = max(1, (width + 1) // 10)
        for index, load in enumerate(cores):
            core_id = core_ids[index] if index < len(core_ids) else index
            text.append("\n" if index % columns == 0 else " ")
            text.append(f"{core_id:02} ", style=MUTED)
            text.append(f"{load:5.1f}%", style=AMBER if load >= 50 else TEXT)
        return text


class MemWidget(MetricWidget):
    mem_data = reactive(None)

    def watch_mem_data(self, data):
        self.refresh_content()

    def metric_text(self):
        if self.mem_data is None:
            return Text("Waiting for memory telemetry", style=MUTED)
        mem, swap = self.mem_data['mem'], self.mem_data['swap']
        width = max(1, self.graph_width - 2)
        text = Text(f"RAM  {mem.percent:4.1f}%", style=f"bold {CYAN}")
        text.append(f"  {format_bytes(mem.used)} / {format_bytes(mem.total)}\n", style=TEXT)
        text.append(f"[{make_bar(mem.percent, width)}]\n", style=CYAN)
        text.append(f"Available {format_bytes(mem.available)}", style=MUTED)
        if hasattr(mem, 'buffers') and hasattr(mem, 'cached'):
            text.append(f"\nBuffers {format_bytes(mem.buffers)}  Cache {format_bytes(mem.cached)}", style=MUTED)
        text.append(f"\nSWAP {swap.percent:4.1f}%", style=f"bold {CYAN}")
        text.append(f"  {format_bytes(swap.used)} / {format_bytes(swap.total)}\n", style=TEXT)
        text.append(f"[{make_bar(swap.percent, width)}]", style=CYAN)
        return text


class GPUWidget(MetricWidget):
    gpu_data = reactive(None)

    def watch_gpu_data(self, data):
        self.refresh_content()

    def metric_text(self):
        if self.gpu_data is None:
            return Text("Waiting for GPU telemetry", style=MUTED)
        if not self.gpu_data or all(gpu['name'] == 'No GPU' for gpu in self.gpu_data):
            return Text("No GPU telemetry available\nCPU and memory monitoring remain active.", style=MUTED)
        text = Text(style=TEXT)
        for gpu in self.gpu_data:
            if text:
                text.append("\n\n")
            text.append(gpu.get('name') or 'N/A', style=f"bold {MINT}")
            load = gpu.get('load')
            if isinstance(load, (int, float)):
                text.append(f"\nLoad {load:5.1f}% ", style=MINT)
                text.append(make_bar(load, max(1, self.graph_width - 13)), style=MINT)
            else:
                text.append("\nLoad N/A", style=MUTED)
            mem_total = gpu.get('mem_total')
            mem_used = gpu.get('mem_used')
            mem_pct = gpu.get('mem_pct')
            if isinstance(mem_total, (int, float)) and mem_total > 0:
                text.append(
                    f"\nVRAM {display_value(mem_pct, '%')}  "
                    f"{display_value(mem_used)} / {display_value(mem_total)} MB", style=CYAN,
                )
            else:
                text.append("\nVRAM N/A", style=MUTED)
            text.append(f"\nTemp {display_value(gpu.get('temperature'), ' C')}", style=AMBER)
            text.append(f"  Power {display_value(gpu.get('power_w'), ' W')}", style=MUTED)
            text.append(f"  Fan {display_value(gpu.get('fan_pct'), '%')}", style=MUTED)
            text.append(
                f"\nClocks {display_value(gpu.get('clock_graphics_mhz'))} / "
                f"{display_value(gpu.get('clock_memory_mhz'))} MHz", style=MUTED,
            )
            if gpu.get('status') and gpu.get('status') != 'available':
                text.append(f"\n{gpu['status']}", style=MUTED)
        return text


class NetWidget(MetricWidget):
    net_data = reactive(None, always_update=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.down_hist = deque(maxlen=240)
        self.up_hist = deque(maxlen=240)
        self.max_speed = 1024

    def watch_net_data(self, data):
        if data is not None:
            self.down_hist.append(data['down'])
            self.up_hist.append(data['up'])
            self.max_speed = max(*self.down_hist, *self.up_hist, 1024)
        self.refresh_content()

    def metric_text(self):
        data = self.net_data
        if data is None:
            return Text("Waiting for network telemetry", style=MUTED)
        text = Text(style=TEXT)
        for label, key, history, color in [
            ("DOWN", "down", self.down_hist, CYAN),
            ("UP", "up", self.up_hist, MINT),
        ]:
            if text:
                text.append("\n")
            text.append(f"{label:<4} {format_bytes(data[key])}/s", style=f"bold {color}")
            text.append(f"  /  {format_bytes(data['total_' + key])} total\n", style=MUTED)
            points = [value / self.max_speed * 100 for value in history]
            text.append(make_braille_graph(points, self.graph_width, 2), style=color)
        text.append(f"\nShared scale {format_bytes(self.max_speed)}/s", style=MUTED)
        for interface in data.get('interfaces') or []:
            state = 'UP' if interface.get('is_up') is True else 'DOWN' if interface.get('is_up') is False else 'N/A'
            speed = display_value(interface.get('speed_mbps'), ' Mbps')
            mtu = display_value(interface.get('mtu'))
            text.append(f"\n{interface.get('name') or 'N/A'} {state}", style=MINT if state == 'UP' else MUTED)
            text.append(f"  {speed}  MTU {mtu}", style=MUTED)
        return text


class ConnsWidget(Vertical):
    conns_data = reactive(None)

    def compose(self):
        self.dt = DataTable(cursor_type="row", zebra_stripes=True)
        self.dt.add_columns("PROTO", "LOCAL PORT", "STATE")
        yield self.dt
        self.empty = Static("No listening ports", classes="empty", markup=False)
        yield self.empty

    def watch_conns_data(self, data):
        if data is None:
            return
        row = self.dt.cursor_row
        self.dt.clear()
        seen = set()
        for connection in data:
            key = (connection['proto'], connection['port'])
            if key not in seen:
                self.dt.add_row(Text(str(key[0])), str(key[1]), Text(str(connection.get('state', 'LISTEN'))))
                seen.add(key)
        self.dt.move_cursor(row=min(row, max(0, self.dt.row_count - 1)), scroll=False)
        self.empty.display = not seen


class DiskWidget(Vertical):
    disk_data = reactive(None)

    def compose(self):
        self.io_static = Static(markup=False, classes="io-readout")
        yield self.io_static
        self.dt = DataTable(cursor_type="row", zebra_stripes=True)
        for label in ("MOUNT", "FS", "SIZE", "USED", "FREE", "USE%"):
            self.dt.add_column(label, key=label)
        yield self.dt

    def on_resize(self):
        if self.disk_data is not None:
            self.watch_disk_data(self.disk_data)

    def watch_disk_data(self, data):
        if data is None:
            return
        io = data.get('io', {})
        text = Text(f"READ {format_bytes(io.get('read_bytes', 0))}/s", style=CYAN)
        text.append(f"  WRITE {format_bytes(io.get('write_bytes', 0))}/s", style=MINT)
        if not data.get('partitions'):
            text.append("\nNo mounted partitions", style=MUTED)
        for device in data.get('devices') or []:
            text.append(f"\n{device.get('name') or 'N/A'}  ", style=MUTED)
            text.append(device.get('model') or device.get('type') or 'N/A')
            text.append(f"  {format_bytes(device['capacity']) if device.get('capacity') is not None else 'N/A'}", style=MUTED)
        for zram in data.get('zram') or []:
            text.append(f"\n{zram.get('name') or 'zram'}  ", style=MINT)
            text.append(
                f"{format_bytes(zram['memory_bytes']) if zram.get('memory_bytes') is not None else 'N/A'} RAM / "
                f"{format_bytes(zram['original_bytes']) if zram.get('original_bytes') is not None else 'N/A'} data",
                style=MUTED,
            )
        self.io_static.update(text)
        row = self.dt.cursor_row
        self.dt.clear()
        for disk in {partition['mount']: partition for partition in data.get('partitions', [])}.values():
            mount = Text(disk['mount'], no_wrap=True)
            mount.truncate(max(5, min(14, self.content_size.width - 34)), overflow="ellipsis")
            self.dt.add_row(
                mount, disk.get('filesystem') or 'N/A', format_bytes(disk['total']).replace('.0', ''),
                format_bytes(disk['used']).replace('.0', ''),
                format_bytes(disk.get('free', disk['total'] - disk['used'])).replace('.0', ''),
                f"{disk['percent']:.1f}%", key=disk['mount'],
            )
        self.dt.move_cursor(row=min(row, max(0, self.dt.row_count - 1)), scroll=False)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        self.dt.tooltip = event.row_key.value


class ProcWidget(Vertical):
    proc_data = reactive(None, always_update=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._cell_cache = {}

    def compose(self):
        self.dt = DataTable(cursor_type="row", zebra_stripes=True, fixed_columns=1, id="process-table")
        for label, width in [("PID", 7), ("USER", 10), ("CPU%", 7), ("MEM%", 6), ("COMMAND", None)]:
            self.dt.add_column(label, key=label, width=width)
        self.empty = Static("Waiting for the first sample", classes="empty", markup=False, id="process-empty")
        yield self.empty
        yield self.dt

    @property
    def selected_pid(self):
        if not self.dt.row_count:
            return None
        return int(self.dt.ordered_rows[self.dt.cursor_row].key.value)

    def on_resize(self):
        if self.proc_data is not None:
            self.watch_proc_data(self.proc_data)

    def watch_proc_data(self, data):
        if data is None:
            return
        selected = self.selected_pid
        cursor_row = self.dt.cursor_row
        order = {str(process['pid']): index for index, process in enumerate(data)}
        for key in set(self._cell_cache) - order.keys():
            self.dt.remove_row(key)
            del self._cell_cache[key]

        command_width = max(12, self.content_size.width - 42)
        for process in data:
            pid = str(process['pid'])
            name = str(process.get('name') or '')
            command = process.get('cmdline') or ''
            if isinstance(command, (list, tuple)):
                command = ' '.join(command)
            command = str(command)
            if command and command != name.removeprefix(process.get('tree_prefix', '')):
                name += f" | {command}"
            display_name = Text(' '.join(name.splitlines()), style=TEXT, no_wrap=True)
            display_name.truncate(command_width, overflow="ellipsis")
            user = Text(str(process.get('username') or ''), no_wrap=True, overflow="ellipsis")
            user.truncate(10, overflow="ellipsis")
            cells = (
                pid, user, Text(f"{process.get('cpu_percent') or 0:6.1f}", style=AMBER),
                Text(f"{process.get('memory_percent') or 0:5.1f}", style=CYAN), display_name,
            )
            cached = self._cell_cache.get(pid)
            if cached is None:
                self.dt.add_row(*cells, key=pid)
            else:
                for column, old, new in zip(self.dt.ordered_columns, cached, cells):
                    if old != new:
                        self.dt.update_cell(pid, column.key, new, update_width=True)
            self._cell_cache[pid] = cells

        # DataTable insertion order is not display order. Keep identity, not row number.
        self.dt.sort("PID", key=lambda pid: order[pid])
        if data:
            row = order.get(str(selected), min(cursor_row, len(data) - 1))
            self.dt.move_cursor(row=row, scroll=self.dt.has_focus)
        self.empty.display = not data
        self.dt.show_cursor = bool(data)


class AnimePetWidget(Static):
    """A one-line companion in the masthead, never a separate dashboard row."""

    cpu_speed = reactive(0.0)
    paused = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tick = 0

    def on_mount(self):
        self.animate_pet()
        self.set_interval(0.3, self.animate_pet)

    def animate_pet(self):
        if self.paused:
            return
        self.tick += 2 if self.cpu_speed >= 50 else 1
        face = "(=^.^=)" if self.tick % 8 < 6 else "(=-.-=)"
        self.update(Text(face, style=MINT, justify="right"))


class DismissibleModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "close", "Close", priority=True)]
    AUTO_FOCUS = ".cancel"

    def action_close(self):
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.has_class("cancel"):
            event.stop()
            self.dismiss(False)


class HelpModal(DismissibleModal):
    BINDINGS = [Binding("h", "close", "Close", show=False)]

    def compose(self):
        with Vertical(classes="dialog", id="help-dialog"):
            yield Static("COOLER BTOP / FIELD GUIDE", classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                text = Text(style=TEXT)
                for keys, description in [
                    ("p / space", "Pause or resume sampling and graphs"),
                    ("r", "Start/stop recording (up to 300 samples in memory)"),
                    ("e", "Replay recording at the sampling interval"),
                    ("[ / ]", "Step backward/forward and pause replay"),
                    ("l", "Return to live monitoring"),
                    ("f / /", "Filter by name, command, user or PID"),
                    ("Enter / Esc", "Apply filter / clear filter and return to processes"),
                    ("c / m", "Sort CPU / memory, highest first"),
                    ("t", "Toggle flat list / process tree"),
                    ("Enter", "Inspect the selected process"),
                    ("k", "Request SIGTERM; confirmation defaults to Cancel"),
                    ("Tab / Shift+Tab", "Next / previous panel (scrolls into view)"),
                    ("Arrows / PgUp/Dn", "Scroll the focused panel or table"),
                    ("g / Home", "Focus processes / scroll a focused panel to its top"),
                    ("h / Esc", "Close help or a dialog"),
                    ("q", "Quit"),
                ]:
                    text.append(f"{keys:<17}", style=MINT)
                    text.append(description + "\n")
                text.append("\nTree order uses branch totals; cells show each PID's own usage.\n", style=MUTED)
                text.append("Filtering temporarily uses a flat list. Sorting and filtering also work while paused.\n", style=MUTED)
                text.append("Replay shows recorded timestamps; live process inspection and termination are disabled.\n", style=MUTED)
                text.append("A new recording replaces the previous one; recordings are lost on exit.\n", style=MUTED)
                text.append("All telemetry panels remain reachable by Tab or mouse-wheel scrolling.", style=MUTED)
                yield Static(text)
            yield Button("Close", id="close-help", classes="cancel")


class ProcDetailsModal(DismissibleModal):
    def __init__(self, pid: int, **kwargs):
        super().__init__(**kwargs)
        self.pid = pid

    def compose(self):
        with Vertical(classes="dialog", id="details-dialog"):
            yield Static(f"PROCESS / PID {self.pid}", classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                self.details = Static("Loading process details...", markup=False)
                yield self.details
            yield Button("Close", id="close-details", classes="cancel")

    def on_mount(self):
        self.load_details()

    @work(thread=True, exit_on_error=False)
    def load_details(self):
        text = Text(style=TEXT)
        try:
            process = psutil.Process(self.pid)
            with process.oneshot():
                for label, read in [
                    ("Name", process.name),
                    ("Command", lambda: ' '.join(process.cmdline())),
                    ("Executable", process.exe),
                    ("User", process.username),
                    ("Parent PID", process.ppid),
                    ("Status", process.status),
                    ("Created", lambda: datetime.datetime.fromtimestamp(process.create_time()).strftime("%Y-%m-%d %H:%M:%S")),
                    ("Memory", lambda: f"{process.memory_percent():.1f}% / {format_bytes(process.memory_info().rss)} RSS"),
                    ("Threads", process.num_threads),
                    ("Open FDs", lambda: process.num_fds()),
                    ("Connections", lambda: len(process.net_connections())),
                ]:
                    try:
                        value = read()
                    except (psutil.Error, OSError, AttributeError):
                        value = "Unavailable"
                    text.append(f"{label:<12}", style=MUTED)
                    text.append(f"{value}\n")
        except (psutil.Error, OSError, ValueError) as error:
            text.append(f"Process unavailable: {error}", style=AMBER)
        self.app.call_from_thread(self._show_details, text)

    def _show_details(self, text):
        if self.is_mounted:
            self.details.update(text)


class TerminateModal(DismissibleModal):
    def __init__(self, pid: int, name: str, **kwargs):
        if pid <= 0:
            raise ValueError("Only a positive PID may be terminated")
        super().__init__(**kwargs)
        self.pid = pid
        self.process_name = name

    def compose(self):
        with Vertical(classes="dialog", id="terminate-dialog"):
            yield Static("TERMINATE PROCESS?", classes="dialog-title")
            with VerticalScroll(classes="dialog-body"):
                text = Text(f"PID {self.pid}  ", style=f"bold {AMBER}")
                text.append(self.process_name, style=TEXT)
                text.append("\nSend SIGTERM to this process? Unsaved work may be lost.\nEscape cancels; no signal is sent until you confirm.", style=MUTED)
                yield Static(text)
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel-terminate", classes="cancel")
                yield Button("Send SIGTERM", id="confirm-terminate", variant="warning")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "confirm-terminate":
            event.stop()
            self.dismiss(True)
