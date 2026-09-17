import ipaddress
import math
import os
import psutil
import re
import socket
import subprocess
import time
from collections import namedtuple

from . import fast_telemetry
from .fast_telemetry import FastTelemetry

SYS_PATH = os.environ.get('HOST_SYS', '/sys')
TOPOLOGY_CACHE_SECONDS = 5.0
NVIDIA_STALE_SECONDS = 30.0
UUID_PATTERN = re.compile(
    r'(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])'
)
MAC_PATTERN = re.compile(
    r'(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])'
)
PRIVATE_PATH_COMPONENT = re.compile(
    r'(?i)(?:[0-9a-f]{4}-[0-9a-f]{4}|[0-9a-f]{12}|[0-9a-f]{16}|'
    r'(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2})'
)
IP_CANDIDATE_PATTERN = re.compile(
    r'(?i)(?<![0-9a-z])\[?[0-9a-f:.%]{2,}\]?(?![0-9a-z])'
)
IPV4_CANDIDATE_PATTERN = re.compile(
    r'(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])'
)
SCOPED_IPV6_CANDIDATE_PATTERN = re.compile(
    r'(?i)(?<![0-9a-z])\[?(?=[0-9a-f:.]*:)'
    r'[0-9a-f:.]+%[0-9a-z_.-]+\]?(?![0-9a-z])'
)
MAC_INTERFACE_PATTERN = re.compile(
    r'(?i)(?<![0-9a-z])(?:enx|wlx|wwx)[0-9a-f]{12}(?![0-9a-z])'
)
DISK_SECTOR_BYTES = 512
MAX_CONNECTIONS = 10
MAX_FALLBACK_PROCESS_COUNT = 4096
MAX_PROCESS_CMDLINE_CHARS = 4096


def _read_interface_link_addresses():
    root = os.path.join(SYS_PATH, 'class', 'net')
    addresses = {}
    try:
        names = os.listdir(root)
    except OSError:
        return addresses
    for name in names:
        try:
            with open(os.path.join(root, name, 'address'), 'r', encoding='ascii') as stream:
                address = stream.read().strip().casefold()
        except OSError:
            continue
        if address:
            addresses[name] = address
    return addresses


def _normalize_link_identity(address):
    if not isinstance(address, str):
        return None
    address = address.strip().casefold()
    if not re.fullmatch(r'(?:[0-9a-f]{2}:){5,19}[0-9a-f]{2}', address):
        return None
    if set(address.replace(':', '')) == {'0'}:
        return None
    return address


def _address_snapshot(interface_addresses):
    try:
        if callable(interface_addresses):
            snapshot = interface_addresses()
        else:
            snapshot = interface_addresses or {}
        return snapshot if hasattr(snapshot, 'get') else {}
    except (OSError, ValueError, TypeError, psutil.Error):
        return {}


def _link_identity(addresses, name):
    link_identity = addresses.get(name) if hasattr(addresses, 'get') else None
    if not isinstance(link_identity, str):
        try:
            link_identity = next((
                address.address for address in link_identity or ()
                if getattr(address, 'family', None) == psutil.AF_LINK
                and isinstance(getattr(address, 'address', None), str)
            ), None)
        except (TypeError, AttributeError, psutil.Error):
            link_identity = None
    return _normalize_link_identity(link_identity)


def _public_interface_name(name):
    if not isinstance(name, str):
        return str(name)
    match = re.fullmatch(r'(enx|wlx|wwx)[0-9a-fA-F]{12}', name)
    if not match:
        return name
    return {'enx': 'Ethernet', 'wlx': 'Wi-Fi', 'wwx': 'WWAN'}[match.group(1).casefold()]


def _redact_private_tokens(value):
    def redact_ip(match):
        candidate = match.group(0)
        address = candidate.strip('[]').split('%', 1)[0]
        try:
            ipaddress.ip_address(address)
        except ValueError:
            return candidate
        return '[redacted]'

    components = []
    for component in str(value).split('/'):
        if PRIVATE_PATH_COMPONENT.fullmatch(component):
            components.append('[redacted]')
            continue
        component = UUID_PATTERN.sub('[redacted]', component)
        # A zone identifier may itself be a colon-separated hardware address.
        # Remove that token before the scoped-IPv6 matcher can split it at the
        # first colon.
        component = MAC_PATTERN.sub('[redacted]', component)
        # Redact the whole scoped address before processing the scope name.
        # Otherwise a regex that stops at the interface's first non-hex
        # character can leave a MAC-derived name such as enx<mac> visible.
        component = SCOPED_IPV6_CANDIDATE_PATTERN.sub(
            lambda _match: '[redacted]', component,
        )
        component = IPV4_CANDIDATE_PATTERN.sub(redact_ip, component)
        component = IP_CANDIDATE_PATTERN.sub(redact_ip, component)
        component = MAC_INTERFACE_PATTERN.sub('[redacted]', component)
        components.append(component)
    return '/'.join(components)


