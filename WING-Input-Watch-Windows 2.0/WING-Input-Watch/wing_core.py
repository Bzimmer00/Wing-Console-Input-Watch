"""Local state, settings, reports and audited read-only WING transport for v2.
No setter protocol is implemented. See READ_ONLY.txt for the wire allowlist.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import csv
import html
import io
import ipaddress
import json
import math
import os
import queue
import select
import socket
import struct
import threading
import time
from zipfile import ZipFile, ZIP_DEFLATED

VERSION = '2.0'
PORT, OSC_PORT, METER_PORT = 2222, 2223, 2224
STALE_SECONDS = 3.0
INPUT_REPORT, OUTPUT_REPORT = 0x574D0001, 0x574D0002


@dataclass(frozen=True)
class Strip:
    key: str
    label: str
    group: str
    index: int
    meter_type: int
    is_output: bool


STRIPS = tuple(Strip(f'{group}/{i}', f'{label} {i}', group, i, token, output)
               for group, label, count, token, output in (
                   ('ch', 'CH', 40, 0xa0, False), ('aux', 'AUX', 8, 0xa1, False),
                   ('bus', 'BUS', 16, 0xa2, True), ('main', 'MAIN', 4, 0xa3, True),
                   ('mtx', 'MTX', 8, 0xa4, True)) for i in range(1, count + 1))
BY_KEY = {s.key: s for s in STRIPS}
INPUTS = tuple(s for s in STRIPS if not s.is_output)
OUTPUTS = tuple(s for s in STRIPS if s.is_output)
REPORTS = {INPUT_REPORT: INPUTS, OUTPUT_REPORT: OUTPUTS}
NAME_PATHS = {f'/{s.key}/$name': s.key for s in STRIPS}


def frame(payload):
    return b'\xdf\xd3' + payload.replace(b'\xdf', b'\xdf\xde')


def subscription(port, report):
    if not isinstance(port, int) or not 1 <= port <= 65535 or report not in REPORTS:
        raise ValueError('Invalid telemetry request')
    out = bytearray(b'\xd3' + struct.pack('>H', port) + b'\xd4' + struct.pack('>I', report) + b'\xdc')
    last = None
    for strip in REPORTS[report]:
        if strip.meter_type != last:
            out.append(strip.meter_type)
            last = strip.meter_type
        out.append(strip.index - 1)
    return bytes(out) + b'\xde'


def renewal(report):
    if report not in REPORTS:
        raise ValueError('Unknown report')
    return b'\xd4' + struct.pack('>I', report)


def send_meter_only(sock, payload, port):
    # Sole TCP write point. No access to channel 1/2 or property values.
    permitted = tuple(p for report in REPORTS for p in (subscription(port, report), renewal(report)))
    if payload not in permitted:
        raise ValueError('Blocked: only fixed channel-3 meter subscriptions and renewals are allowed')
    sock.sendall(frame(payload))


def osc_string(value):
    b = value.encode('utf-8') + b'\0'
    return b + b'\0' * ((-len(b)) % 4)


def name_query(path):
    # Address only, no OSC type tags or arguments. $name is a read-only leaf.
    if path not in NAME_PATHS:
        raise ValueError('Blocked: only known read-only $name leaves may be requested')
    return osc_string(path)


def send_name_read(sock, ip, path):
    sock.sendto(name_query(path), (str(ipaddress.IPv4Address(ip)), OSC_PORT))


def send_discovery(sock, ip):
    sock.sendto(b'WING?', (str(ipaddress.IPv4Address(ip)), PORT))


def read_osc_string(data, offset):
    end = data.index(b'\0', offset)
    text = data[offset:end].decode('utf-8')
    following = (end + 4) & ~3
    if following > len(data) or any(data[end:following]):
        raise ValueError('Invalid OSC padding')
    return text, following


def decode_name(data):
    try:
        path, offset = read_osc_string(data, 0)
        tags, offset = read_osc_string(data, offset)
        if path not in NAME_PATHS or tags != ',s':
            return None
        name, _ = read_osc_string(data, offset)
        # Display and exported names cannot inject new lines/control characters.
        name = ''.join(c for c in name if c.isprintable()).strip()[:64]
        return NAME_PATHS[path], name
    except (ValueError, UnicodeError, IndexError):
        return None


def decode_meters(data):
    if len(data) < 4:
        return None
    report = struct.unpack('>I', data[:4])[0]
    strips = REPORTS.get(report)
    if strips is None or len(data) != 4 + len(strips) * 16:
        return None
    words = struct.unpack(f'>{len(strips)*8}h', data[4:])
    result = {}
    for i, strip in enumerate(strips):
        offset = i * 8 + (2 if strip.is_output else 0)
        result[strip.key] = (words[offset] / 256., words[offset+1] / 256.)
    return report, result


def now_stamp():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def default_profile():
    return {'warning': -6., 'critical': -1., 'outputs': False, 'names': True,
            'reconnect': True, 'sound': False, 'raise_window': True,
            'strips': {s.key: {'enabled': True, 'alias': '', 'warning': None, 'critical': None} for s in STRIPS}}


def thresholds(profile, key):
    item = profile['strips'][key]
    return (profile['warning'] if item['warning'] is None else item['warning'],
            profile['critical'] if item['critical'] is None else item['critical'])


def validate_thresholds(warning, critical):
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in (warning, critical)):
        raise ValueError('Thresholds must be finite numbers')
    if not -90 <= warning < critical <= 0:
        raise ValueError('Use -90 ≤ warning < critical ≤ 0 dBFS')


def clean_profile(raw):
    result = default_profile()
    if not isinstance(raw, dict):
        return result
    w, c = raw.get('warning', -6.), raw.get('critical', -1.)
    try:
        validate_thresholds(w, c)
        result.update(warning=float(w), critical=float(c))
    except ValueError:
        pass
    for key in ('outputs', 'names', 'reconnect', 'sound', 'raise_window'):
        if isinstance(raw.get(key), bool):
            result[key] = raw[key]
    items = raw.get('strips', {})
    if not isinstance(items, dict):
        return result
    for key, item in result['strips'].items():
        old = items.get(key, {})
        if not isinstance(old, dict):
            continue
        if isinstance(old.get('enabled'), bool):
            item['enabled'] = old['enabled']
        if isinstance(old.get('alias'), str):
            item['alias'] = ''.join(c for c in old['alias'] if c.isprintable())[:64]
        w, c = old.get('warning'), old.get('critical')
        try:
            validate_thresholds(result['warning'] if w is None else w, result['critical'] if c is None else c)
            item.update(warning=w, critical=c)
        except ValueError:
            pass
    return result


class Settings:
    def __init__(self, folder=None):
        self.folder = Path(folder) if folder else Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'WingInputWatch'
        self.path = self.folder / 'settings-v2.json'
        self.data = {'last_ip': '', 'geometry': '1280x850', 'profiles': {}}
        self.error = ''
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(raw, dict) and isinstance(raw.get('profiles'), dict):
                self.data = {'last_ip': str(raw.get('last_ip', '')), 'geometry': str(raw.get('geometry', '1280x850')), 'profiles': raw['profiles']}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            self.error = f'Settings could not be loaded: {exc}'

    def profile(self, ip):
        return clean_profile(self.data['profiles'].get(ip))

    def save(self, ip, profile, geometry):
        if ip:
            ip = str(ipaddress.IPv4Address(ip))
            self.data['profiles'][ip] = clean_profile(profile)
            self.data['last_ip'] = ip
        self.data['geometry'] = geometry
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.data, indent=2, allow_nan=False), encoding='utf-8')
        os.replace(temporary, self.path)


@dataclass
class Episode:
    key: str
    severity: str
    started: str
    threshold: float
    peak: float
    name: str
    duration: float = 0.
    ended: str = ''
    reason: str = ''


class Monitor:
    def __init__(self, profile=None):
        self.profile = profile or default_profile()
        self.names = {}
        self.levels = {}
        self.peaks = {s.key: -128. for s in STRIPS}
        self.last = {}
        self.active = {}
        self.latched = {}
        self.counts = {s.key: {'warning': 0, 'critical': 0} for s in STRIPS}
        self.durations = {s.key: {'warning': 0., 'critical': 0.} for s in STRIPS}
        self.histories = {s.key: deque(maxlen=1200) for s in STRIPS}
        self.episodes = []
        self.connections = []

    def title(self, key):
        return self.profile['strips'][key]['alias'] or self.names.get(key) or BY_KEY[key].label

    def enabled(self, key):
        return self.profile['strips'][key]['enabled'] and (self.profile['outputs'] or not BY_KEY[key].is_output)

    def level_state(self, key):
        if not self.enabled(key):
            return 'Disabled'
        if key not in self.levels:
            return 'No live data'
        value = max(self.levels[key])
        w, c = thresholds(self.profile, key)
        return 'CRITICAL' if value >= c else 'Warning' if value >= w else 'Below thresholds'

    def feed(self, values, mono, stamp):
        changes = []
        for key, pair in values.items():
            previous = self.last.get(key)
            dt = 0 if previous is None else mono - previous
            if not 0 <= dt <= STALE_SECONDS:
                self.gap([key], stamp, 'Meter gap', changes)
                dt = 0
            self.levels[key] = pair
            value = max(pair)
            self.peaks[key] = max(self.peaks[key], value)
            history = self.histories[key]
            if not history or mono - history[-1][0] >= .1:
                history.append((mono, value))
            for severity, limit in zip(('warning', 'critical'), thresholds(self.profile, key)):
                token = key, severity
                episode = self.active.get(token)
                if episode is not None:
                    episode.duration += dt
                    self.durations[key][severity] += dt
                    episode.peak = max(episode.peak, value)
                high = self.enabled(key) and value >= limit
                if high and episode is None:
                    episode = Episode(key, severity, stamp, limit, value, self.title(key))
                    self.active[token] = episode
                    self.episodes.append(episode)
                    self.counts[key][severity] += 1
                    held = self.latched.get(key)
                    if held is None or severity == 'critical' or held.severity != 'critical':
                        self.latched[key] = episode
                    changes.append(('start', episode))
                elif not high and episode is not None:
                    self.end(token, stamp, 'Recovered', changes)
            self.last[key] = mono
        return changes

    def end(self, token, stamp, reason, changes):
        episode = self.active.pop(token)
        episode.ended, episode.reason = stamp, reason
        changes.append(('end', episode))

    def gap(self, keys=None, stamp=None, reason='Connection interrupted', changes=None):
        changes = [] if changes is None else changes
        keys = set(BY_KEY if keys is None else keys)
        stamp = stamp or now_stamp()
        for token in list(self.active):
            if token[0] in keys:
                self.end(token, stamp, reason, changes)
        for key in keys:
            self.levels.pop(key, None)
            self.last.pop(key, None)
            history = self.histories[key]
            if history and history[-1][1] is not None:
                history.append((time.monotonic(), None))
        return changes

    def acknowledge(self):
        self.latched.clear()


def csv_value(value):
    # Treat mixer/user text as text in spreadsheet exports.
    return "'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value


def csv_text(rows):
    buf = io.StringIO(newline='')
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow([csv_value(v) for v in row])
    return buf.getvalue()


class EventLog:
    def __init__(self, folder):
        self.path = Path(folder) / ('events-v2-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.csv')
        self.error = ''

    def write(self, category, key='', name='', severity='', level='', duration='', detail=''):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not self.path.exists()
            with self.path.open('a', encoding='utf-8-sig' if fresh else 'utf-8', newline='') as stream:
                if fresh:
                    stream.write(csv_text([('Timestamp', 'Event', 'Strip', 'Name', 'Severity', 'Peak dBFS', 'Observed seconds', 'Detail')]))
                stream.write(csv_text([(now_stamp(), category, key, name, severity, level, duration, detail)]))
        except OSError as exc:
            self.error = str(exc)


def report_rows(model):
    summary = [('Strip', 'Name', 'Enabled', 'Tap', 'Warning dBFS', 'Critical dBFS', 'Peak dBFS', 'Warning count', 'Critical count', 'Warning seconds', 'Critical seconds')]
    for s in STRIPS:
        w, c = thresholds(model.profile, s.key)
        summary.append((s.label, model.title(s.key), model.enabled(s.key), 'Output' if s.is_output else 'Input', w, c,
                        round(model.peaks[s.key], 2) if model.peaks[s.key] > -128 else '',
                        model.counts[s.key]['warning'], model.counts[s.key]['critical'],
                        round(model.durations[s.key]['warning'], 3), round(model.durations[s.key]['critical'], 3)))
    alerts = [('Strip', 'Name at trigger', 'Severity', 'Started', 'Ended', 'Threshold dBFS', 'Peak dBFS', 'Observed seconds', 'End reason')]
    alerts.extend((BY_KEY[e.key].label, e.name, e.severity, e.started, e.ended, e.threshold, round(e.peak, 2), round(e.duration, 3), e.reason or 'Still active') for e in model.episodes)
    connections = [('Timestamp', 'Connection event', 'Detail')] + model.connections
    return summary, alerts, connections


def export_report(model, path):
    summary, alerts, connections = report_rows(model)
    def table(rows):
        return '<table>' + ''.join('<tr>' + ''.join(f'<{"th" if i == 0 else "td"}>{html.escape(str(v))}</{"th" if i == 0 else "td"}>' for v in row) + '</tr>' for i, row in enumerate(rows)) + '</table>'
    document = ('<!doctype html><meta charset="utf-8"><title>WING monitoring report</title>'
                '<style>body{font:14px system-ui;margin:32px;color:#173047}table{border-collapse:collapse;font-size:12px}td,th{padding:7px;border:1px solid #ccd6df;text-align:left}th{background:#e8eef4}h1{color:#155e75}</style>'
                '<h1>WING Input Watch — Event report</h1><p>Generated ' + html.escape(now_stamp()) + '</p>'
                '<p>Threshold warnings from sampled meters, not hardware clipping flags. Durations cover observed data only; gaps are excluded. Warning totals include time also above the critical threshold. Statistics cover this app session, including reconnects.</p>'
                '<h2>Channel summary</h2>' + table(summary) + '<h2>Threshold episodes</h2>' + table(alerts) + '<h2>Monitoring interruptions and connections</h2>' + table(connections))
    with ZipFile(path, 'w', ZIP_DEFLATED) as bundle:
        bundle.writestr('report.html', document)
        bundle.writestr('channel-summary.csv', '\ufeff' + csv_text(summary))
        bundle.writestr('threshold-episodes.csv', '\ufeff' + csv_text(alerts))
        bundle.writestr('connections.csv', '\ufeff' + csv_text(connections))


class MeterWorker(threading.Thread):
    def __init__(self, ip, events, stop, outputs=False, names=True, reconnect=True):
        super().__init__(daemon=True)
        self.ip = str(ipaddress.IPv4Address(ip))
        self.events, self.stop = events, stop
        self.outputs, self.names, self.reconnect = outputs, names, reconnect

    def run(self):
        delay = 2
        try:
            while not self.stop.is_set():
                try:
                    self.session()
                except (OSError, ValueError) as exc:
                    if self.stop.is_set():
                        break
                    self.events.put(('lost', str(exc)))
                    if not self.reconnect:
                        break
                    self.events.put(('retry', f'Retrying in {delay} seconds…'))
                    if self.stop.wait(delay):
                        break
                    delay = min(delay * 2, 15)
        finally:
            self.events.put(('ended', None))

    def session(self):
        received = valid = rejected = name_count = 0
        last_packet = 'none'
        reports = (INPUT_REPORT, OUTPUT_REPORT) if self.outputs else (INPUT_REPORT,)
        last_valid = {report: time.monotonic() for report in reports}
        self.events.put(('status', 'Connecting…'))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as namesock:
            try:
                udp.bind(('0.0.0.0', METER_PORT))
            except OSError as exc:
                raise OSError(f'Cannot open UDP {METER_PORT}; close other copies. {exc}') from exc
            udp.settimeout(3)
            send_discovery(udp, self.ip)
            reply, sender = udp.recvfrom(4096)
            if sender[0] != self.ip or not reply.startswith(b'WING,'):
                raise OSError('No valid WING discovery reply from the selected IP')
            identity = reply.decode('utf-8', errors='replace').strip()
            namesock.bind(('0.0.0.0', 0))
            namesock.setblocking(False)
            with socket.create_connection((self.ip, PORT), timeout=3) as tcp:
                tcp.settimeout(1)
                local_ip = tcp.getsockname()[0]
                receive_port = udp.getsockname()[1]
                for report in reports:
                    send_meter_only(tcp, subscription(receive_port, report), receive_port)
                started = time.monotonic()
                last_valid = {report: started for report in reports}
                next_renew = started + 2
                paths = [path for path, key in NAME_PATHS.items() if self.outputs or not BY_KEY[key].is_output] if self.names else []
                path_index = 0
                next_name = started
                self.events.put(('connected', identity))
                while not self.stop.is_set():
                    now = time.monotonic()
                    if now >= next_renew:
                        for report in reports:
                            send_meter_only(tcp, renewal(report), receive_port)
                        next_renew = now + 2
                        self.events.put(('diagnostic', f'PC {local_ip} | WING {self.ip} | UDP receive {receive_port}\nMeter packets: {received} | Valid: {valid} | Rejected: {rejected} | Last: {last_packet}\nNames read: {name_count} | Name reads: {"enabled" if self.names else "disabled"}\n{identity}'))
                    if paths and now >= next_name:
                        try:
                            send_name_read(namesock, self.ip, paths[path_index])
                        except OSError:
                            # Optional name reads must never interrupt metering.
                            pass
                        path_index += 1
                        if path_index == len(paths):
                            path_index = 0
                            next_name = now + 20
                        else:
                            next_name = now + .03
                    ready, _, _ = select.select([udp, tcp, namesock], [], [], .03)
                    if tcp in ready and not tcp.recv(16384):
                        raise OSError('WING closed TCP connection')
                    if namesock in ready:
                        data, source = namesock.recvfrom(4096)
                        if source[0] == self.ip:
                            result = decode_name(data)
                            if result:
                                name_count += 1
                                self.events.put(('name', result))
                    if udp in ready:
                        data, source = udp.recvfrom(8192)
                        if source[0] == self.ip and not data.startswith(b'WING,'):
                            received += 1
                            last_packet = f'{len(data)} bytes / {data[:4].hex()}'
                            result = decode_meters(data)
                            if result is not None and result[0] in reports:
                                valid += 1
                                report, values = result
                                last_valid[report] = time.monotonic()
                                self.events.put(('meters', (last_valid[report], now_stamp(), report, values)))
                            else:
                                rejected += 1
                    now = time.monotonic()
                    missing = [r for r in reports if now - last_valid[r] > STALE_SECONDS]
                    if missing:
                        groups = ', '.join('inputs' if r == INPUT_REPORT else 'outputs' for r in missing)
                        raise OSError(f'MONITORING LOST: no usable {groups} meter reports for 3 seconds')
