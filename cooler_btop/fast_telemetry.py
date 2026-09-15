import math
import os
import platform
import pwd
import time

PROC_PATH = os.environ.get('HOST_PROC', '/proc')
SYS_PATH = os.environ.get('HOST_SYS', '/sys')
HOST_ROOT = os.environ.get('HOST_ROOT', '/')


def select_root_block_devices(devices, sys_path=SYS_PATH):
    """Return whole physical devices without partitions or sysfs slaves."""
    unique_devices = list(dict.fromkeys(devices))
    block_root = os.path.join(sys_path, 'class', 'block')
    if not os.path.isdir(block_root):
        return None

    roots = []
    for device in unique_devices:
        block_path = os.path.join(sys_path, 'class', 'block', device)
        try:
            entries = os.listdir(block_path)
        except OSError:
            return None
        if 'partition' in entries:
            continue
        if 'slaves' not in entries:
            return None
        try:
            has_slaves = bool(os.listdir(os.path.join(block_path, 'slaves')))
        except OSError:
            return None
        if not has_slaves:
            roots.append(device)
    return roots


def _read_optional_device_id(path):
    try:
        with open(path, 'rb') as stream:
            return stream.read().strip()
    except FileNotFoundError:
        return None


def _block_device_fingerprint(device, sys_path=SYS_PATH):
    block_path = os.path.join(sys_path, 'class', 'block', device)
    try:
        model = _read_optional_device_id(os.path.join(block_path, 'device', 'model'))
        disk_sequence = _read_optional_device_id(os.path.join(block_path, 'diskseq'))
    except OSError:
        return None
    if disk_sequence is None or not disk_sequence.isdigit():
        return None
    return os.path.realpath(block_path), disk_sequence, model


def _snapshot_block_fingerprints(sys_path=SYS_PATH):
    root = os.path.join(sys_path, 'class', 'block')
    try:
        names = os.listdir(root)
    except OSError:
        return None
    return {name: _block_device_fingerprint(name, sys_path) for name in names}


