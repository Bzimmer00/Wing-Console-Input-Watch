import ast
import copy
import json
import queue
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from zipfile import ZipFile
import wing_core as core


def payload_frames(stream):
    # Split audited TCP wire data. Any engine channel other than 3 fails.
    result, current = [], bytearray()
    i = 0
    while i < len(stream):
        if stream[i] != 0xdf:
            current.append(stream[i]); i += 1; continue
        if i+1 >= len(stream):
            raise ValueError('Incomplete escape')
        following = stream[i+1]
        if following == 0xde:
            current.append(0xdf)
        elif following == 0xd3:
            if current:
                result.append(bytes(current)); current.clear()
        else:
            raise ValueError(f'Forbidden channel escape {following:x}')
        i += 2
    if current:
        result.append(bytes(current))
    return result


def packet(report=core.INPUT_REPORT, level=-20):
    words = []
    for s in core.REPORTS[report]:
        # Distinct input/output pairs catch the wrong tap selection.
        words += [-10*256, -11*256, round(level*256), round((level-1)*256), 0, 0, 0, 0] if s.is_output else [round(level*256), round((level-1)*256), -30*256, -31*256, 0, 0, 0, 0]
    return struct.pack('>I' + str(len(words)) + 'h', report, *words)


class ProtocolTests(unittest.TestCase):
    def test_input_layout_matches_working_v1(self):
        want = b'\xd3\x37\x37\xd4WM\x00\x01\xdc\xa0' + bytes(range(40)) + b'\xa1' + bytes(range(8)) + b'\xde'
        self.assertEqual(core.subscription(14135, core.INPUT_REPORT), want)

    def test_outputs_use_correct_groups(self):
        payload = core.subscription(2224, core.OUTPUT_REPORT)
        self.assertEqual(payload[8:], b'\xdc\xa2' + bytes(range(16)) + b'\xa3' + bytes(range(4)) + b'\xa4' + bytes(range(8)) + b'\xde')

    def test_all_port_values_are_escaped_without_channel_switches(self):
        for port in range(1, 65536):
            p = core.subscription(port, core.INPUT_REPORT)
            self.assertEqual(payload_frames(core.frame(p)), [p])

    def test_tcp_rejects_control_paths_and_modified_payloads(self):
        mock = Mock()
        for bad in (b'', b'/ch/1/mute', b'\xdf\xd2', b'\xd7\x01\x02\x03\x04\x01', core.subscription(2224, core.INPUT_REPORT)+b'\x01'):
            with self.assertRaises(ValueError):
                core.send_meter_only(mock, bad, 2224)
        mock.sendall.assert_not_called()

    def test_decode_input_and_output_taps(self):
        for report in core.REPORTS:
            rid, result = core.decode_meters(packet(report, -.5))
            self.assertEqual(rid, report)
            self.assertEqual(set(result), {s.key for s in core.REPORTS[report]})
            self.assertTrue(all(value == (-.5, -1.5) for value in result.values()))

    def test_invalid_meter_reports(self):
        for bad in (b'', packet()[:-2], packet()+b'\0\0', b'FAIL'+packet()[4:]):
            self.assertIsNone(core.decode_meters(bad))

    def test_name_queries_only_readonly_exact_paths(self):
        for path in core.NAME_PATHS:
            self.assertEqual(core.name_query(path), core.osc_string(path))
            self.assertNotIn(b',', core.name_query(path))
        for bad in ('/ch/1/name', '/ch/1/fdr', '/ch/1/mute', '/ch/1/$name=Oops', '/ch/1/$name\0,s\0Oops', '/ch/41/$name', '/rec/1/start'):
            with self.assertRaises(ValueError):
                core.name_query(bad)

    def test_valid_names_and_malformed_names(self):
        valid = core.osc_string('/ch/1/$name') + core.osc_string(',s') + core.osc_string('Pastor Mic')
        self.assertEqual(core.decode_name(valid), ('ch/1', 'Pastor Mic'))
        for bad in (b'', valid[:-1], core.osc_string('/ch/1/name')+valid[len(core.osc_string('/ch/1/$name')):], core.osc_string('/ch/1/$name')+core.osc_string(',f')+b'\0'*4):
            self.assertIsNone(core.decode_name(bad))

    def test_source_all_send_points_are_guarded(self):
        tree = ast.parse(Path(core.__file__).read_text())
        calls = []
        class Walk(ast.NodeVisitor):
            def __init__(self): self.fn = None
            def visit_FunctionDef(self, node):
                previous = self.fn; self.fn = node.name; self.generic_visit(node); self.fn = previous
            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute) and node.func.attr in ('send', 'sendall', 'sendto', 'sendmsg'):
                    calls.append((self.fn, node.func.attr))
                self.generic_visit(node)
        Walk().visit(tree)
        self.assertEqual(sorted(calls), [('send_discovery', 'sendto'), ('send_meter_only', 'sendall'), ('send_name_read', 'sendto')])


