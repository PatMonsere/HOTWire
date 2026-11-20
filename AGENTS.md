# AGENTS.md: notes for coding agents working on HOTWire

HOTWire is a PySide6 desktop app for an FRC team's pit laptop (Windows). It talks to two devices on the robot network:

- **SystemCore** (robot controller, `10.0.67.2`, SSH user `systemcore`): has the WPILib `.wpilog` and CTRE `.hoot` logs in `/home/systemcore/logs`, and runs the **NetworkTables 4 server** on port 5810.
- **Jetson** (HOTVision vision coprocessor, `10.0.67.7`, SSH user `hotros`): runs `hotvision.service` (a Flask app on port 5800 with MJPEG streams) and publishes camera data to the robot's NT under `/HOTVision/<camera>/...`.

Read `README.md` for what users see. This file explains how the code fits together and which rules must not break.

## Hard rules (safety: don't weaken these without the user asking)

1. **Field gate** (`hotwire/robotstate.py: gate`). Before any SSH, file listing, download, delete, Jetson poll or camera stream, code calls `ctx.gate(manual, transfer)`:
   - FMS attached → nothing (the robot is on the field; bandwidth there is limited and shared).
   - Robot enabled → no downloads or deletes (`transfer=True`). Read-only streams and health are OK.
   - State unknown → auto mode does nothing; manual actions must ask the user to confirm they're in the pits.
   - Competition lock (`settings.competition_lock`, or `lock_when_processes`) → no connections at all, and the NT client is stopped.
   Long-running work must **re-check the gate** while it runs (see `LogsTab._start_sync.still_allowed`).
2. **Delete only after verification** (`hotwire/logsync.py: sync_file`). The robot file is removed only when the local SHA-256 equals the SHA-256 computed **on the robot** (`sha256sum`). If the robot can't hash, never auto-delete. On a mismatch, discard the local copy and keep the robot file.
3. **Never touch the file being written.** `in_use` = modified within `in_use_seconds`, judged by the **robot's clock** (`date +%s`), because the SystemCore clock is often wrong.
4. **Passwords are never stored.** `sshconn.install_key` uses the password once to append the laptop's public key to `~/.ssh/authorized_keys`.
5. **Streams stop** when the Cameras tab is hidden, the window is minimized or closed to the tray, the gate fails or the app quits. Use HOTVision's overlay stream `/stream/<cam>/<pipeline>` (capped at ~30 fps, counted as a viewer). Don't use `/stream/<cam>/raw`: it has no fps cap.
6. **HOTWire never publishes to NetworkTables.** It only subscribes. Keep it that way.

## Two NetworkTables backends (and Windows Smart App Control)

Windows **Smart App Control** blocks unsigned DLLs and EXEs, and it is on for some team laptops. With it on, robotpy's `pyntcore` can't load. So there are two clients with the same interface (`connected`, `connected_host`, `connect_time`, `backend`, `start/stop`, `subscribe/unsubscribe`, `add_listener`, `get`, `get_timestamped`, `topics`, `server_time_us`):

- `hotwire/nt_pyntcore.py`: `PyntcoreClient`, the official WPILib library. Preferred.
- `hotwire/nt4.py`: `NT4Client`, a pure-Python NT4 client (WebSocket via `websocket-client`, MessagePack via `msgpack`).

`nt4.make_client(settings.nt_backend, port)` chooses between them: `"auto"` uses pyntcore if it loads and falls back to built-in, or you can force `"pyntcore"` or `"builtin"`. Both clients must keep their subscriptions across `stop()`/`start()`, because the competition lock does that. The tests run against both.

Other consequences of Smart App Control:

- `hotwire/pitcheck/wpilog.py` is a pure-Python `.wpilog` reader, so it doesn't depend on robotpy.
- Two ways to ship HOTWire. `build_exe.ps1` makes a single-file PyInstaller `dist\HOTWire.exe` (unsigned, so it's blocked where Smart App Control is on). It bundles pyntcore's native DLLs with `--collect-all ntcore/wpiutil/wpinet/native`, `examples/pit_checks.yaml` and `assets/hotwire.ico`, and excludes unused Qt modules. The exe is attached to GitHub Releases and not committed. `install.ps1`/`install.bat` create a venv on the signed python.org Python 3.13 with `pythonw -m hotwire` shortcuts, which works everywhere. Bundled data files are found through `sys._MEIPASS` when running frozen (see `pit_tab.EXAMPLE`).
- PySide6, numpy, paramiko/cryptography, msgpack and ruamel.yaml load even with Smart App Control on. Before adding a dependency with native code, keep the app working when that dependency is blocked.

## Layout

```
hotwire/
  app.py            MainWindow: header (state badge + competition lock), tabs, activity log, tray, 1 s tick
  settings.py       Settings/Device/HostEntry dataclasses -> %APPDATA%\HOTWire\settings.json (HOTWIRE_HOME overrides)
  nt4.py            make_client() + NT4Client (built-in): background thread, reconnect, subscribe, values, time sync
  nt_pyntcore.py    PyntcoreClient: same interface on the official pyntcore library
  robotstate.py     RobotStateReader (/DriverStation and /FMSInfo FMSControlData bits) + gate()
  sshconn.py        Paramiko connect (key or password, tries hosts in order, "via" jump through another device),
                    trust-on-first-use known_hosts in the app data folder, ensure_key/install_key/test_key
  logsync.py        RemoteFS protocol + SSHRemoteFS, list_logs, destination(), Index (download_index.json),
                    download (.part + speed limit + cancel), sync_file (verify -> record -> delete)
  jetson.py         snapshot commands (journalctl, camera_config.json, system report), live health script + parser
  streams.py        TcpLink (direct or via SSH channel), http_get_json, StreamReader (MJPEG parse + quality stats)
  signals.py        Signals tab data (UI-free): LiveBuffer, load_file (.wpilog/.csv), value_at, window_stats,
                    topic tree, unit guesses, Layout/StripCfg/TraceCfg (saved as JSON)
  pitcheck/
    config.py       YAML schema + validation (ruamel round-trip so comments survive "Learn")
    analyze.py      analyze() for live and final mode, Metric/CheckResult, learn(), apply_suggestions()
    recorder.py     Recorder: NT send-all subscription -> {topic: [(t, v)]}, CSV save/load
    wpilog.py       read_wpilog(): numeric entries -> recording format
  ui/
    context.py      AppContext (QObject): settings, NT client, state, gate(), log(), signals
    common.py       Job (background thread -> Qt signals), status colors, app icon (HOT 67 logo, assets/hotwire.ico)
    logs_tab.py     Auto/Manual log sync UI
    pit_tab.py      recording, live analysis, chart (QtCharts), Learn dialog, YAML editor with signal picker
    signals_tab.py  signal viewer: list panel, values panel, live/file sources, layouts
    signal_strips.py pyqtgraph strips: shared time axis, L/R Y axes, mouse gestures, cursors, drag-drop targets
    side_panel.py   MDA-style side panels: pop out on hover over the plot, or pinned in the splitter
    cameras_tab.py  stream tiles from /HOTVision NT topics (only cameras with a 'connected' topic)
    jetson_tab.py   health polling while visible, snapshots ("since last snapshot" uses the Jetson's clock)
    settings_tab.py settings form, device dialog, key setup, forget host key
tools/fake_robot.py tiny NT4 server that simulates the robot (pit test signals, cameras, FMS/enabled flags)
examples/pit_checks.yaml  example checks using the real robot's topic names
tests/              pytest: logsync safety (LocalFS fake), analysis, config, wpilog, gate,
                    NT4 vs fake robot, MJPEG reader vs fake server, offscreen GUI smoke test
```

## Data formats and conventions

- **Recording**: `dict[topic, list[(t_seconds, value)]]`, sorted, values sample-and-hold. Topics always start with `/` (`nt4.norm()`). Live recordings use the robot's (server) timestamps.
- **Robot NT topics** (from the robot repo `Rebuilt_Systemcore`, `frc/util/Kraken.java`): `/HOT/<Subsystem>/<Device>/<Subsystem><Device>{Position,Velocity,Supply Current}`. Setpoints are **not** published, so checks use fixed targets in YAML. The pit test routine (`RobotContainer.getPitTestRoutine`, branch `pit_test_mode`) runs in Utility mode, and the driver's *back* button moves to the next step. The step names are only `System.out` prints, so checks find their start with `start_when` rising edges, in order.
- **HOTVision NT**: `/HOTVision/<cam>/{connected,capture_fps}`, `/HOTVision/<cam>/<pipeline>/{fps,latency,num_tags,...}`. `BACK` is a merged entry of BACK_LEFT and BACK_RIGHT and has no stream. From the HOTVision `jetson-heartbeat` branch: `/HOTVision/heartbeat/{seq,jetson_uptime_s,app_uptime_s,vin_v,vin_min_v,throttled,...}`, `/HOTVision/events/{last_event (sendAll),event_count,usb_disconnects}` and `/HOTVision/<cam>/status/...` (camera health, **not** a pipeline: `cameras_tab.NOT_PIPELINES`). The Jetson tab subscribes to heartbeat/events only while visible.
- **USB errors** (`jetson.count_usb_errors`): kernel lines from this boot; the `fusb301` probe message is ignored, and resets count only after `USB_BOOT_SETTLE_S` of uptime.
- **Analysis**: a check starts at the first **rising edge** of `start_when` after the previous check's start (`in_order`). `final=False` is live mode: incomplete windows are `running`, but a `peak_max` breach fails immediately. `learn()` never changes targets (`final`, `settle_to`, `tolerance`).
- **Saved files**: logs go to `<save>/<device>/<EVENT>/<MATCH>/` for FRC_…_EVENT_MATCH.wpilog, otherwise `<save>/<device>/<date>/<relative path>`. Pit runs go to `<save>/PitTests/<stamp>/{recording.csv, report.txt, report.json, pit_checks.yaml}`. Jetson snapshots go to `<save>/<device>/snapshots/<stamp>/`.
- **Signals tab**: live subscriptions only while the tab is visible: `"/"` at 0.5 s for the tree values, plus send-all for the plotted topics. They stop when the FMS is attached, NT is down or the lock is on. Plot time is seconds from the live session start (`live_t0`, server clock) or from the file's first sample. Values are drawn as steps (sample-and-hold, `stepMode="left"`). **Units are never guessed**: only the topic's `"unit"` property (`nt4.unit_of`; `Topic.properties` on both NT clients, the entry metadata in a `.wpilog`), else blank. Upper strips keep their time axis with `showValues=False`, because pyqtgraph draws the vertical grid from the axis. State: `<data dir>/signals_state.json`; named layouts: `<data dir>/signal_layouts/*.json`. pyqtgraph needs `QtOpenGL`, `QtOpenGLWidgets`, `QtSvg` and `QtTest`: don't exclude them in `build_exe.ps1`.
- **Window**: opens maximized; each tab sits in a `QScrollArea` (`MainWindow.show_tab(tab)` selects one) so small laptop screens scroll instead of cutting off the bottom.
- **UI threading**: all network or disk work runs in `Job` (a background thread). Results come back through Qt signals to the UI thread. Never block the UI thread on SSH or HTTP.

## Testing

```
.venv\Scripts\python -m pytest
```

All tests run without hardware. Tests use fixed local ports in the 158xx range (fake robot, fake MJPEG server). Things the tests **can't** prove, which need a check on the real robot:

- the real SystemCore NT4 server accepts this client (subprotocol, subscribe options, time sync);
- 2027 control word bit layout and the `/DriverStation` vs `/FMSInfo` table name (both are read);
- `sha256sum`, `date +%s` and `df -k` output on the SystemCore (BusyBox);
- SSH `direct-tcpip` forwarding through the SystemCore for "via" hosts;
- hoot file naming and folder layout.

## Style

Python 3.13, type hints, dataclasses, small modules. Keep user-facing messages plain and specific: students read them in the pits. When you change a safety rule or a data format, update this file and `README.md`.