def _public_storage_source(source):
    source = str(source)
    if source.casefold().startswith('/dev/disk/by-'):
        return 'Persistent device'
    if ':' in source or source.startswith('//'):
        return 'Network filesystem'
    return _redact_private_tokens(source)


def key_interface_counters(counters, interface_index, interface_addresses):
    """Key psutil counters by the kernel identity of each interface."""
    addresses = _address_snapshot(interface_addresses)
    keyed = {}
    try:
        items = list(counters.items())
    except (AttributeError, TypeError):
        return keyed
    for item in items:
        try:
            name, values = item
            link_identity = _link_identity(addresses, name)
            identity = (name, interface_index(name), link_identity)
        except (OSError, ValueError, TypeError, AttributeError, psutil.Error):
            continue
        keyed[identity] = values
    return keyed


def _sample_interface_counters(interface_index, interface_addresses):
    addresses_before = _address_snapshot(interface_addresses)
    indices_before = {}
    for name in addresses_before:
        try:
            indices_before[name] = interface_index(name)
        except (OSError, ValueError):
            continue
    try:
        counters = psutil.net_io_counters(pernic=True)
    except (OSError, AttributeError, TypeError, psutil.Error):
        return {}
    addresses_after = _address_snapshot(interface_addresses)
    keyed = key_interface_counters(counters, interface_index, addresses_after)
    stable = {}
    for identity, values in keyed.items():
        name, index, link_identity = identity
        if (link_identity is None
                or _link_identity(addresses_before, name) != link_identity
                or indices_before.get(name) != index):
            identity = (name, index, None)
        stable[identity] = values
    return stable


