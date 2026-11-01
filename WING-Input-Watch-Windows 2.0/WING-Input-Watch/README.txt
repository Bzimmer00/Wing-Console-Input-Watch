WING INPUT WATCH 2.0 — WINDOWS / STRICT READ ONLY
================================================

UPGRADE FROM 1.1
1. Close the old monitor. Keep its folder as a fallback.
2. Extract this ZIP to a NEW folder; keep wing_monitor.py and wing_core.py together.
3. Double-click Start-WING-Monitor.bat. Use the same Python installation as before.
4. Enter the WING's IP address. The first run of v2 starts with new preferences.
5. Connect and compare the app's meters with your console at soundcheck.

Python 3.10 or newer with Tcl/Tk is required. No third-party packages are needed.
The program is inspectable Python source, not a signed Windows EXE.

WHAT'S NEW
* Names: optional read-only $name requests read the display names from the WING.
  Replies refresh in the background. Missing names never block metering.
  A blank/unavailable name falls back to CH 1, AUX 1, etc. No names are written.
* Two thresholds: yellow WARNING, red CRITICAL. Defaults: -6 and -1 dBFS.
* Per-channel preferences: custom thresholds, enable/disable alerts, local alias.
* Channel selection, search and filters, including a Warnings only view.
* Output meters: optional buses 1–16, mains 1–4, matrices 1–8 (28 outputs).
* 120-second rolling level graph of each channel's maximum of L/R.
* Counts and observed duration above both thresholds, plus session peaks.
* Optional PC sound on a new critical event; sound is OFF by default.
* Saved IP, thresholds, channel preferences, sound/popup choices and window size.
  Preferences are separate for each mixer IP address.
* Automatic reconnect with a 2/4/8/15-second backoff. Manual Disconnect stops it.
* Event reports: HTML report and three CSVs in one ZIP; optional alert-only CSV.
* Automatic CSV audit log includes alerts, recoveries, interruptions and edits.

USING THE PROGRAM
Connection row:
  Include outputs / Read names / Auto reconnect apply when you next Connect.
  These checkboxes are disabled during a connection/reconnect attempt.
  Include outputs is OFF initially; only inputs are required for a green status.
  Read names and Auto reconnect start ON.

Local settings:
  Set global Warning and Critical values at any time, including while connected.
  Valid range: -90 <= warning < critical <= 0 dBFS.
  Changes end current measurement episodes and are logged locally; the next
  received meter sample is evaluated against the new settings.

Configure selected:
  Select a row or several rows with Ctrl/Shift, then click Configure selected.
  Double-clicking a row also opens its preferences.
  Enable or disable alerts; use global defaults or enter custom thresholds.
  A single channel can have a local name override, e.g. "Lectern".
  Leave the local name blank to use the WING's display name.
  A multi-row save applies the shown preferences to all selected rows.
  Find disabled channels using the All channels view to re-enable them.
  Disabled channels still have meter/history data when reports arrive; they
  do not generate new threshold alerts. Acknowledgment clears any older latch.

Alerts:
  Each crossing starts an episode. Critical crossing escalates the held popup.
  An episode ends at the next below-threshold report or when data is interrupted.
  Alerts stay latched after recovery or loss of connection until acknowledged.
  Acknowledge all is local only; current high levels still show in the table.
  Acknowledgment does not create a new alert while a threshold stays exceeded.
  Recovery followed by another crossing creates a new alert.
  Closing the popup hides it without acknowledging it.
  Raise warning window OFF keeps new warnings in the main banner/history.
  PC sound plays only for critical starts, at most once every two seconds.
  It uses Windows' default sound output: choose headphones or an appropriate
  output before enabling it, especially if the PC's audio is routed to the PA.

Graphs and statistics:
  Select a row, then open Level history; or choose a channel key on that tab.
  Graphs retain up to 120 seconds at up to 10 points/second. Every meter report
  is still checked for alerts; the graph is a decimated visualization.
  Graph lines break across meter gaps. Threshold lines show CURRENT settings.
  Warning counts/duration include critical episodes as a subset.
  Duration is approximate time between observed reports, not an audio-sample
  measurement. Missing data and reconnect downtime are excluded.
  Threshold statistics survive reconnects to the same IP during this app run.
  They do not reload from disk after a restart. Audit logs remain on disk.
  Switching to a different IP starts a fresh statistics session. Export the
  current report first if you want a report for the old IP.