class FastTelemetry:
    def __init__(self):
        # Open in unbuffered binary mode
        try:
            self._stat_file = open(f'{PROC_PATH}/stat', 'rb', buffering=0)
            self._mem_file = open(f'{PROC_PATH}/meminfo', 'rb', buffering=0)
        except Exception:
            self._stat_file = None
            self._mem_file = None


        self._mem_buf = bytearray(2048)

        self._last_cpu_total = 0
        self._last_cpu_idle = 0

        self._last_per_core_total = {}
        self._last_per_core_idle = {}
        self._last_cpu_ids = []

        # Process tracking
        self.clock_ticks = os.sysconf("SC_CLK_TCK")
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        self._prev_proc_cpu = {}
        self._prev_proc_time = time.monotonic()

        # Disk IO tracking
        self._prev_disk_rw = {}
        self._prev_disk_time = time.monotonic()

        self._specs_cache = None

    @staticmethod
    def _read_text(path):
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as stream:
                value = stream.read().strip()
        except OSError:
            return None
        return value or None

    @classmethod
    def _read_number(cls, path, scale=1.0):
        value = cls._read_text(path)
        if value is None:
            return None
        try:
            number = float(value) / scale
        except ValueError:
            return None
        return number if math.isfinite(number) else None

    def _get_specs(self):
        if self._specs_cache is not None:
            return self._specs_cache

        os_name = None
        release = self._read_text(os.path.join(HOST_ROOT, 'etc', 'os-release'))
        release_fields = {}
        if release:
            for line in release.splitlines():
                if '=' not in line or line.lstrip().startswith('#'):
                    continue
                key, value = line.split('=', 1)
                release_fields[key] = value.strip().strip('"\'')
            os_name = release_fields.get('PRETTY_NAME') or release_fields.get('NAME')

        dmi_root = os.path.join(SYS_PATH, 'class', 'dmi', 'id')
        dmi = {
            key: self._read_text(os.path.join(dmi_root, filename))
            for key, filename in {
                'vendor': 'sys_vendor', 'product_model': 'product_name',
                'product_version': 'product_version', 'bios_version': 'bios_version',
                'bios_date': 'bios_date',
            }.items()
        }
        chassis_code = self._read_text(os.path.join(dmi_root, 'chassis_type'))
        chassis_types = {
            '3': 'Desktop', '4': 'Low Profile Desktop', '6': 'Mini Tower',
            '7': 'Tower', '8': 'Portable', '9': 'Laptop', '10': 'Notebook',
            '11': 'Hand Held', '13': 'All in One', '14': 'Sub Notebook',
            '17': 'Main Server', '23': 'Rack Mount', '30': 'Tablet',
            '31': 'Convertible', '32': 'Detachable', '35': 'Mini PC',
        }

        cpu_model = None
        cpuinfo_records = []
        physical_cores = set()
        complete_topology_records = 0
        cpuinfo = self._read_text(os.path.join(PROC_PATH, 'cpuinfo'))
        if cpuinfo:
            current = {}
            for line in cpuinfo.splitlines() + ['']:
                if not line.strip():
                    if current:
                        cpuinfo_records.append(current)
                        current = {}
                    continue
                if ':' in line:
                    key, value = line.split(':', 1)
                    current[key.strip().casefold()] = value.strip()
            for processor in cpuinfo_records:
                cpu_model = cpu_model or next((
                    processor.get(field) for field in
                    ('model name', 'cpu', 'hardware', 'machine', 'uarch')
                    if processor.get(field)
                ), None)

        processors = [
            record for record in cpuinfo_records
            if (record.get('processor') or '').isdigit()
        ]
        for processor in processors:
            physical_id = processor.get('physical id')
            core_id = processor.get('core id')
            if physical_id is None or core_id is None:
                continue
            complete_topology_records += 1
            physical_cores.add((physical_id, core_id))

        cpu_ids = []
        for index, processor in enumerate(processors):
            cpu_id = processor.get('processor', str(index))
            cpu_ids.append(cpu_id if cpu_id.isdigit() else str(index))
        if not cpu_ids:
            cpu_root = os.path.join(SYS_PATH, 'devices', 'system', 'cpu')
            try:
                cpu_ids = sorted(
                    name[3:] for name in os.listdir(cpu_root)
                    if name.startswith('cpu') and name[3:].isdigit()
                )
            except OSError:
                cpu_ids = []

        if not processors or complete_topology_records != len(processors):
            physical_cores = set()
            for cpu_id in cpu_ids:
                topology = os.path.join(
                    SYS_PATH, 'devices', 'system', 'cpu', f'cpu{cpu_id}', 'topology',
                )
                package_id = self._read_text(os.path.join(topology, 'physical_package_id'))
                core_id = self._read_text(os.path.join(topology, 'core_id'))
                if package_id is not None and core_id is not None:
                    physical_cores.add((package_id, core_id))
        logical_count = len(processors) or len(cpu_ids) or os.cpu_count()
        architecture = platform.machine() or None
        physical_count = len(physical_cores) or None
        self._specs_cache = {
            'os': os_name,
            'architecture': architecture,
            'chassis': chassis_types.get(chassis_code, chassis_code),
            **dmi,
            'cpu_model': cpu_model or architecture,
            'logical_cores': logical_count,
            'physical_cores': physical_count,
        }
        return self._specs_cache

    @staticmethod
    def _sensor_category(driver):
        if driver in {'coretemp', 'k10temp', 'zenpower'}:
            return 'CPU'
        if driver == 'nvme':
            return 'NVMe'
        if driver.startswith('spd'):
            return 'DIMM'
        if driver.startswith('iwlwifi'):
            return 'Wi-Fi'
        if driver in {'amdgpu', 'nouveau'}:
            return 'GPU'
        return None

    @staticmethod
    def _sensor_label(category, driver, label, index):
        clean = (label or '').replace('_', ' ').strip()
        if category == 'CPU':
            if 'package' in clean.casefold():
                suffix = clean.rsplit(' ', 1)[-1] if clean[-1:].isdigit() else ''
                return f"CPU Package {suffix}".rstrip()
            if clean.casefold() in {'tdie', 'tctl'}:
                return f"CPU {clean.capitalize()}"
        if category == 'NVMe':
            return f"NVMe {clean or 'Temperature'}"
        if category == 'DIMM':
            return f"DIMM {index}"
        if category == 'Wi-Fi':
            return 'Wi-Fi Temperature'
        if category == 'GPU':
            return f"GPU {clean or 'Temperature'}"
        return clean or driver or f"Sensor {index}"

    def _get_sensors(self):
        root = os.path.join(SYS_PATH, 'class', 'hwmon')
        sensors = []
        core_values = []
        try:
            entries = sorted(name for name in os.listdir(root) if name.startswith('hwmon'))
        except OSError:
            return sensors
        for hwmon in entries:
            base = os.path.join(root, hwmon)
            driver = (self._read_text(os.path.join(base, 'name')) or '').casefold()
            try:
                filenames = sorted(os.listdir(base))
            except OSError:
                continue
            category = self._sensor_category(driver)
            for filename in filenames:
                if filename.startswith('temp') and filename.endswith('_input') and category:
                    sensor_id = filename[:-6]
                    label = self._read_text(os.path.join(base, sensor_id + '_label'))
                    value = self._read_number(os.path.join(base, filename), 1000.0)
                    if value is None or not -100 <= value <= 250:
                        continue
                    if category == 'CPU' and label and label.casefold().startswith('core '):
                        core_values.append(value)
                        continue
                    sensors.append({
                        'label': self._sensor_label(category, driver, label, len(sensors) + 1),
                        'category': category, 'type': 'temperature',
                        'value': round(value, 1), 'unit': 'C',
                    })
                elif filename.startswith('fan') and filename.endswith('_input'):
                    sensor_id = filename[:-6]
                    value = self._read_number(os.path.join(base, filename))
                    if value is None or value < 0:
                        continue
                    raw_label = self._read_text(os.path.join(base, sensor_id + '_label'))
                    words = (raw_label or f"{driver} fan {sensor_id[3:]}").replace('_', ' ').split()
                    label = ' '.join(
                        word.upper() if word.casefold() in {'cpu', 'gpu'} else word.casefold()
                        for word in words
                    )
                    sensors.append({
                        'label': label, 'category': 'Fan', 'type': 'fan',
                        'value': round(value, 1), 'unit': 'RPM',
                    })
        if core_values:
            sensors.append({
                'label': 'CPU Cores (max)', 'category': 'CPU', 'type': 'temperature',
                'value': round(max(core_values), 1), 'unit': 'C',
            })
        order = {'CPU': 0, 'GPU': 1, 'NVMe': 2, 'DIMM': 3, 'Wi-Fi': 4, 'Fan': 5}
        sensors.sort(key=lambda sensor: (order.get(sensor['category'], 9), sensor['label']))
        return sensors[:24]

    def _get_battery_info(self):
        root = os.path.join(SYS_PATH, 'class', 'power_supply')
        batteries = []
        try:
            supplies = sorted(os.listdir(root))
        except OSError:
            return batteries
        for name in supplies:
            base = os.path.join(root, name)
            supply_type = self._read_text(os.path.join(base, 'type'))
            if supply_type != 'Battery' and not name.startswith('BAT'):
                continue
            percentage = self._read_number(os.path.join(base, 'capacity'))
            status = self._read_text(os.path.join(base, 'status'))
            voltage = self._read_number(os.path.join(base, 'voltage_now'), 1_000_000.0)
            power = self._read_number(os.path.join(base, 'power_now'), 1_000_000.0)
            energy_now = self._read_number(os.path.join(base, 'energy_now'), 1_000_000.0)
            energy_full = self._read_number(os.path.join(base, 'energy_full'), 1_000_000.0)
            charge_now = self._read_number(os.path.join(base, 'charge_now'), 1_000_000.0)
            charge_full = self._read_number(os.path.join(base, 'charge_full'), 1_000_000.0)
            current = self._read_number(os.path.join(base, 'current_now'), 1_000_000.0)
            if voltage is not None:
                if energy_now is None and charge_now is not None:
                    energy_now = charge_now * voltage
                if energy_full is None and charge_full is not None:
                    energy_full = charge_full * voltage
                if power is None and current is not None:
                    power = current * voltage
            seconds = None
            if power and power > 0:
                if status and status.casefold() == 'charging' and energy_full is not None and energy_now is not None:
                    seconds = round(max(0.0, energy_full - energy_now) / power * 3600)
                elif status and status.casefold() == 'discharging' and energy_now is not None:
                    seconds = round(energy_now / power * 3600)
            batteries.append({
                'name': name, 'percentage': percentage, 'status': status,
                'power_w': None if power is None else round(power, 2),
                'energy_now_wh': None if energy_now is None else round(energy_now, 2),
                'energy_full_wh': None if energy_full is None else round(energy_full, 2),
                'voltage_v': None if voltage is None else round(voltage, 2),
                'time_remaining_s': seconds,
            })
        return batteries

    def get_disk_io(self):
        current_time = time.monotonic()
        dt = current_time - self._prev_disk_time
        if dt <= 0: dt = 1.0

        disk_stats = {}
        fingerprints_before = _snapshot_block_fingerprints(SYS_PATH)
        if fingerprints_before is None:
            self._prev_disk_rw = {}
            self._prev_disk_time = current_time
            return 0, 0

        try:
            with open(f'{PROC_PATH}/diskstats', 'rb') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 14:
                        # Fields 0:1 identify the device; field 2 is its current name.
                        # A sector is typically 512 bytes
                        try:
                            identity = (int(parts[0]), int(parts[1]))
                            dev = parts[2].decode('ascii')
                            r_sec = int(parts[5])
                            w_sec = int(parts[9])
                        except (UnicodeDecodeError, ValueError):
                            continue
                        if dev.startswith('loop') or dev.startswith('ram'):
                            continue

                        disk_stats[identity] = (dev, r_sec, w_sec)

            dr = 0
            dw = 0
            selected_devices = select_root_block_devices(
                [values[0] for values in disk_stats.values()], SYS_PATH,
            )
            if selected_devices is None:
                self._prev_disk_rw = {}
                self._prev_disk_time = current_time
                return 0, 0

            selected_devices = set(selected_devices)
            current_disk_rw = {}
            for device_number, (dev, read_sectors, write_sectors) in disk_stats.items():
                if dev not in selected_devices:
                    continue
                fingerprint = _block_device_fingerprint(dev, SYS_PATH)
                if fingerprint is None or fingerprints_before.get(dev) != fingerprint:
                    self._prev_disk_rw = {}
                    self._prev_disk_time = current_time
                    return 0, 0
                identity = (*device_number, fingerprint)
                current_disk_rw[identity] = (read_sectors, write_sectors)
            for identity, (read_sectors, write_sectors) in current_disk_rw.items():
                previous = self._prev_disk_rw.get(identity)
                if previous is None:
                    continue
                if read_sectors >= previous[0]:
                    dr += (read_sectors - previous[0]) * 512 / dt
                if write_sectors >= previous[1]:
                    dw += (write_sectors - previous[1]) * 512 / dt

            self._prev_disk_rw = current_disk_rw
            self._prev_disk_time = current_time

            return dr, dw

        except FileNotFoundError:
            return 0, 0

    def get_procs(self, total_mem_bytes=None):
        current_time = time.monotonic()
        dt = current_time - self._prev_proc_time
        if dt <= 0: dt = 1.0

        procs = []
        new_proc_cpu = {}

        try:
            for pid_str in os.listdir(PROC_PATH):
                if not pid_str.isdigit():
                    continue

                try:
                    with open(f'{PROC_PATH}/{pid_str}/stat', 'rb') as f_stat, \
                         open(f'{PROC_PATH}/{pid_str}/statm', 'rb') as f_statm:

                        stat_data = f_stat.read()
                        statm_data = f_statm.read()

                        lparen = stat_data.find(b'(')
                        rparen = stat_data.rfind(b')')
                        if lparen < 0 or rparen <= lparen:
                            continue

                        name = stat_data[lparen+1:rparen].decode('utf-8', errors='replace')
                        rest = stat_data[rparen+2:].split()
                        if len(rest) < 13:
                            continue

                        utime = int(rest[11])
                        stime = int(rest[12])
                        ppid = int(rest[1])
                        start_time = int(rest[19]) if len(rest) > 19 else None

                        cpu_time = (utime + stime) / self.clock_ticks

                        cpu_percent = 0.0
                        previous = self._prev_proc_cpu.get(pid_str)
                        if previous is not None and start_time is not None and previous[1] == start_time:
                            delta_c = cpu_time - previous[0]
                            if delta_c > 0 and dt > 0:
                                cpu_percent = (delta_c / dt) * 100.0

                        statm_fields = statm_data.split()
                        rss_pages = int(statm_fields[1])
                        memory_rss_bytes = rss_pages * self.page_size

                        mem_percent = 0.0
                        if total_mem_bytes and total_mem_bytes > 0:
                            mem_percent = (memory_rss_bytes / total_mem_bytes) * 100.0

                        uid = None
                        try:
                            with open(f'{PROC_PATH}/{pid_str}/status', 'rb') as f_status:
                                for status_line in f_status:
                                    if status_line.startswith(b'Uid:'):
                                        uid = int(status_line.split()[1])
                                        break
                        except (OSError, ValueError, IndexError):
                            pass

                        username = 'Unavailable'
                        if uid is not None:
                            try:
                                username = pwd.getpwuid(uid).pw_name
                            except (KeyError, OverflowError, OSError):
                                username = str(uid)

                        cmdline = name
                        try:
                            with open(f'{PROC_PATH}/{pid_str}/cmdline', 'rb') as f_cmdline:
                                arguments = [part for part in f_cmdline.read().split(b'\0') if part]
                            if arguments:
                                cmdline = ' '.join(part.decode('utf-8', errors='replace') for part in arguments)
                        except OSError:
                            pass

                        with open(f'{PROC_PATH}/{pid_str}/stat', 'rb') as f_stat:
                            current_stat = f_stat.read()
                        current_rparen = current_stat.rfind(b')')
                        if current_rparen < 0:
                            continue
                        current_rest = current_stat[current_rparen+2:].split()
                        if len(current_rest) <= 19 or int(current_rest[19]) != start_time:
                            continue

                        new_proc_cpu[pid_str] = (cpu_time, start_time)

                        procs.append({
                            'pid': int(pid_str),
                            'ppid': ppid,
                            'name': name,
                            'uid': uid,
                            'start_time': start_time,
                            'username': username,
                            'cmdline': cmdline,
                            'cpu_percent': round(cpu_percent, 1),
                            'memory_percent': round(mem_percent, 1),
                            'rss': memory_rss_bytes
                        })
                except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError):
                    continue

        except FileNotFoundError:
            pass

        self._prev_proc_cpu = new_proc_cpu
        self._prev_proc_time = current_time

        return procs

    def get_connections(self):
        conns = []
        try:
            with open(f'{PROC_PATH}/net/tcp', 'r') as f:
                lines = f.readlines()[1:] # skip header
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 10:
                        local_ip_port = parts[1]
                        state = parts[3]

                        if state == '0A': # LISTEN state
                            ip_hex, port_hex = local_ip_port.split(':')
                            port = int(port_hex, 16)
                            conns.append({'port': port, 'proto': 'TCP'})
            # Just take top 10 to keep it lightweight
            return conns[:10]
        except (OSError, ValueError, IndexError):
            return []

    def _get_cpu_temperature(self, sensors=None):
        if sensors is not None:
            cpu_temperatures = [
                sensor for sensor in sensors
                if sensor['category'] == 'CPU' and sensor['type'] == 'temperature'
            ]
            priorities = ('CPU Tdie', 'CPU Package', 'CPU Tctl', 'CPU Cores')
            for prefix in priorities:
                matches = [sensor for sensor in cpu_temperatures
                           if sensor['label'].startswith(prefix)]
                if matches:
                    return sorted(matches, key=lambda sensor: sensor['label'])[0]['value']

        hwmon_root = f'{SYS_PATH}/class/hwmon'
        hwmon_candidates = []
        try:
            for hwmon in sorted(os.listdir(hwmon_root)):
                if not hwmon.startswith('hwmon'):
                    continue
                base = f'{hwmon_root}/{hwmon}'
                try:
                    with open(f'{base}/name', 'rb') as f_name:
                        driver = f_name.read().strip().lower()
                except OSError:
                    driver = b''
                try:
                    filenames = sorted(os.listdir(base))
                except OSError:
                    continue
                for filename in filenames:
                    if not filename.startswith('temp') or not filename.endswith('_input'):
                        continue
                    sensor = filename[:-6]
                    try:
                        with open(f'{base}/{sensor}_label', 'rb') as f_label:
                            label = f_label.read().strip().lower()
                    except OSError:
                        label = b''
                    if driver == b'k10temp' and label == b'tdie':
                        priority = (0, 0)
                    elif driver == b'k10temp' and label == b'tctl':
                        priority = (0, 1)
                    elif driver == b'coretemp' and b'package' in label:
                        try:
                            package_id = int(label.rsplit(maxsplit=1)[-1])
                        except ValueError:
                            package_id = 9999
                        priority = (1, package_id)
                    else:
                        continue
                    try:
                        with open(f'{base}/{filename}', 'rb') as f_input:
                            temperature = float(f_input.read().strip()) / 1000.0
                    except (OSError, ValueError):
                        continue
                    if not math.isfinite(temperature):
                        continue
                    hwmon_candidates.append((*priority, hwmon, filename, temperature))
        except (OSError, ValueError):
            pass
        if hwmon_candidates:
            return min(hwmon_candidates)[-1]

        thermal_root = f'{SYS_PATH}/class/thermal'
        candidates = []
        try:
            for zone in sorted(os.listdir(thermal_root)):
                if not zone.startswith('thermal_zone'):
                    continue
                try:
                    with open(f'{thermal_root}/{zone}/type', 'rb') as f_type:
                        ztype = f_type.read().strip().lower()
                    with open(f'{thermal_root}/{zone}/temp', 'rb') as f_temp:
                        temperature = float(f_temp.read().strip()) / 1000.0
                except (OSError, ValueError):
                    continue
                if not math.isfinite(temperature):
                    continue
                if b'x86_pkg_temp' in ztype or b'cpu' in ztype:
                    priority = 0
                elif b'acpitz' in ztype:
                    priority = 1
                else:
                    continue
                candidates.append((priority, zone, temperature))
        except OSError:
            pass
        if candidates:
            return sorted(candidates)[0][2]
        return 0.0

    def get_sys_info(self):
        try:
            with open(f'{PROC_PATH}/uptime', 'rb') as f:
                uptime_s = float(f.read().split()[0])

            with open(f'{PROC_PATH}/sys/kernel/hostname', 'rb') as f:
                hostname = f.read().strip().decode('ascii')
            with open(f'{PROC_PATH}/sys/kernel/osrelease', 'rb') as f:
                kernel = f.read().strip().decode('ascii')

            procs_running = 0
            forks = 0
            with open(f'{PROC_PATH}/stat', 'rb') as f:
                for line in f:
                    if line.startswith(b'procs_running'):
                        procs_running = int(line.split()[1])
                    elif line.startswith(b'processes'):
                        forks = int(line.split()[1])
            procs_total = sum(
                pid.isdigit() and os.path.isdir(f'{PROC_PATH}/{pid}')
                for pid in os.listdir(PROC_PATH)
            )

            sensors = self._get_sensors()
            cpu_temp = self._get_cpu_temperature(sensors)

            battery_info = self._get_battery_info()
            battery = "N/A"
            for item in battery_info:
                if item['percentage'] is not None:
                    percentage = f"{item['percentage']:g}%"
                    battery = f"{percentage} ({item['status'] or 'Unknown'})"
                    break

            # Get Open File Descriptors
            open_fds = 0
            max_fds = 0
            try:
                with open(f'{PROC_PATH}/sys/fs/file-nr', 'rb') as f:
                    parts = f.read().split()
                    open_fds = int(parts[0])
                    max_fds = int(parts[2])
            except (OSError, ValueError, IndexError):
                pass

            # Get Load Average
            load_avg = ""
            try:
                with open(f'{PROC_PATH}/loadavg', 'rb') as f:
                    parts = f.read().split()
                    load_avg = f"{parts[0].decode('ascii')} {parts[1].decode('ascii')} {parts[2].decode('ascii')}"
            except (OSError, UnicodeError, IndexError):
                pass

            m, s = divmod(uptime_s, 60)
            h, m = divmod(m, 60)
            d, h = divmod(h, 24)
            uptime_str = f"{int(d)}d {int(h)}h {int(m)}m"

            return {
                "hostname": hostname,
                "kernel": kernel,
                "uptime": uptime_str,
                "procs_running": procs_running,
                "procs_total": procs_total,
                "forks": forks,
                "cpu_temp": round(cpu_temp, 1),
                "battery": battery,
                "battery_info": battery_info,
                "sensors": sensors,
                "specs": self._get_specs(),
                "open_fds": open_fds,
                "max_fds": max_fds,
                "load_avg": load_avg
            }
        except (OSError, ValueError, UnicodeError, IndexError):
            return {
                "hostname": "unknown",
                "kernel": "unknown",
                "uptime": "0m",
                "procs_running": 0,
                "procs_total": 0,
                "forks": 0,
                "cpu_temp": 0.0,
                "battery": "N/A",
                "battery_info": [],
                "sensors": [],
                "specs": self._get_specs(),
                "open_fds": 0,
                "max_fds": 0,
                "load_avg": "0.0 0.0 0.0"
            }

    def __del__(self):
        try:
            if getattr(self, '_stat_file', None):
                self._stat_file.close()
            if getattr(self, '_mem_file', None):
                self._mem_file.close()
        except Exception:
            pass

    def get_cpu_percent(self):
        if not self._stat_file:
            return 0.0, []

        self._stat_file.seek(0)
        per_core_percentages = []
        current_core_ids = []
        global_pct = 0.0

        for line in self._stat_file:
            fields = line.split()
            if not fields or (fields[0] != b'cpu' and not fields[0].startswith(b'cpu')):
                continue
            try:
                values = [int(value) for value in fields[1:11]]
            except ValueError:
                continue
            if len(values) < 4:
                continue
            while len(values) < 8:
                values.append(0)

            # guest and guest_nice are already included in user and nice.
            idle = values[3] + values[4]
            total = max(0, sum(values[:8]) - sum(values[8:10]))
            is_global = fields[0] == b'cpu'

            if is_global:
                idle_delta = idle - self._last_cpu_idle
                total_delta = total - self._last_cpu_total
                self._last_cpu_idle = idle
                self._last_cpu_total = total
                if total_delta > 0:
                    global_pct = min(100.0, max(0.0, 100.0 * (total_delta - idle_delta) / total_delta))
            else:
                try:
                    core_id = int(fields[0][3:])
                except ValueError:
                    continue

                current_core_ids.append(core_id)
                previous_idle = self._last_per_core_idle.get(core_id)
                previous_total = self._last_per_core_total.get(core_id)
                self._last_per_core_idle[core_id] = idle
                self._last_per_core_total[core_id] = total
                if previous_idle is not None and previous_total is not None:
                    idle_delta = idle - previous_idle
                    total_delta = total - previous_total
                else:
                    total_delta = 0
                if total_delta > 0:
                    pct = min(100.0, max(0.0, 100.0 * (total_delta - idle_delta) / total_delta))
                    per_core_percentages.append(pct)
                else:
                    per_core_percentages.append(0.0)

        self._last_per_core_idle = {
            core_id: self._last_per_core_idle[core_id] for core_id in current_core_ids
        }
        self._last_per_core_total = {
            core_id: self._last_per_core_total[core_id] for core_id in current_core_ids
        }
        self._last_cpu_ids = current_core_ids
        return round(global_pct, 1), [round(p, 1) for p in per_core_percentages]

    def get_meminfo(self):
        if not self._mem_file:
            self._last_mem_free = None
            return 0, 0, 0, 0, 0, 0

        self._mem_file.seek(0)
        n = self._mem_file.readinto(self._mem_buf)
        if n == 0:
            self._last_mem_free = None
            return 0, 0, 0, 0, 0, 0

        mem_total = 0
        mem_available = 0
        mem_free = None
        swap_total = 0
        swap_free = 0
        buffers = 0
        cached = 0

        cursor = 0
        while cursor < n:
            if self._mem_buf[cursor:cursor+8] == b'MemTotal':
                mem_total = self._parse_mem_val(cursor, n)
            elif self._mem_buf[cursor:cursor+12] == b'MemAvailable':
                mem_available = self._parse_mem_val(cursor, n)
            elif self._mem_buf[cursor:cursor+7] == b'MemFree':
                mem_free = self._parse_mem_val(cursor, n)
            elif self._mem_buf[cursor:cursor+9] == b'SwapTotal':
                swap_total = self._parse_mem_val(cursor, n)
            elif self._mem_buf[cursor:cursor+8] == b'SwapFree':
                swap_free = self._parse_mem_val(cursor, n)
            elif self._mem_buf[cursor:cursor+7] == b'Buffers':
                buffers = self._parse_mem_val(cursor, n)
            elif self._mem_buf[cursor:cursor+6] == b'Cached':
                cached = self._parse_mem_val(cursor, n)

            # fast forward to next line
            while cursor < n and self._mem_buf[cursor] != 10:
                cursor += 1
            cursor += 1

        self._last_mem_free = None if mem_free is None else mem_free * 1024
        return mem_total * 1024, mem_available * 1024, swap_total * 1024, swap_free * 1024, buffers * 1024, cached * 1024

    def _parse_mem_val(self, cursor, n):
        # find the colon
        while cursor < n and self._mem_buf[cursor] != 58:
            cursor += 1
        cursor += 1

        current_val = 0
        in_number = False

        while cursor < n and self._mem_buf[cursor] != 10:
            b = self._mem_buf[cursor]
            if 48 <= b <= 57:
                in_number = True
                current_val = (current_val * 10) + (b - 48)
            elif in_number and b == 32:
                break
            cursor += 1

        return current_val