class ModelTests(unittest.TestCase):
    def setUp(self): self.m = core.Monitor()
    def feed(self, value, t, **kwargs): return self.m.feed({'ch/1': (value, value-1)}, t, str(t))

    def test_warning_then_critical_escalates_and_counts(self):
        self.feed(-5, 0)
        self.assertEqual(self.m.latched['ch/1'].severity, 'warning')
        self.feed(0, 1)
        self.assertEqual(self.m.latched['ch/1'].severity, 'critical')
        self.assertEqual(self.m.counts['ch/1'], {'warning': 1, 'critical': 1})
        self.feed(-20, 2)
        self.assertFalse(self.m.active)
        self.assertEqual(self.m.durations['ch/1'], {'warning': 2, 'critical': 1})
        self.assertEqual(self.m.latched['ch/1'].severity, 'critical')

    def test_ack_does_not_realert_until_recovery(self):
        self.feed(0, 0)
        self.m.acknowledge()
        self.assertEqual(self.feed(0, 1), [])
        self.assertFalse(self.m.latched)
        self.feed(-20, 2)
        self.assertEqual(len(self.feed(0, 3)), 2)

    def test_gap_excludes_downtime_and_preserves_latch(self):
        self.feed(0, 0); self.feed(0, 1)
        self.m.gap(stamp='2')
        self.assertNotIn('ch/1', self.m.levels)
        self.assertIn('ch/1', self.m.latched)
        self.feed(0, 20)
        self.assertEqual(self.m.durations['ch/1']['critical'], 1)
        self.assertEqual(self.m.counts['ch/1']['critical'], 2)

    def test_implicit_gap(self):
        self.feed(0, 0); self.feed(0, 20)
        self.assertEqual(self.m.durations['ch/1']['critical'], 0)
        self.assertEqual(self.m.levels['ch/1'], (0, -1))

    def test_disabled_channel_records_history_without_alerting(self):
        self.m.profile['strips']['ch/1']['enabled'] = False
        self.feed(0, 0)
        self.assertFalse(self.m.episodes)
        self.assertEqual(len(self.m.histories['ch/1']), 1)

    def test_output_alerts_opt_in(self):
        self.m.feed({'main/1': (0, 0)}, 0, 'a')
        self.assertFalse(self.m.episodes)
        self.m.profile['outputs'] = True
        self.m.feed({'main/1': (0, 0)}, 1, 'b')
        self.assertEqual(len(self.m.episodes), 2)

    def test_local_override_thresholds_and_name(self):
        self.m.names['ch/1'] = 'Mic'
        self.assertEqual(self.m.title('ch/1'), 'Mic')
        self.m.profile['strips']['ch/1'].update(alias='Lectern', warning=-12, critical=-8)
        self.feed(-10, 0)
        self.assertEqual(self.m.latched['ch/1'].name, 'Lectern')
        self.assertEqual(self.m.latched['ch/1'].threshold, -12)

    def test_history_bounded_and_decimated(self):
        for n in range(30000): self.feed(-20, n/100)
        self.assertLessEqual(len(self.m.histories['ch/1']), 1200)

    def test_threshold_validation(self):
        for w, c in ((-1, -6), (0, 0), (float('nan'), -1), (-6, float('inf')), (-91, -1), ('x', -1)):
            with self.assertRaises(ValueError): core.validate_thresholds(w, c)


class PersistenceTests(unittest.TestCase):
    def test_profiles_separate_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = core.Settings(folder)
            p = core.default_profile(); p['strips']['ch/1'].update(alias='Mic', warning=-10, critical=-2)
            settings.save('192.168.1.2', p, '1100x800')
            q = core.Settings(folder)
            self.assertEqual(q.profile('192.168.1.2')['strips']['ch/1']['alias'], 'Mic')
            self.assertEqual(q.profile('192.168.1.3')['strips']['ch/1']['alias'], '')
            self.assertEqual(q.data['geometry'], '1100x800')

    def test_bad_settings_safe_defaults(self):
        p = core.clean_profile({'warning': float('nan'), 'critical': -1, 'strips': {'ch/1': {'warning': 0, 'critical': -8}}})
        self.assertEqual(core.thresholds(p, 'ch/1'), (-6, -1))
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'settings-v2.json').write_text('{bad')
            self.assertTrue(core.Settings(folder).error)

    def test_reports_escape_text_and_include_gaps(self):
        m = core.Monitor()
        m.names['ch/1'] = '=HYPERLINK("bad")<script>'
        m.feed({'ch/1': (0, 0)}, 0, 'now')
        m.gap(stamp='later')
        m.connections.append(('now', 'Monitoring lost', 'test'))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'report.zip')
            core.export_report(m, path)
            with ZipFile(path) as z:
                self.assertEqual(len(z.namelist()), 4)
                self.assertIn('&lt;script&gt;', z.read('report.html').decode())
                self.assertNotIn('<script>', z.read('report.html').decode())
                self.assertIn("'=HYPERLINK", z.read('channel-summary.csv').decode('utf-8-sig'))
                self.assertIn('Monitoring lost', z.read('connections.csv').decode())

    def test_audit_log_append(self):
        with tempfile.TemporaryDirectory() as folder:
            log = core.EventLog(folder)
            log.write('start', name='=Bad')
            log.write('end')
            text = log.path.read_text(encoding='utf-8-sig')
            self.assertEqual(len(text.splitlines()), 3)
            self.assertIn("'=Bad", text)