Reports:
  Export event report saves a ZIP containing report.html, channel-summary.csv,
  threshold-episodes.csv and connections.csv. Extract it and open report.html.
  Selecting Alert CSV instead exports only threshold episodes.
  Reports include recovered/acknowledged alerts, currently active episodes,
  session peaks, counts, observed durations, connections and interruptions.
  The history tab shows the latest 1,000 episodes; exports include the full run.
  Audit logs record episode starts/ends. Export captures current accumulated
  duration for still-active episodes; close normally to log their final state.

Local files:
  %LOCALAPPDATA%\WingInputWatch\settings-v2.json
  %LOCALAPPDATA%\WingInputWatch\events-v2-<date-time>.csv
  The v1.1 high-input-history.csv is not modified. Settings are saved locally,
  not sent to the WING. No internet connection is used by the running app.

CONNECTION STATUS / TROUBLESHOOTING
Green means recent valid reports from every enabled meter group.
Red means disconnected, connecting, reconnecting or monitoring lost.
A three-second gap ends measurement episodes and clears live values. Latches
and session peaks remain visible. No live data is never treated as silence.

The Connection & read-only policy tab shows packet counts and Copy diagnostics.
  Meter packets 0: no meter packets have reached the app's UDP socket.
  Packets >0 with Valid 0: share diagnostics; format compatibility needs checking.
  Missing names: keep your desired locks enabled; labels can fall back to CH/AUX.

Network requirements:
  WING TCP 2222 for fixed meter subscriptions and renewals.
  WING UDP 2222 for discovery; PC UDP 2224 for incoming reports.
  Optional WING UDP 2223 for read-only name queries; replies use the requesting
  socket's dynamically assigned local port.
  Discovery and meter reception use the same UDP 2224 socket, preserving the
  working v1.1 return-traffic behavior. Only one Input Watch can use that port.
  Windows Firewall / network ACLs must permit the relevant traffic. The app
  never adds firewall rules, changes VLANs, or changes the console's network.

STRICT READ ONLY
Read READ_ONLY.txt for the exact network allowlist and its tests.
All buttons and edits in this interface are local to the Windows application.
There are no gain/fader/mute/routing/phantom-power/scene/RTA-source/lock commands.
The console's TCP Remote Lock can remain enabled as an extra safeguard. This
app never changes it. It can affect other remote-control apps, so keep the
lock state appropriate for your existing workflow.

LIMITS / VALIDATION
These are sampled level warnings, NOT confirmed hardware clipping flags.
Input strips use their reported input L/R meters; output groups use reported
output L/R meters. Unpatched physical inputs are not monitored. Very brief
peaks between reports, upstream distortion, or clipping elsewhere can be missed.

Development checks passed: 24 protocol/state/persistence/network tests, plus
one GUI integration test. Network tests use a simulated WING on loopback and
capture outgoing messages. GUI flows were exercised and visually checked in a
Linux virtual display. v2 has NOT been tested against a physical WING or a
Windows desktop. Names/output-meter support may vary with console firmware.
The original input subscription layout and UDP return port match working v1.1.

To run the non-GUI tests (they contact a simulator, not your WING):
  py -3 -m unittest discover -s . -v
Optional GUI test opens a window and also uses only a loopback simulator:
  set WING_GUI_TEST=1
  py -3 -m unittest test_gui -v
Tests temporarily use loopback TCP/UDP ports 2222/2223 and local UDP 2224;
close other copies before testing.

FILES
wing_monitor.py          Windows desktop interface; local controls only.
wing_core.py             Protocol allowlist, monitoring, preferences and reports.
Start-WING-Monitor.bat   Launcher, no installation or configuration changes.
test_monitor.py          Protocol / state / persistence / network tests.
test_gui.py              Optional GUI integration test.
READ_ONLY.txt            Auditable read-only policy.
README.txt               This guide.
