"""WING Input Watch 2.0. All UI actions are local. See wing_core.py for wire policy."""
import copy
import ipaddress
import math
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from wing_core import (VERSION, STRIPS, BY_KEY, INPUT_REPORT, OUTPUT_REPORT, STALE_SECONDS,
                       MeterWorker, Monitor, Settings, EventLog, default_profile,
                       thresholds, validate_thresholds, now_stamp, csv_text,
                       report_rows, export_report)


class App:
    def __init__(self, root, settings=None):
        self.root = root
        self.settings = settings or Settings()
        self.profile_ip = self.settings.data['last_ip']
        self.profile = self.settings.profile(self.profile_ip)
        self.model = Monitor(self.profile)
        self.events = queue.SimpleQueue()
        self.worker = None
        self.stop = threading.Event()
        self.requested = False
        self.popup = None
        self.latest = {}
        self.status = 'Disconnected — not monitoring'
        self.diagnostic = 'No connection yet.'
        self.retry = ''
        self.session_started = now_stamp()
        self.logger = EventLog(self.settings.folder)
        self.logger.write('Application started', detail=f'WING Input Watch {VERSION}')
        self.last_beep = 0
        self.ui_ticks = 0
        root.title(f'WING Input Watch {VERSION} — STRICT READ ONLY')
        try:
            root.geometry(self.settings.data['geometry'])
        except tk.TclError:
            root.geometry('1280x850')
        root.minsize(1000, 680)
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('Treeview', rowheight=25)
        style.configure('Title.TLabel', font=('Segoe UI', 21, 'bold'), foreground='#17354a')
        style.configure('Subtitle.TLabel', foreground='#16745a', font=('Segoe UI', 10, 'bold'))
        top = ttk.Frame(root, padding=(14, 12, 14, 6))
        top.pack(fill='x')
        ttk.Label(top, text='WING INPUT WATCH', style='Title.TLabel').pack(side='left')
        ttk.Label(top, text='STRICT READ ONLY  •  v' + VERSION, style='Subtitle.TLabel').pack(side='right')
        controls = ttk.Frame(root, padding=(14, 6))
        controls.pack(fill='x')
        ttk.Label(controls, text='WING IP').pack(side='left')
        self.ip = tk.StringVar(value=self.profile_ip)
        self.ipentry = ttk.Entry(controls, textvariable=self.ip, width=18)
        self.ipentry.pack(side='left', padx=6)
        self.ipentry.bind('<FocusOut>', lambda e: self.change_profile())
        self.connectbtn = ttk.Button(controls, text='Connect', command=self.connect)
        self.connectbtn.pack(side='left')
        self.disconnectbtn = ttk.Button(controls, text='Disconnect', command=self.disconnect, state='disabled')
        self.disconnectbtn.pack(side='left', padx=5)
        self.outputvar = tk.BooleanVar(value=self.profile['outputs'])
        self.namesvar = tk.BooleanVar(value=self.profile['names'])
        self.reconnectvar = tk.BooleanVar(value=self.profile['reconnect'])
        self.transport_checks = []
        for label, var in [('Include outputs', self.outputvar), ('Read names', self.namesvar), ('Auto reconnect', self.reconnectvar)]:
            cb = ttk.Checkbutton(controls, text=label, variable=var, command=self.transport_changed)
            cb.pack(side='left', padx=7)
            self.transport_checks.append(cb)
        self.statuslabel = tk.Label(root, text=self.status, bg='#3c5264', fg='white', font=('Segoe UI', 11, 'bold'), anchor='w', padx=14, pady=9)
        self.statuslabel.pack(fill='x', pady=(6, 0))
        self.banner = tk.Label(root, text='No unacknowledged warnings', bg='#eaf0f4', fg='#17354a', font=('Segoe UI', 11, 'bold'), anchor='w', padx=14, pady=9, wraplength=1200)
        self.banner.pack(fill='x')
        bar = ttk.Frame(root, padding=(14, 8))
        bar.pack(fill='x')
        ttk.Button(bar, text='Acknowledge all', command=self.ack).pack(side='left')
        ttk.Button(bar, text='Show warnings', command=self.show_popup).pack(side='left', padx=5)
        ttk.Button(bar, text='Local settings', command=self.local_settings).pack(side='left', padx=5)
        ttk.Button(bar, text='Export event report', command=self.export).pack(side='right')
        self.soundvar = tk.BooleanVar(value=self.profile['sound'])
        self.raisevar = tk.BooleanVar(value=self.profile['raise_window'])
        ttk.Checkbutton(bar, text='PC sound (critical)', variable=self.soundvar, command=self.alert_options).pack(side='left', padx=7)
        ttk.Checkbutton(bar, text='Raise warning window', variable=self.raisevar, command=self.alert_options).pack(side='left', padx=7)
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill='both', expand=True, padx=14, pady=(0, 8))
        monitor_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(monitor_tab, text='Live monitoring')
        filters = ttk.Frame(monitor_tab)
        filters.pack(fill='x', pady=(0, 8))
        ttk.Label(filters, text='View').pack(side='left')
        self.filtervar = tk.StringVar(value='All enabled')
        choices = ['All enabled', 'All channels', 'Inputs', 'Outputs', 'Warnings only']
        box = ttk.Combobox(filters, textvariable=self.filtervar, values=choices, width=17, state='readonly')
        box.pack(side='left', padx=5)
        box.bind('<<ComboboxSelected>>', lambda e: self.render_table())
        ttk.Label(filters, text='Search').pack(side='left', padx=(12, 0))
        self.searchvar = tk.StringVar()
        ttk.Entry(filters, textvariable=self.searchvar, width=22).pack(side='left', padx=5)
        self.searchvar.trace_add('write', lambda *a: self.render_table())
        ttk.Button(filters, text='Configure selected…', command=self.configure_selected).pack(side='right')
        ttk.Label(filters, text='Ctrl/Shift to select several rows').pack(side='right', padx=8)
        self.table = self.make_tree(monitor_tab,
            [('strip', 'Strip', 80), ('name', 'Name', 180), ('tap', 'Tap', 70), ('left', 'L dBFS', 74), ('right', 'R dBFS', 74),
             ('peak', 'Peak', 70), ('limits', 'Warn / Crit', 100), ('counts', 'Count W / C', 100), ('duration', 'Seconds W / C', 115), ('state', 'State', 205)], extended=True)
        for tag, bg, fg in [('critical', '#f8d7da', '#842029'), ('warning', '#fff3cd', '#664d03'), ('held', '#e6dcfa', '#4d2876'), ('disabled', '#eef1f3', '#687782')]:
            self.table.tag_configure(tag, background=bg, foreground=fg)
        self.table.bind('<<TreeviewSelect>>', self.row_selected)
        self.table.bind('<Double-1>', lambda e: self.configure_selected())
        history_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(history_tab, text='Level history')
        graphcontrols = ttk.Frame(history_tab)
        graphcontrols.pack(fill='x')
        ttk.Label(graphcontrols, text='Channel').pack(side='left')
        self.graphkey = tk.StringVar(value='ch/1')
        self.graphbox = ttk.Combobox(graphcontrols, values=[s.key for s in STRIPS], textvariable=self.graphkey, state='readonly', width=14)
        self.graphbox.pack(side='left', padx=8)
        self.graphbox.bind('<<ComboboxSelected>>', lambda e: self.draw_graph())
        self.graphlabel = ttk.Label(graphcontrols, text='')
        self.graphlabel.pack(side='left', padx=8)
        self.canvas = tk.Canvas(history_tab, bg='#122331', highlightthickness=0, height=320)
        self.canvas.pack(fill='both', expand=True, pady=12)
        self.canvas.bind('<Configure>', lambda e: self.draw_graph())
        ttk.Label(history_tab, text='Last 120 seconds • maximum of L/R • sampled at up to 10 points/second • yellow = warning; red = critical.\nGaps in monitoring remain gaps in the graph. All received meter reports are checked for alerts.').pack(anchor='w')
        alert_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(alert_tab, text='Alert history')
        ttk.Label(alert_tab, text='Latest 1,000 threshold episodes. Warning time includes time also above critical. Reports export the full session.').pack(anchor='w', pady=(0, 8))
        self.alerttable = self.make_tree(alert_tab,
            [('start', 'Started (local)', 210), ('channel', 'Channel / name', 190), ('severity', 'Severity', 90), ('peak', 'Peak dBFS', 85), ('duration', 'Observed sec', 100), ('end', 'Status / end reason', 230)])
        diag_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(diag_tab, text='Connection & read-only policy')
        ttk.Button(diag_tab, text='Copy diagnostics', command=self.copy_diagnostics).pack(anchor='w')
        self.diagtext = tk.Text(diag_tab, height=11, wrap='word', font=('Consolas', 10))
        self.diagtext.pack(fill='x', pady=10)
        ttk.Label(diag_tab, text='Only these requests may leave this app:\n• WING discovery (UDP 2222).\n• Fixed meter subscriptions and renewals on native channel 3 (TCP 2222).\n• Optional address-only queries for the read-only $name fields (UDP 2223).\n\nThere are no gain, fader, mute, routing, phantom-power, scene, RTA-source, or lock commands.\nAll configuration controls in this app change local preferences only.\nRead names / outputs / reconnect options apply on the next connection.', wraplength=1000, padding=6).pack(anchor='w')
        self.footer = ttk.Label(root, text='', padding=(14, 5, 14, 10), wraplength=1200)
        self.footer.pack(fill='x')
        self.render_table()
        self.render_status()
        if self.settings.error:
            self.diagnostic = self.settings.error
        root.bind('<Configure>', self.resize_labels)
        root.after(60, self.tick)

    def resize_labels(self, event):
        if event.widget is self.root:
            width = max(300, event.width-28)
            self.banner.configure(wraplength=width)
            self.footer.configure(wraplength=width)

    def make_tree(self, parent, columns, extended=False):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show='headings', selectmode='extended' if extended else 'browse')
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=55, anchor='w', stretch=key in ('name', 'state', 'end', 'channel'))
        sy = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        sx = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        tree.grid(row=0, column=0, sticky='nsew')
        sy.grid(row=0, column=1, sticky='ns')
        sx.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def save(self):
        try:
            self.settings.save(self.profile_ip, self.profile, self.root.geometry())
            self.settings.error = ''
        except (OSError, ValueError) as exc:
            self.settings.error = f'Could not save preferences: {exc}'

    def change_profile(self):
        if self.worker is not None:
            return
        try:
            new_ip = str(ipaddress.IPv4Address(self.ip.get().strip()))
        except ValueError:
            return
        if new_ip == self.profile_ip:
            return
        self.save()
        self.profile_ip = new_ip
        self.profile = self.settings.profile(new_ip)
        self.model = Monitor(self.profile)
        self.latest.clear()
        self.outputvar.set(self.profile['outputs'])
        self.namesvar.set(self.profile['names'])
        self.reconnectvar.set(self.profile['reconnect'])
        self.soundvar.set(self.profile['sound'])
        self.raisevar.set(self.profile['raise_window'])
        self.diagnostic = 'Mixer profile changed; statistics start a new session for this IP.'
        self.render_table()

    def transport_changed(self):
        self.profile.update(outputs=self.outputvar.get(), names=self.namesvar.get(), reconnect=self.reconnectvar.get())
        self.save()
        self.render_table()

    def alert_options(self):
        self.profile.update(sound=self.soundvar.get(), raise_window=self.raisevar.get())
        self.save()

    def connect(self):
        if self.worker is not None:
            return
        try:
            ip = str(ipaddress.IPv4Address(self.ip.get().strip()))
        except ValueError:
            messagebox.showerror('WING IP', 'Enter a valid IPv4 address.', parent=self.root)
            return
        self.change_profile()
        self.transport_changed()
        self.save()
        self.events = queue.SimpleQueue()
        self.stop = threading.Event()
        self.requested = True
        self.status = 'Connecting…'
        self.worker = MeterWorker(ip, self.events, self.stop, outputs=self.profile['outputs'], names=self.profile['names'], reconnect=self.profile['reconnect'])
        self.set_controls(True)
        self.worker.start()

    def set_controls(self, active):
        self.connectbtn.configure(state='disabled' if active else 'normal')
        self.disconnectbtn.configure(state='normal' if active else 'disabled')
        self.ipentry.configure(state='disabled' if active else 'normal')
        for check in self.transport_checks:
            check.configure(state='disabled' if active else 'normal')

    def disconnect(self):
        self.requested = False
        self.stop.set()
        self.status = 'Disconnecting…'
        self.record_changes(self.model.gap(reason='Manual disconnect'))
        self.latest.clear()
        self.connection_event('Manual disconnect', '')

    def connection_event(self, kind, detail):
        self.model.connections.append((now_stamp(), kind, detail))
        self.logger.write(kind, detail=detail)

    def record_changes(self, changes):
        raise_alert = False
        for kind, e in changes:
            self.logger.write('Alert ' + kind, e.key, e.name, e.severity, e.peak, round(e.duration, 3), e.reason or f'Threshold {e.threshold:g} dBFS')
            if kind == 'start':
                raise_alert = True
                if e.severity == 'critical' and self.profile['sound'] and time.monotonic() - self.last_beep >= 2:
                    try:
                        import winsound
                        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                    except (ImportError, RuntimeError):
                        self.root.bell()
                    self.last_beep = time.monotonic()
        if raise_alert:
            self.show_popup(raise_window=self.profile['raise_window'])

    def tick(self):
        try:
            for _ in range(4000):
                try:
                    kind, payload = self.events.get_nowait()
                except queue.Empty:
                    break
                if kind == 'meters':
                    mono, stamp, report, values = payload
                    self.latest[report] = mono
                    self.record_changes(self.model.feed(values, mono, stamp))
                    self.status = 'Receiving meters'
                    self.retry = ''
                elif kind == 'name':
                    key, name = payload
                    self.model.names[key] = name
                elif kind == 'diagnostic':
                    self.diagnostic = payload
                elif kind == 'connected':
                    self.status = 'Connected — waiting for meter reports'
                    self.connection_event('Connected', payload)
                elif kind == 'lost':
                    self.status = payload
                    self.latest.clear()
                    self.record_changes(self.model.gap(reason='Monitoring interrupted'))
                    self.connection_event('Monitoring lost', payload)
                elif kind == 'retry':
                    self.retry = payload
                elif kind == 'status':
                    self.status = payload
                elif kind == 'ended':
                    self.worker = None
                    self.set_controls(False)
                    self.latest.clear()
                    self.record_changes(self.model.gap(reason='Disconnected'))
                    if not self.requested:
                        self.status = 'Disconnected — not monitoring'
                        self.retry = ''
            # Guard against queued/stale samples even if the worker is delayed.
            stale_keys = [key for key, last in self.model.last.items() if time.monotonic() - last > STALE_SECONDS]
            if stale_keys:
                self.record_changes(self.model.gap(stale_keys, reason='Meter gap'))
            self.ui_ticks += 1
            if self.ui_ticks % 3 == 0:
                self.render_table()
                self.render_status()
                if self.popup is not None:
                    self.update_popup()
                if self.tabs.index(self.tabs.select()) == 1:
                    self.draw_graph()
                if self.tabs.index(self.tabs.select()) == 2:
                    self.render_alerts()
        except Exception as exc:
            # Unexpected local error is visible; do not silently present stale healthy state.
            self.status = f'Local monitoring error: {exc}'
            self.latest.clear()
            self.model.gap(reason='Local error')
            self.logger.write('Local error', detail=str(exc))
            self.render_status()
        self.root.after(60, self.tick)

    def render_status(self):
        reports = [INPUT_REPORT] + ([OUTPUT_REPORT] if self.profile['outputs'] else [])
        fresh = self.worker is not None and not self.stop.is_set() and all(time.monotonic() - self.latest.get(r, -1e10) <= STALE_SECONDS for r in reports)
        self.statuslabel.configure(text=self.status + (' • ' + self.retry if self.retry else ''), bg='#13795b' if fresh else '#842029')
        held = self.model.latched
        critical = any(e.severity == 'critical' for e in held.values())
        names = ', '.join(f'{BY_KEY[k].label} {self.model.title(k)}' if self.model.title(k) != BY_KEY[k].label else BY_KEY[k].label for k in held)
        self.banner.configure(text=f'{len(held)} HELD ALERT(S) — {names}' if held else 'No unacknowledged warnings', bg='#b02a37' if critical else '#ffe69c' if held else '#eaf0f4', fg='white' if critical else '#17354a')
        errors = ' | '.join(e for e in (self.settings.error, 'CSV logging failed: ' + self.logger.error if self.logger.error else '') if e)
        self.footer.configure(text=errors or 'Read only • Sampled level warnings, not hardware clip flags • Session totals survive reconnects • All preferences are local to this PC')
        self.diagtext.configure(state='normal')
        self.diagtext.delete('1.0', 'end')
        self.diagtext.insert('1.0', f'WING Input Watch {VERSION}\n{self.status}\n{self.retry}\n{self.diagnostic}\nLocal audit log: {self.logger.path}')
        self.diagtext.configure(state='disabled')

    def visible(self, strip):
        key = strip.key
        group = self.filtervar.get()
        if strip.is_output and not self.profile['outputs']:
            return False
        if group == 'All enabled' and not self.model.enabled(key):
            return False
        if group == 'Inputs' and strip.is_output or group == 'Outputs' and not strip.is_output:
            return False
        if group == 'Warnings only' and key not in self.model.latched and not any(t[0] == key for t in self.model.active):
            return False
        query = self.searchvar.get().strip().lower()
        return not query or query in f'{strip.label} {self.model.title(key)}'.lower()

    def render_table(self):
        selected = set(self.table.selection())
        for strip in STRIPS:
            key = strip.key
            if not self.visible(strip):
                if self.table.exists(key):
                    self.table.detach(key)
                continue
            pair = self.model.levels.get(key)
            peak = self.model.peaks[key]
            state = self.model.level_state(key)
            held = self.model.latched.get(key)
            tag = 'critical' if state == 'CRITICAL' else 'warning' if state == 'Warning' else 'disabled' if not self.model.enabled(key) else 'held' if held else ''
            if held:
                state += ' • held ' + held.severity
            if not pair:
                state = ('Disabled' if not self.model.enabled(key) else 'No live data') + (' • held ' + held.severity if held else '')
            w, c = thresholds(self.profile, key)
            counts, durations = self.model.counts[key], self.model.durations[key]
            row = (strip.label, self.model.title(key), 'Output' if strip.is_output else 'Input',
                   f'{pair[0]:.1f}' if pair else '—', f'{pair[1]:.1f}' if pair else '—', f'{peak:.1f}' if peak > -128 else '—',
                   f'{w:g} / {c:g}', f'{counts["warning"]} / {counts["critical"]}',
                   f'{durations["warning"]:.1f} / {durations["critical"]:.1f}', state)
            if self.table.exists(key):
                self.table.reattach(key, '', 'end')
                self.table.item(key, values=row, tags=(tag,) if tag else ())
            else:
                self.table.insert('', 'end', iid=key, values=row, tags=(tag,) if tag else ())
        # Reattaching must not select hidden rows or lose the user's selections.
        self.table.selection_set([k for k in selected if self.table.exists(k) and self.table.parent(k) == '' and k in self.table.get_children('')])

    def row_selected(self, event=None):
        selection = self.table.selection()
        if selection:
            self.graphkey.set(selection[0])

    def render_alerts(self):
        items = self.model.episodes[-1000:]
        existing = set(self.alerttable.get_children())
        wanted = set()
        for index, e in enumerate(items, start=max(0, len(self.model.episodes)-1000)):
            key = str(index)
            wanted.add(key)
            row = (e.started, f'{BY_KEY[e.key].label} / {e.name}', e.severity.upper(), f'{e.peak:.1f}', f'{e.duration:.2f}', e.reason or 'Active')
            if key in existing:
                self.alerttable.item(key, values=row)
            else:
                self.alerttable.insert('', 0, iid=key, values=row)
        for key in existing - wanted:
            self.alerttable.delete(key)

    def draw_graph(self):
        c = self.canvas
        c.delete('all')
        key = self.graphkey.get()
        if key not in BY_KEY:
            return
        self.graphlabel.configure(text=f'{self.model.title(key)} • {"output" if BY_KEY[key].is_output else "input"} L/R maximum')
        width, height = max(c.winfo_width(), 200), max(c.winfo_height(), 160)
        left, right, top, bottom = 60, width-24, 25, height-40
        w, critical = thresholds(self.profile, key)
        low = min(-60., math.floor((w-6)/10)*10)
        def y(value):
            return bottom - (max(low, min(0, value))-low) / -low * (bottom-top)
        for value in range(int(low), 1, 10):
            yy = y(value)
            c.create_line(left, yy, right, yy, fill='#294354')
            c.create_text(left-9, yy, text=str(value), anchor='e', fill='#b8c8d4')
        c.create_text(left, 10, text='dBFS', anchor='w', fill='#b8c8d4')
        for value, color in ((w, '#ffd166'), (critical, '#ff6b6b')):
            c.create_line(left, y(value), right, y(value), fill=color, dash=(5, 4))
            c.create_text(right-2, y(value)-8, text=f'{value:g}', anchor='e', fill=color)
        now = time.monotonic()
        previous = None
        count = 0
        for stamp, value in self.model.histories[key]:
            if stamp < now-120 or value is None:
                previous = None
                continue
            point = (left + (stamp-(now-120))/120 * (right-left), y(value))
            if previous is not None and stamp - previous[0] <= STALE_SECONDS:
                c.create_line(previous[1], previous[2], *point, fill='#55d6bd', width=2)
            previous = (stamp, *point)
            count += 1
        for seconds in (120, 90, 60, 30, 0):
            xx = left + (120-seconds)/120*(right-left)
            c.create_text(xx, bottom+18, text='now' if seconds == 0 else f'−{seconds}s', fill='#b8c8d4')
        if not count:
            c.create_text((left+right)/2, (top+bottom)/2, text='No recent meter history for this channel', fill='#b8c8d4')

    def local_settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('Local defaults — no WING settings changed')
        dialog.transient(self.root)
        pane = ttk.Frame(dialog, padding=18)
        pane.pack(fill='both', expand=True)
        ttk.Label(pane, text='Default thresholds (dBFS)', font=('Segoe UI', 12, 'bold')).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))
        warning, critical = tk.StringVar(value=str(self.profile['warning'])), tk.StringVar(value=str(self.profile['critical']))
        for row, (label, value) in enumerate((('Warning', warning), ('Critical', critical)), 1):
            ttk.Label(pane, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=5)
            ttk.Entry(pane, textvariable=value, width=12).grid(row=row, column=1)
        ttk.Label(pane, text='Applies to channels without overrides.\nThreshold edits work while connected and are local only.').grid(row=3, column=0, columnspan=2, sticky='w', pady=10)
        def apply():
            try:
                w, c = float(warning.get()), float(critical.get())
                validate_thresholds(w, c)
                for key, item in self.profile['strips'].items():
                    validate_thresholds(w if item['warning'] is None else item['warning'], c if item['critical'] is None else item['critical'])
            except ValueError as exc:
                messagebox.showerror('Thresholds', str(exc), parent=dialog)
                return
            self.record_changes(self.model.gap(reason='Local thresholds changed'))
            self.profile.update(warning=w, critical=c)
            self.logger.write('Local defaults changed', detail=f'Warning {w:g}; critical {c:g}')
            self.save()
            self.render_table()
            dialog.destroy()
        ttk.Button(pane, text='Save local defaults', command=apply).grid(row=4, column=0, columnspan=2, sticky='ew')

    def configure_selected(self):
        keys = list(self.table.selection())
        if not keys:
            messagebox.showinfo('Channel settings', 'Select one or more rows first. Use “All channels” to find disabled rows.', parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Channel preferences — local only')
        dialog.transient(self.root)
        pane = ttk.Frame(dialog, padding=18)
        pane.pack(fill='both', expand=True)
        ttk.Label(pane, text=f'{len(keys)} selected: ' + ', '.join(BY_KEY[k].label for k in keys[:8]), wraplength=480).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))
        first = self.profile['strips'][keys[0]]
        enabled = tk.BooleanVar(value=first['enabled'])
        use_defaults = tk.BooleanVar(value=first['warning'] is None and first['critical'] is None)
        w, c = thresholds(self.profile, keys[0])
        warning, critical = tk.StringVar(value=str(w)), tk.StringVar(value=str(c))
        alias = tk.StringVar(value=first['alias'])
        ttk.Checkbutton(pane, text='Enable alerts for selected channels', variable=enabled).grid(row=1, column=0, columnspan=2, sticky='w')
        ttk.Checkbutton(pane, text='Use global local defaults', variable=use_defaults).grid(row=2, column=0, columnspan=2, sticky='w', pady=8)
        for row, (label, variable) in enumerate((('Warning dBFS', warning), ('Critical dBFS', critical)), 3):
            ttk.Label(pane, text=label).grid(row=row, column=0, sticky='w', pady=5, padx=(0, 15))
            ttk.Entry(pane, textvariable=variable, width=12).grid(row=row, column=1)
        if len(keys) == 1:
            ttk.Label(pane, text='Local name override').grid(row=5, column=0, sticky='w', pady=5)
            ttk.Entry(pane, textvariable=alias, width=26).grid(row=5, column=1)
        ttk.Label(pane, text='Blank local name uses the name read from the WING.\nThis does not rename anything on the mixer.\nFor multiple rows, these choices replace each selected row’s preferences.').grid(row=6, column=0, columnspan=2, sticky='w', pady=12)
        def apply():
            try:
                w, c = (self.profile['warning'], self.profile['critical']) if use_defaults.get() else (float(warning.get()), float(critical.get()))
                validate_thresholds(w, c)
            except ValueError as exc:
                messagebox.showerror('Thresholds', str(exc), parent=dialog)
                return
            self.record_changes(self.model.gap(keys, reason='Local channel preferences changed'))
            for key in keys:
                item = self.profile['strips'][key]
                item.update(enabled=enabled.get(), warning=None if use_defaults.get() else w, critical=None if use_defaults.get() else c)
                if len(keys) == 1:
                    item['alias'] = ''.join(ch for ch in alias.get() if ch.isprintable())[:64]
                self.logger.write('Local channel preferences changed', key=key, name=self.model.title(key), detail=f'Enabled {enabled.get()}; warning {w:g}; critical {c:g}')
            self.save()
            self.render_table()
            dialog.destroy()
        ttk.Button(pane, text='Save local preferences', command=apply).grid(row=7, column=0, columnspan=2, sticky='ew')

    def show_popup(self, raise_window=True):
        if self.popup is None:
            self.popup = tk.Toplevel(self.root)
            self.popup.title('WING — held threshold alerts')
            self.popup.geometry('700x450')
            self.popup.protocol('WM_DELETE_WINDOW', self.popup.withdraw)
            self.popupheader = tk.Label(self.popup, text='THRESHOLD ALERTS', font=('Segoe UI', 18, 'bold'), fg='white', bg='#b02a37', pady=12)
            self.popupheader.pack(fill='x')
            pane = ttk.Frame(self.popup)
            pane.pack(fill='both', expand=True)
            self.popuptext = tk.Text(pane, wrap='word', font=('Consolas', 11), padx=12, pady=12)
            scroll = ttk.Scrollbar(pane, command=self.popuptext.yview)
            self.popuptext.configure(yscrollcommand=scroll.set)
            self.popuptext.pack(side='left', fill='both', expand=True)
            scroll.pack(side='right', fill='y')
            ttk.Button(self.popup, text='Acknowledge all (local only)', command=self.ack).pack(pady=12)
            self.popup.withdraw()
        self.update_popup()
        if raise_window:
            self.popup.deiconify()
            self.popup.attributes('-topmost', True)
            self.popup.lift()

    def update_popup(self):
        text = '\n\n'.join(f'{BY_KEY[k].label} — {self.model.title(k)}\n{e.severity.upper()} ≥ {e.threshold:g} dBFS | peak {e.peak:.1f} dBFS\n{e.started}\n{e.reason or "Still above threshold"}' for k, e in self.model.latched.items())
        critical = any(e.severity == 'critical' for e in self.model.latched.values())
        self.popupheader.configure(bg='#b02a37' if critical else '#9a6700')
        self.popuptext.configure(state='normal')
        self.popuptext.delete('1.0', 'end')
        self.popuptext.insert('1.0', text or 'No unacknowledged warnings.')
        self.popuptext.configure(state='disabled')

    def ack(self):
        self.model.acknowledge()
        self.logger.write('Warnings acknowledged locally')
        if self.popup:
            self.popup.withdraw()
        self.render_table()
        self.render_status()

    def copy_diagnostics(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(f'WING Input Watch {VERSION}\n{self.status}\n{self.retry}\n{self.diagnostic}')
        self.root.update_idletasks()

    def export(self):
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension='.zip', initialfile='WING-event-report-' + datetime.now().strftime('%Y%m%d-%H%M') + '.zip', filetypes=[('Report bundle', '*.zip'), ('Alert CSV', '*.csv')])
        if not path:
            return
        try:
            if path.lower().endswith('.csv'):
                Path(path).write_text('\ufeff' + csv_text(report_rows(self.model)[1]), encoding='utf-8')
            else:
                export_report(self.model, path)
            messagebox.showinfo('Export complete', 'Event report saved.', parent=self.root)
        except OSError as exc:
            messagebox.showerror('Export failed', str(exc), parent=self.root)

    def close(self):
        self.requested = False
        self.stop.set()
        self.record_changes(self.model.gap(reason='Application closed'))
        self.logger.write('Application closed')
        self.save()
        self.root.destroy()


if __name__ == '__main__':
    App(tk.Tk()).root.mainloop()