class FakeWing:
    def __init__(self, with_outputs=True):
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.osc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        for sock, port in ((self.udp, 2222), (self.tcp, 2222), (self.osc, 2223)):
            sock.bind(('127.0.0.1', port)); sock.settimeout(.1)
        self.tcp.listen(2)
        self.stop = threading.Event()
        self.streams, self.queries, self.discoveries = [], [], []
        self.emit = True
        self.with_outputs = with_outputs
        self.connections = 0
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        conn = None; target = None; sent = 0
        while not self.stop.is_set():
            try:
                data, addr = self.udp.recvfrom(4096)
                self.discoveries.append((data, addr))
                self.udp.sendto(b'WING,127.0.0.1,Test,ngc-full,0,3.0.5', addr)
                target = addr
            except socket.timeout: pass
            except OSError: break
            if conn is None:
                try:
                    conn, _ = self.tcp.accept(); conn.settimeout(.01)
                    self.connections += 1
                    self.streams.append(bytearray())
                except socket.timeout: pass
            if conn is not None:
                try:
                    data = conn.recv(4096)
                    if not data: conn.close(); conn = None
                    else: self.streams[-1].extend(data)
                except socket.timeout: pass
                except OSError: conn = None
            try:
                query, addr = self.osc.recvfrom(4096)
                self.queries.append(query)
                path = query.rstrip(b'\0').decode()
                self.osc.sendto(core.osc_string(path)+core.osc_string(',s')+core.osc_string('Test Mic'), addr)
            except socket.timeout: pass
            except OSError: break
            if target and conn and self.emit and time.monotonic()-sent > .05:
                try:
                    self.udp.sendto(packet(core.INPUT_REPORT, -20), target)
                    if self.with_outputs:
                        self.udp.sendto(packet(core.OUTPUT_REPORT, -10), target)
                    sent = time.monotonic()
                except OSError: pass
        if conn: conn.close()

    def close(self):
        self.stop.set(); self.thread.join(2)
        for sock in (self.udp, self.tcp, self.osc): sock.close()


class NetworkTests(unittest.TestCase):
    def wait_event(self, events, wanted, timeout=6):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            try: item = events.get(timeout=.3)
            except queue.Empty: continue
            if item[0] == wanted: return item[1]
        self.fail('No event: ' + wanted)

    def test_simulated_wing_names_inputs_outputs_and_strict_wire_policy(self):
        server = FakeWing()
        stop = threading.Event(); events = queue.SimpleQueue()
        worker = core.MeterWorker('127.0.0.1', events, stop, outputs=True, names=True, reconnect=False)
        worker.start()
        try:
            seen = set()
            deadline = time.monotonic()+5
            while time.monotonic() < deadline and (len(seen)<2 or not server.queries):
                payload = self.wait_event(events, 'meters')
                seen.add(payload[2])
            self.assertEqual(seen, {core.INPUT_REPORT, core.OUTPUT_REPORT})
            self.assertTrue(server.queries)
            key, name = self.wait_event(events, 'name')
            self.assertIn(key, core.BY_KEY); self.assertEqual(name, 'Test Mic')
            self.wait_event(events, 'diagnostic')
        finally:
            stop.set(); worker.join(4); server.close()
        permitted = {p for r in core.REPORTS for p in (core.subscription(2224,r),core.renewal(r))}
        for stream in server.streams:
            for payload in payload_frames(stream): self.assertIn(payload, permitted)
        for data, addr in server.discoveries:
            self.assertEqual(data, b'WING?'); self.assertEqual(addr[1], core.METER_PORT)
        for data in server.queries:
            self.assertIn(data, {core.name_query(p) for p in core.NAME_PATHS})
        self.assertFalse(worker.is_alive())

    def test_loss_reconnect_and_stop(self):
        server = FakeWing(False)
        stop = threading.Event(); events = queue.SimpleQueue()
        worker = core.MeterWorker('127.0.0.1', events, stop, names=False, reconnect=True)
        worker.start()
        try:
            self.wait_event(events, 'meters')
            server.emit = False
            self.assertIn('MONITORING LOST', self.wait_event(events, 'lost', 5))
            self.wait_event(events, 'retry')
            server.emit = True
            self.wait_event(events, 'connected', 5)
            self.wait_event(events, 'meters')
            self.assertGreaterEqual(server.connections, 2)
            self.assertFalse(server.queries)
        finally:
            stop.set(); worker.join(4); server.close()
        self.assertFalse(worker.is_alive())
        allowed = {core.subscription(core.METER_PORT, core.INPUT_REPORT), core.renewal(core.INPUT_REPORT)}
        for stream in server.streams:
            for payload in payload_frames(stream):
                self.assertIn(payload, allowed)


if __name__ == '__main__': unittest.main(verbosity=2)