def build_process_tree(processes, sort_by="cpu_percent"):
    """Order branches by total usage while retaining each process's own metrics."""
    process_map = {
        p['pid']: {
            **p,
            'cumulative_cpu': p.get('cpu_percent') or 0,
            'cumulative_memory': p.get('memory_percent') or 0,
            'children': [],
        }
        for p in processes
    }
    roots = []
    for pid, node in process_map.items():
        ppid = node.get('ppid')
        if ppid in process_map and ppid != pid:
            process_map[ppid]['children'].append(node)
        else:
            roots.append(node)

    metric = 'cumulative_memory' if sort_by == 'memory_percent' else 'cumulative_cpu'
    order = lambda node: (-node[metric], node['pid'])
    forest = []
    visited = set()
    # Iterative postorder also keeps deep trees and racing/cyclic PID snapshots usable.
    for root in roots + list(process_map.values()):
        if root['pid'] in visited:
            continue
        forest.append(root)
        stack = [(root, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                for child in node['children']:
                    node['cumulative_cpu'] += child['cumulative_cpu']
                    node['cumulative_memory'] += child['cumulative_memory']
                node['children'].sort(key=order)
            else:
                visited.add(node['pid'])
                node['children'] = [c for c in node['children'] if c['pid'] not in visited]
                stack.append((node, True))
                stack.extend((child, False) for child in reversed(node['children']))

    forest.sort(key=order)
    result = []
    stack = [(root, '', True, True) for root in reversed(forest)]
    while stack:
        node, prefix, is_last, is_root = stack.pop()
        tree_prefix = '' if is_root else prefix + ('└─ ' if is_last else '├─ ')
        result.append({
            **{key: value for key, value in node.items() if key != 'children'},
            'name': tree_prefix + str(node.get('name') or ''),
            'tree_prefix': tree_prefix,
        })
        child_prefix = '' if is_root else prefix + ('   ' if is_last else '│  ')
        children = node['children']
        stack.extend(
            (child, child_prefix, i == len(children) - 1, False)
            for i, child in reversed(list(enumerate(children)))
        )
    return result


def prepare_processes(processes, sort_by="cpu_percent", filter_str="", tree=False, limit=None):
    """Present a snapshot without resampling or mutating the collector's data."""
    query = filter_str.strip().casefold()
    procs = [
        p for p in processes
        if not query or query in (
            f"{p['pid']} {p.get('name', '')} {p.get('cmdline', '')} {p.get('username', '')}"
        ).casefold()
    ]
    if tree and not query:
        procs = build_process_tree(procs, sort_by=sort_by)
    else:
        procs.sort(key=lambda p: (-(p.get(sort_by) or 0), p['pid']))
    return procs if limit is None else procs[:limit]

class DataCollector:
    def __init__(self, interface_index=socket.if_nametoindex, interface_addresses=None):
        self._interface_index = interface_index
        self._interface_addresses = interface_addresses or _read_interface_link_addresses
        self._last_net_by_interface = _sample_interface_counters(
            interface_index, self._interface_addresses,
        )
        self.last_time = time.monotonic()
        self._proc_cache = {}
        self._fast = FastTelemetry()
        self._data_disk_io_enabled = True

        # Let the fast CPU parser run once to establish a baseline for deltas
        self._fast.get_cpu_percent()

    def get_cpu(self):
        try:
            global_pct, per_core = self._fast.get_cpu_percent()
        except (AttributeError, OSError, TypeError, ValueError, psutil.Error):
            global_pct, per_core = 0.0, []
        core_ids = list(getattr(self._fast, '_last_cpu_ids', range(len(per_core))))
        # Fallback to psutil if /proc isn't available
        if global_pct == 0.0 and not per_core:
            try:
                global_pct = psutil.cpu_percent()
                per_core = psutil.cpu_percent(percpu=True)
            except (OSError, psutil.Error):
                global_pct, per_core = 0.0, []
            core_ids = list(range(len(per_core)))

        try:
            cpu_freq = psutil.cpu_freq()
        except (OSError, psutil.Error):
            cpu_freq = None
        freq = cpu_freq.current if cpu_freq else 0
        return {
            "total": global_pct,
            "per_core": per_core,
            "core_ids": core_ids,
            "freq": freq
        }

    def get_mem(self):
        MemInfo = namedtuple(
            'MemInfo',
            ['total', 'available', 'percent', 'used', 'free', 'buffers', 'cached'],
        )
        SwapInfo = namedtuple('SwapInfo', ['total', 'used', 'free', 'percent'])
        try:
            mem_total, mem_avail, swap_total, swap_free, buffers, cached = (
                self._fast.get_meminfo()
            )
        except (AttributeError, OSError, TypeError, ValueError, psutil.Error):
            mem_total, mem_avail, swap_total, swap_free, buffers, cached = (0,) * 6

        if mem_total == 0:
            # Fallback
            try:
                mem = psutil.virtual_memory()
                swap = psutil.swap_memory()
            except (OSError, psutil.Error):
                mem = MemInfo(0, 0, 0.0, 0, 0, 0, 0)
                swap = SwapInfo(0, 0, 0, 0.0)
            return {"mem": mem, "swap": swap}

        mem_used = mem_total - mem_avail
        mem_pct = round((mem_used / mem_total) * 100, 1) if mem_total > 0 else 0.0

        swap_free = min(swap_total, max(0, swap_free))
        swap_used = swap_total - swap_free
        swap_pct = round((swap_used / swap_total) * 100, 1) if swap_total > 0 else 0.0
        mem_free = getattr(self._fast, '_last_mem_free', None)
        if not isinstance(mem_free, (int, float)):
            mem_free = mem_avail

        mem = MemInfo(total=mem_total, available=mem_avail, percent=mem_pct, used=mem_used, free=mem_free, buffers=buffers, cached=cached)
        swap = SwapInfo(total=swap_total, used=swap_used, free=swap_free, percent=swap_pct)

        return {"mem": mem, "swap": swap}

    def get_disk(self):
        disks = []
        try:
            partitions = psutil.disk_partitions()
            for p in partitions:
                try:
                    device = getattr(p, 'device', None)
                    mountpoint = getattr(p, 'mountpoint', None)
                    if not isinstance(device, str) or not isinstance(mountpoint, str):
                        continue
                    if 'loop' in device.casefold():
                        continue
                    usage = psutil.disk_usage(mountpoint)
                    disks.append({
                        "mount": _redact_private_tokens(mountpoint),
                        "device": _public_storage_source(device),
                        "_device_name": os.path.basename(os.path.realpath(device)),
                        "filesystem": getattr(p, 'fstype', None) or None,
                        "total": usage.total, "used": usage.used,
                        "free": usage.free, "percent": usage.percent,
                    })
                except (OSError, AttributeError, TypeError, ValueError, psutil.Error):
                    continue
        except (OSError, AttributeError, TypeError, ValueError, psutil.Error):
            # A disappearing mount table must not make the whole sample fail.
            pass

        r_io, w_io = self._get_disk_io()
        devices = self._get_disk_devices()
        for partition in disks:
            partition_name = partition.pop('_device_name')
            metadata = next((
                device for device in sorted(devices, key=lambda item: -len(item['name']))
                if partition_name == device['name']
                or partition_name.startswith(device['name'] + 'p')
                or (partition_name.startswith(device['name'])
                    and partition_name[len(device['name']):].isdigit())
            ), None)
            for key in ('model', 'vendor', 'type', 'capacity'):
                partition[key] = metadata.get(key) if metadata else None

        return {
            "partitions": disks,
            "io": {"read_bytes": r_io, "write_bytes": w_io},
            "devices": devices,
            "zram": self._get_zram(),
        }

    @staticmethod
    def _is_virtual_disk_name(name):
        return isinstance(name, str) and name.startswith(('loop', 'ram', 'zram'))

    def _reset_disk_io_baseline(self, current_time):
        self._data_disk_io_prev = {}
        self._data_disk_io_time = current_time
        return 0, 0

    def _get_disk_io(self):
        """Return physical-device disk rates while keeping transient /proc failures cold.

        The fast collector historically returned an aggregate that could include
        zram and retained its previous sample if diskstats temporarily vanished.
        Keep the fast method as the fallback for lightweight test doubles and
        non-Linux callers, but use a small data-layer tracker for the normal
        FastTelemetry implementation.
        """
        fast = getattr(self, '_fast', None)
        use_data_layer = getattr(
            self, '_data_disk_io_enabled', isinstance(fast, FastTelemetry),
        )
        if not use_data_layer or not isinstance(fast, FastTelemetry):
            try:
                rates = fast.get_disk_io()
                if (isinstance(rates, (tuple, list)) and len(rates) == 2
                        and all(isinstance(rate, (int, float)) and math.isfinite(rate)
                                for rate in rates)):
                    return rates
            except (AttributeError, OSError, TypeError, ValueError, psutil.Error):
                pass
            return 0, 0

        current_time = time.monotonic()
        previous_time = getattr(self, '_data_disk_io_time', current_time)
        dt = current_time - previous_time
        if dt <= 0:
            dt = 1.0

        proc_path = getattr(fast_telemetry, 'PROC_PATH', '/proc')
        sys_path = SYS_PATH
        try:
            fingerprints = fast_telemetry._snapshot_block_fingerprints(sys_path)
        except (AttributeError, OSError, TypeError, ValueError):
            fingerprints = None
        if fingerprints is None:
            return self._reset_disk_io_baseline(current_time)

        disk_stats = {}
        try:
            with open(os.path.join(proc_path, 'diskstats'), 'rb') as stream:
                for line in stream:
                    parts = line.split()
                    if len(parts) < 14:
                        continue
                    try:
                        device_number = (int(parts[0]), int(parts[1]))
                        device_name = parts[2].decode('ascii')
                        read_sectors = int(parts[5])
                        write_sectors = int(parts[9])
                    except (UnicodeDecodeError, ValueError):
                        continue
                    if self._is_virtual_disk_name(device_name):
                        continue
                    disk_stats[device_number] = (
                        device_name, read_sectors, write_sectors,
                    )
        except (OSError, TypeError, ValueError):
            return self._reset_disk_io_baseline(current_time)

        try:
            selected_devices = fast_telemetry.select_root_block_devices(
                [values[0] for values in disk_stats.values()], sys_path,
            )
        except (OSError, TypeError, ValueError):
            selected_devices = None
        if selected_devices is None:
            return self._reset_disk_io_baseline(current_time)
        selected_devices = {
            name for name in selected_devices
            if not self._is_virtual_disk_name(name)
        }

        current_disk_rw = {}
        for device_number, (device_name, read_sectors, write_sectors) in disk_stats.items():
            if device_name not in selected_devices:
                continue
            try:
                fingerprint = fast_telemetry._block_device_fingerprint(device_name, sys_path)
            except (AttributeError, OSError, TypeError, ValueError):
                fingerprint = None
            if fingerprint is None or fingerprints.get(device_name) != fingerprint:
                return self._reset_disk_io_baseline(current_time)
            identity = (*device_number, fingerprint)
            current_disk_rw[identity] = (read_sectors, write_sectors)

        previous = getattr(self, '_data_disk_io_prev', {})
        if not hasattr(previous, 'get'):
            previous = {}
        read_rate = 0.0
        write_rate = 0.0
        for identity, (read_sectors, write_sectors) in current_disk_rw.items():
            old = previous.get(identity)
            if old is None:
                continue
            if read_sectors >= old[0]:
                read_rate += (read_sectors - old[0]) * DISK_SECTOR_BYTES / dt
            if write_sectors >= old[1]:
                write_rate += (write_sectors - old[1]) * DISK_SECTOR_BYTES / dt

        self._data_disk_io_prev = current_disk_rw
        self._data_disk_io_time = current_time
        return read_rate, write_rate

    @staticmethod
    def _read_sys_text(path):
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as stream:
                value = stream.read().strip()
        except OSError:
            return None
        return value or None

    def _get_disk_devices(self):
        now = time.monotonic()
        cached = getattr(self, '_disk_devices_cache', None)
        cached_at = getattr(self, '_disk_devices_cache_at', None)
        if cached is not None and cached_at is not None and now - cached_at < TOPOLOGY_CACHE_SECONDS:
            return cached
        root = os.path.join(SYS_PATH, 'class', 'block')
        devices = []
        try:
            names = sorted(os.listdir(root))
        except OSError:
            self._disk_devices_cache = devices
            self._disk_devices_cache_at = now
            return devices
        for name in names:
            if name.startswith(('loop', 'ram', 'zram', 'dm-')):
                continue
            base = os.path.join(root, name)
            if os.path.exists(os.path.join(base, 'partition')):
                continue
            model = self._read_sys_text(os.path.join(base, 'device', 'model'))
            vendor = self._read_sys_text(os.path.join(base, 'device', 'vendor'))
            sectors = self._read_sys_text(os.path.join(base, 'size'))
            rotational = self._read_sys_text(os.path.join(base, 'queue', 'rotational'))
            try:
                capacity = int(sectors) * 512 if sectors is not None else None
            except ValueError:
                capacity = None
            if name.startswith('nvme'):
                device_type = 'NVMe SSD'
            elif rotational == '0':
                device_type = 'SSD'
            elif rotational == '1':
                device_type = 'HDD'
            else:
                device_type = 'Block device'
            devices.append({
                'name': name, 'model': model, 'vendor': vendor,
                'type': device_type, 'capacity': capacity,
            })
        self._disk_devices_cache = devices
        self._disk_devices_cache_at = now
        return devices

    def _get_zram(self):
        root = os.path.join(SYS_PATH, 'block')
        zram = []
        try:
            names = sorted(name for name in os.listdir(root) if name.startswith('zram'))
        except OSError:
            return zram
        for name in names:
            fields = self._read_sys_text(os.path.join(root, name, 'mm_stat'))
            if not fields:
                continue
            try:
                values = [int(value) for value in fields.split()]
            except ValueError:
                continue
            if len(values) < 3:
                continue
            memory_limit = values[3] if len(values) > 3 and values[3] > 0 else None
            zram.append({
                'name': name, 'original_bytes': values[0],
                'compressed_bytes': values[1], 'memory_bytes': values[2],
                'limit_bytes': memory_limit,
            })
        return zram

    def get_net(self):
        current_time = time.monotonic()
        previous_time = getattr(self, 'last_time', current_time)
        try:
            dt = current_time - previous_time
        except TypeError:
            dt = 0.0
        interface_index = getattr(self, '_interface_index', socket.if_nametoindex)
        try:
            current = _sample_interface_counters(
                interface_index, getattr(self, '_interface_addresses', None),
            )
        except (OSError, AttributeError, TypeError, ValueError, psutil.Error):
            current = {}
        try:
            link_stats = psutil.net_if_stats()
        except (OSError, AttributeError, TypeError, psutil.Error):
            link_stats = {}
        if not hasattr(link_stats, 'get'):
            link_stats = {}
        previous = getattr(self, '_last_net_by_interface', {})
        if not hasattr(previous, 'get'):
            previous = {}
        interfaces = []
        try:
            current_items = sorted(current.items())
        except (AttributeError, TypeError, ValueError):
            current_items = []
        for item in current_items:
            try:
                identity, counters = item
                name = identity[0]
                link_identity = identity[2]
                if not isinstance(name, str) or name == 'lo':
                    continue
                bytes_recv = getattr(counters, 'bytes_recv')
                bytes_sent = getattr(counters, 'bytes_sent')
                if (not isinstance(bytes_recv, (int, float))
                        or not isinstance(bytes_sent, (int, float))
                        or not math.isfinite(bytes_recv)
                        or not math.isfinite(bytes_sent)
                        or bytes_recv < 0 or bytes_sent < 0):
                    continue
                old = previous.get(identity)
                down = 0
                up = 0
                if old is not None and link_identity is not None and dt > 0:
                    old_recv = getattr(old, 'bytes_recv', None)
                    old_sent = getattr(old, 'bytes_sent', None)
                    if (isinstance(old_recv, (int, float)) and math.isfinite(old_recv)
                            and bytes_recv >= old_recv):
                        down = (bytes_recv - old_recv) / dt
                    if (isinstance(old_sent, (int, float)) and math.isfinite(old_sent)
                            and bytes_sent >= old_sent):
                        up = (bytes_sent - old_sent) / dt
                stats = link_stats.get(name)
                speed = getattr(stats, 'speed', None)
                mtu = getattr(stats, 'mtu', None)
                interfaces.append({
                    "name": _public_interface_name(name),
                    "down": down,
                    "up": up,
                    "total_down": bytes_recv,
                    "total_up": bytes_sent,
                    "is_up": getattr(stats, 'isup', None),
                    "speed_mbps": speed if isinstance(speed, (int, float)) and speed > 0 else None,
                    "mtu": mtu if isinstance(mtu, (int, float)) and mtu > 0 else None,
                })
            except (OSError, AttributeError, IndexError, TypeError, ValueError, psutil.Error):
                continue

        self._last_net_by_interface = current
        self.last_time = current_time
        return {
            "down": sum(interface["down"] for interface in interfaces),
            "up": sum(interface["up"] for interface in interfaces),
            "total_down": sum(interface["total_down"] for interface in interfaces),
            "total_up": sum(interface["total_up"] for interface in interfaces),
            "interfaces": interfaces,
        }

    def get_gpu(self):
        drm = self._get_drm_gpus()
        nvidia = self._get_nvidia_gpus()
        if nvidia:
            drm = [gpu for gpu in drm if gpu['vendor'] != 'NVIDIA']
            drm.extend(nvidia)
        if drm:
            return drm
        return [self._gpu_record('No GPU', None, status='unavailable')]

    @staticmethod
    def _gpu_record(name, vendor, **values):
        record = {
            'name': name, 'vendor': vendor, 'load': None,
            'mem_used': None, 'mem_total': None, 'mem_pct': None,
            'temperature': None, 'power_w': None, 'fan_pct': None,
            'clock_graphics_mhz': None, 'clock_memory_mhz': None,
            'status': 'limited telemetry',
        }
        record.update(values)
        return record

    def _get_drm_devices(self):
        now = time.monotonic()
        cached = getattr(self, '_drm_devices_cache', None)
        cached_at = getattr(self, '_drm_devices_cache_at', None)
        if cached is not None and cached_at is not None and now - cached_at < TOPOLOGY_CACHE_SECONDS:
            return cached
        root = os.path.join(SYS_PATH, 'class', 'drm')
        vendors = {'0x8086': 'Intel', '0x1002': 'AMD', '0x10de': 'NVIDIA'}
        devices = []
        try:
            cards = sorted(name for name in os.listdir(root)
                           if name.startswith('card') and name[4:].isdigit())
        except OSError:
            cards = []
        for card in cards:
            path = os.path.join(root, card, 'device')
            vendor_id = (self._read_sys_text(os.path.join(path, 'vendor')) or '').casefold()
            vendor = vendors.get(vendor_id, vendor_id or 'Unknown')
            devices.append((card, path, vendor))
        self._drm_devices_cache = devices
        self._drm_devices_cache_at = now
        return devices

    def _get_drm_gpus(self):
        gpus = []
        for card, path, vendor in self._get_drm_devices():
            load_text = self._read_sys_text(os.path.join(path, 'gpu_busy_percent'))
            try:
                load = float(load_text) if load_text is not None else None
            except ValueError:
                load = None
            if load is not None and not math.isfinite(load):
                load = None
            clock = None
            if vendor == 'Intel':
                card_path = os.path.join(SYS_PATH, 'class', 'drm', card)
                for relative_path in ('gt_cur_freq_mhz',
                                      os.path.join('gt', 'gt0', 'rps_cur_freq_mhz')):
                    clock_text = self._read_sys_text(os.path.join(card_path, relative_path))
                    try:
                        clock = float(clock_text) if clock_text is not None else None
                    except ValueError:
                        clock = None
                    if clock is not None and math.isfinite(clock) and clock >= 0:
                        break
                    clock = None
            name = f'{vendor} GPU'
            if load is not None:
                status = 'available'
            elif clock is not None:
                status = 'frequency only'
            else:
                status = 'limited telemetry'
            gpus.append(self._gpu_record(
                name, vendor, load=load, clock_graphics_mhz=clock, status=status,
            ))
        return gpus

    @staticmethod
    def _optional_float(value):
        if not value or value.casefold() in {'n/a', 'na', '[not supported]', 'not supported'}:
            return None
        try:
            number = float(value)
        except ValueError:
            return None
        return number if math.isfinite(number) else None

    def _get_nvidia_gpus(self):
        now = time.monotonic()
        cached_at = getattr(self, '_nvidia_cache_at', None)
        if cached_at is not None and now - cached_at < 2.0:
            return getattr(self, '_nvidia_cache', [])
        if now < getattr(self, '_nvidia_retry_at', 0.0):
            if cached_at is not None and now - cached_at <= NVIDIA_STALE_SECONDS:
                return [dict(gpu, status='stale telemetry')
                        for gpu in getattr(self, '_nvidia_cache', [])]
            self._nvidia_cache = []
            return []
        fields = (
            'name,utilization.gpu,memory.used,memory.total,temperature.gpu,'
            'power.draw,fan.speed,clocks.gr,clocks.mem'
        )
        try:
            output = subprocess.check_output(
                ['nvidia-smi', f'--query-gpu={fields}', '--format=csv,noheader,nounits'],
                stderr=subprocess.DEVNULL, timeout=0.8,
            ).decode('utf-8', errors='replace')
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            output = ''
        gpus = []
        for line in output.splitlines():
            parts = [part.strip() for part in line.split(',')]
            if len(parts) != 9 or not parts[0]:
                continue
            values = [self._optional_float(value) for value in parts[1:]]
            load, mem_used, mem_total, temperature, power, fan, graphics, memory = values
            mem_pct = None
            if mem_used is not None and mem_total is not None and mem_total > 0:
                mem_pct = round(mem_used / mem_total * 100, 1)
            status = 'available' if load is not None else 'sleeping or unavailable'
            gpus.append(self._gpu_record(
                parts[0], 'NVIDIA', load=load, mem_used=mem_used,
                mem_total=mem_total, mem_pct=mem_pct, temperature=temperature,
                power_w=power, fan_pct=fan, clock_graphics_mhz=graphics,
                clock_memory_mhz=memory, status=status,
            ))
        if gpus:
            self._nvidia_cache_at = now
            self._nvidia_cache = gpus
            self._nvidia_failures = 0
            self._nvidia_retry_at = now
            return gpus

        failures = getattr(self, '_nvidia_failures', 0) + 1
        self._nvidia_failures = failures
        delay = 0.0 if failures == 1 else min(10.0, 2.0 ** (failures - 1))
        self._nvidia_retry_at = now + delay
        if cached_at is not None and now - cached_at <= NVIDIA_STALE_SECONDS:
            return [dict(gpu, status='stale telemetry')
                    for gpu in getattr(self, '_nvidia_cache', [])]
        self._nvidia_cache = []
        return []

    def get_procs(self, sort_by="cpu_percent", filter_str="", tree=True, limit=50):
        # We need total memory for memory_percent calculation
        try:
            mem_info = self.get_mem()["mem"]
            total_mem = mem_info.total
        except Exception:
            total_mem = 0

        try:
            procs = self._fast.get_procs(total_mem_bytes=total_mem)
        except (OSError, TypeError, ValueError, psutil.Error):
            # A restricted procfs is expected in some containers.  Fall back
            # to psutil rather than losing the complete process panel.
            procs = []

        # Fallback to psutil if empty (e.g. not on Linux)
        if not procs:
            procs = []
            try:
                process_iter = psutil.process_iter([
                        'pid', 'ppid', 'name', 'username', 'cpu_percent',
                        'memory_percent', 'cmdline', 'memory_info'])
                for index, p in enumerate(process_iter):
                    if index >= MAX_FALLBACK_PROCESS_COUNT:
                        break
                    try:
                        info = p.info
                        uid = None
                        try:
                            uid = p.uids().real
                        except (AttributeError, NotImplementedError, psutil.NoSuchProcess,
                                psutil.AccessDenied, psutil.ZombieProcess):
                            pass
                        username = info.get('username')
                        if not isinstance(username, str) or not username:
                            username = str(uid) if uid is not None else 'Unavailable'
                        cmdline_list = info.get('cmdline')
                        if isinstance(cmdline_list, (list, tuple)):
                            cmdline = ' '.join(str(argument) for argument in cmdline_list)
                        elif isinstance(cmdline_list, str):
                            cmdline = cmdline_list
                        else:
                            cmdline = ''
                        name = info.get('name')
                        if not isinstance(name, str) or not name:
                            name = 'unknown'
                        memory_info = info.get('memory_info')
                        rss = getattr(memory_info, 'rss', 0)
                        if not isinstance(rss, (int, float)):
                            rss = 0
                        cmdline = cmdline or name
                        if len(cmdline) > MAX_PROCESS_CMDLINE_CHARS:
                            cmdline = cmdline[:MAX_PROCESS_CMDLINE_CHARS - 3].rstrip() + '...'
                        procs.append({
                            'pid': info['pid'],
                            'ppid': info.get('ppid'),
                            'name': name,
                            'uid': uid,
                            'start_time': None,
                            'username': username,
                            'cmdline': cmdline,
                            'cpu_percent': info.get('cpu_percent') or 0.0,
                            'memory_percent': info.get('memory_percent') or 0.0,
                            'rss': rss,
                        })
                    except (
                        AttributeError, KeyError, OSError,
                        psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess,
                    ):
                        pass
            except (AttributeError, KeyError, OSError, psutil.Error, TypeError, ValueError):
                pass

        return prepare_processes(procs, sort_by, filter_str, tree, limit)

    @staticmethod
    def _parse_listening_tcp(lines):
        """Parse listening TCP rows independently so one bad row is harmless."""
        connections = []
        for line in lines:
            try:
                parts = line.split()
                if not parts or parts[0].rstrip(':').casefold() == 'sl':
                    continue
                if len(parts) < 4 or parts[3].casefold() != '0a':
                    continue
                endpoint = parts[1]
                if isinstance(endpoint, bytes):
                    endpoint = endpoint.decode('ascii')
                port = int(endpoint.rsplit(':', 1)[1], 16)
                if not 0 <= port <= 65535:
                    continue
            except (AttributeError, IndexError, TypeError, UnicodeDecodeError, ValueError):
                continue
            connections.append({'port': port, 'proto': 'TCP'})
            if len(connections) >= MAX_CONNECTIONS:
                break
        return connections

    def get_connections(self):
        fast = getattr(self, '_fast', None)
        if not isinstance(fast, FastTelemetry):
            # Keep lightweight collector doubles and non-standard backends
            # compatible with the original delegation behavior.
            try:
                return fast.get_connections()
            except (AttributeError, OSError, TypeError, ValueError, psutil.Error):
                return []

        proc_path = getattr(fast_telemetry, 'PROC_PATH', '/proc')
        try:
            with open(os.path.join(proc_path, 'net', 'tcp'), 'r',
                      encoding='ascii', errors='replace') as stream:
                return self._parse_listening_tcp(stream)
        except (OSError, TypeError, ValueError):
            return []

    def get_sys_info(self):
        return self._fast.get_sys_info()

    def close(self):
        """Release the long-lived procfs readers owned by this collector."""
        fast = getattr(self, '_fast', None)
        close = getattr(fast, 'close', None)
        if callable(close):
            close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
