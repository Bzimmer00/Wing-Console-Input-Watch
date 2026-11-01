"""Optional GUI integration check: WING_GUI_TEST=1, with an available display."""
import os
import tempfile
import time
import unittest
import tkinter as tk
from pathlib import Path
from wing_core import Settings, INPUT_REPORT, now_stamp
from wing_monitor import App
from test_monitor import FakeWing


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def button(root, text):
    return next(w for w in descendants(root) if w.winfo_class() in ('TButton', 'TCheckbutton') and w.cget('text') == text)


@unittest.skipUnless(os.environ.get('WING_GUI_TEST') == '1', 'Set WING_GUI_TEST=1 to open GUI tests')
class GuiTest(unittest.TestCase):
    def test_connect_configure_graph_alert_ack_and_save(self):
        server = FakeWing()
        root = tk.Tk()
        failures = []
        root.report_callback_exception = lambda *args: failures.append(args)
        with tempfile.TemporaryDirectory() as folder:
            app = App(root, Settings(folder))
            try:
                app.ip.set('127.0.0.1')
                app.change_profile()
                app.outputvar.set(True)
                app.transport_changed()
                app.connect()
                deadline = time.monotonic()+6
                while time.monotonic() < deadline and (len(app.model.levels)<76 or not app.model.names):
                    root.update(); time.sleep(.02)
                self.assertEqual(len(app.model.levels), 76)
                self.assertTrue(app.model.names)
                app.render_table()
                self.assertEqual(len(app.table.get_children()), 76)
                # Configure an alias and custom thresholds using actual dialog controls.
                app.table.selection_set('ch/1')
                app.configure_selected(); root.update()
                dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
                button(dialog, 'Use global local defaults').invoke()
                entries = [w for w in descendants(dialog) if w.winfo_class() == 'TEntry']
                for entry, value in zip(entries, ('-12', '-3', 'Lectern')):
                    entry.delete(0, 'end'); entry.insert(0, value)
                button(dialog, 'Save local preferences').invoke(); root.update()
                self.assertEqual(app.model.title('ch/1'), 'Lectern')
                self.assertEqual(app.profile['strips']['ch/1']['critical'], -3)
                server.emit = False
                current = time.monotonic()
                app.events.put(('meters', (current, now_stamp(), INPUT_REPORT, {'ch/1': (0, -1)})))
                deadline = time.monotonic()+.4
                while time.monotonic()<deadline:
                    root.update(); time.sleep(.02)
                self.assertIn('ch/1', app.model.latched)
                self.assertEqual(app.popup.state(), 'normal')
                app.ack(); root.update()
                self.assertFalse(app.model.latched)
                self.assertEqual(app.popup.state(), 'withdrawn')
                self.assertIn('critical', app.table.item('ch/1', 'tags'))
                app.tabs.select(1); app.draw_graph(); root.update()
                self.assertGreater(len(app.canvas.find_all()), 10)
                app.tabs.select(2); app.render_alerts(); root.update()
                self.assertGreater(len(app.alerttable.get_children()), 0)
                app.tabs.select(3); app.render_status(); app.copy_diagnostics(); root.update()
                self.assertIn('WING Input Watch 2.0', root.clipboard_get())
                # Screenshot internal QA only; simulated data, no real user/session.
                if os.environ.get('WING_QA_SCREENSHOT'):
                    app.tabs.select(0); app.render_table(); root.update()
                    from PIL import ImageGrab
                    ImageGrab.grab(xdisplay=os.environ.get('DISPLAY')).save(os.environ['WING_QA_SCREENSHOT'])
                app.disconnect()
                deadline = time.monotonic()+2
                while time.monotonic()<deadline and app.worker is not None:
                    root.update(); time.sleep(.02)
                self.assertIsNone(app.worker)
                app.save()
                saved = Settings(folder).profile('127.0.0.1')
                self.assertEqual(saved['strips']['ch/1']['alias'], 'Lectern')
                self.assertFalse(failures, repr(failures))
            finally:
                app.stop.set()
                if app.worker: app.worker.join(4)
                root.destroy()
                server.close()


if __name__ == '__main__': unittest.main(verbosity=2)
