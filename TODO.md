# HOTWire TODO

## UI
- [x] Logs tab: **Select: All / None / New practice logs** buttons.
- [x] Open **maximized (full size)** on launch (also when restored from the tray).
- [x] Jetson tab: "USB errors" ignores the `fusb301` boot message; counts disconnects, over-current, descriptor read errors and resets after the first 60 s. Hover the number for the last one.
- [x] Jetson tab: heartbeat section (alive, vin / lowest vin, throttled, reboots, USB disconnects, event list) from `/HOTVision/heartbeat` and `/HOTVision/events` on NT. Shows "not published" until the `jetson-heartbeat` branch is on the Jetson.
- [x] Logs tab: "Modified (robot clock)" header fits; tabs scroll instead of being cut off on short screens (the window needed 833 px before).
- [x] Closing the window **quits the app** instead of hiding it in the tray ("minimize to tray" setting removed; "Start minimized" now minimizes to the taskbar).

## Release
- [ ] Bump `hotwire/__init__.py` `__version__` (still says 0.1.0; the window title and log show it). Release v0.2.0 was built from `signals-tab` with 0.1.0 in it.
- [ ] Merge `signals-tab` into `main`.

## Signals tab
Purpose: a quick **live** check in the pits ("look at this value while we get ready"). Match log analysis stays in AdvantageScope / CTRE Tuner X, which already load everything, so no more log-viewing features here (opening a `.wpilog` / `.csv` still works as it is).
- [x] Phase 1: pop-out/pinned signal list, strips with shared time axis and L/R Y axes, wheel / drag / right-drag box zoom, 2 cursors with Δ, colors, saved layouts, live (+ basic .wpilog/.csv opening).
- [ ] Phase 2: a special **discrete strip** (like AdvantageScope) for booleans, strings and enums: one lane per signal, colored bars for true/false or each state, with the text shown in the bar (enabled, mechanism states, HOTVision events).
- [ ] Phase 2: robot mode shading (disabled / auto / teleop) behind the live plots.
- [ ] Phase 2: unit conversion per signal (e.g. rot/s → rpm), like AdvantageScope's "Convert Units…".

## Robot test day (first test on the real robot)
Everything below has only run against `tools/fake_robot.py` so far. Write down what you see (screenshots of the Activity log help); anything that fails becomes a fix item.

### Before you start
- [ ] Laptop on the robot network (tether or radio). Note which, because download speed differs a lot.
- [ ] Settings: SystemCore and Jetson hosts correct, SSH key installed on both (**Set up key (password once)…**), **Test connection** passes.
- [ ] If the Driver Station runs on this laptop, leave **Competition lock while running** empty for this test, or HOTWire locks itself (no NT, no logs) as soon as `DriverStation.exe` starts. Test that setting separately at the end.
- [ ] Note **Settings → NetworkTables client**: which backend loaded (pyntcore or built-in)? Test both if you can (force each one, restart).
- [ ] Copy `%APPDATA%\HOTWire\settings.json` somewhere first, so you can go back.

### NetworkTables (never tested on the real SystemCore)
- [ ] Header shows NT **connected** to `10.0.67.2:5810` within a few seconds. If not, copy the Activity log error.
- [ ] Unplug / replug the tether (or reboot the robot): NT reconnects by itself.
- [ ] Robot state badge: **disabled** / **enabled** follow the Driver Station. Note which table works: `/DriverStation` or `/FMSInfo` (2027 control word bit layout).
- [ ] Enable the robot: downloads and deletes are blocked; Cameras and Jetson health still work.
- [ ] **Competition lock** on: NT disconnects, nothing connects. Off: NT comes back and subscriptions still work (Signals values update again).
- [ ] Robot code still runs normally with HOTWire connected (HOTWire only subscribes and never publishes).
- [ ] Battery badge says "n/a" (expected until the robot publishes `/HOT/Robot/BatteryVoltage`).

### Auto log pull
- Auto only runs when NT is connected and the robot is **disabled** with no FMS (state unknown = does nothing).
- Auto pulls **only FMS match logs** by default, and there's no FMS in the pits. To test it, uncheck **"Auto mode pulls only FMS match logs"** in Settings, then check it again afterward.
- [ ] With that box unchecked: auto finds the practice logs, skips the one being written (`in_use`, robot clock), downloads, verifies the SHA-256 **on the robot**, then deletes the robot copy. Check the files in `<save>/SystemCore/<date>/` and that `download_index.json` lists them.
- [ ] `sha256sum`, `date +%s` and `df -k` work on the SystemCore (if hashing fails, nothing gets deleted: check the Activity log).
- [ ] Enable the robot during a download: it stops, and the `.part` file is cleaned up or resumed later.
- [ ] Note the download speed (tether vs radio) and that a big log doesn't stall (the earlier SFTP fix).
- [ ] `.hoot` files: names and folder layout, and do they get pulled?
- [ ] Check the box again: practice logs are skipped again.
- [ ] Later, at a real event or with FMS: match log names (`WPILIB_<date>_<time>_<EVENT>_<MATCH>.wpilog`?) are recognized by `logsync.is_match_log` and saved to `<save>/SystemCore/<EVENT>/<MATCH>/`.

### Manual logs
- [ ] Manual list, **Select All / None / New practice logs**, download and delete work.
- [ ] State unknown (NT down): manual actions ask "are you in the pits?".

### Signals tab (live)
- [ ] **Live robot** source shows the `/HOT/...` tree with values updating (0.5 s).
- [ ] Plot a few Kraken values (velocity, supply current) while running motors: smooth steps, no gaps.
- [ ] Rate of send-all samples and pit laptop CPU with ~5 and ~20 plotted signals.
- [ ] Switch to another tab: live subscriptions stop (CPU drops); come back and they resume.
- [ ] Units column blank (expected until the robot publishes `"unit"` properties).
- [ ] Open a downloaded `.wpilog` from today: signals load and plot.

### Pit test: record live, then analyze afterward
Goal: run the robot's pit test routine and grade it in HOTWire, both while it runs and from the files afterward.

Robot side:
- [ ] Deploy the `pit_test_mode` branch of `Rebuilt_Systemcore`.
- [ ] Driver Station in **Utility** mode, enable; the driver's **back** button steps through the routine. Write down the step order from the console prints: the checks are matched in order by `start_when`.
- [ ] Check that DataLogManager logs NetworkTables in that branch (`DataLogManager.start()` + NT logging on), otherwise **Analyze a log file…** on the robot's `.wpilog` won't find the signals.

HOTWire setup:
- [ ] **Pit Test → Edit checks…** starts from `examples/pit_checks.yaml`. Use **Insert at cursor** / the signal picker with the robot live to confirm every alias points at a real topic (e.g. `HOT/Drive/TotalCurrent` and the names with spaces, like `Shooter/Left Top`).
- [ ] Put the checks in the same order as the robot's steps, and set `start_when` thresholds that only trip when that step starts.
- [ ] The limits are placeholders, so expect fails or warns on the first run. That's fine.

Live:
- [ ] **Start recording**, then run the routine. During the run, each check should show `running` and then pass / warn / fail as its window finishes; the chart follows along.
- [ ] A `peak_max` breach should fail right away (live mode).
- [ ] **Stop & analyze**: report is written to `<save>/PitTests/<stamp>/` (`recording.csv`, `report.txt`, `report.json`, `pit_checks.yaml`).

After the test:
- [ ] **Learn limits from this run…** on the healthy robot: the suggestions look sensible, targets (`final`, `settle_to`, `tolerance`) are unchanged, and the YAML comments survive the save.
- [ ] Run the routine again with the learned limits: everything passes.
- [ ] **Analyze a log file…** on the saved `recording.csv`: same results as live.
- [ ] Download that session's `.wpilog` from the robot and **Analyze a log file…** on it: same checks found, similar results (timestamps come from the robot).
- [ ] Copy the learned `pit_checks.yaml` into `examples/pit_checks.yaml` (or keep the team copy) and commit it.

Things that may need code or robot changes (write down what you hit):
- [ ] The 3 intake "Roller" motors share one topic, so only one shows up. Rename them in the robot code.
- [ ] No setpoints or step names on NT, so checks can only use fixed targets and `start_when` edges. If steps get mismatched, publishing the step name would fix it (see Robot code below).

### Cameras and Jetson
- [ ] Cameras tab: tiles for every connected camera, overlay streams play; hide the tab or minimize: streams stop. Note fps and quality.
- [ ] "via" SystemCore (SSH `direct-tcpip` forwarding) works, if the Jetson is only reachable through the SystemCore.
- [ ] Jetson tab health polling and snapshots save to `<save>/Jetson/snapshots/<stamp>/`.
- [ ] Heartbeat section with the `jetson-heartbeat` branch running on the Jetson (fake robot only so far).
- [ ] USB error count on the real Jetson: is it 0 after a clean boot?

### Close
- [ ] If you use it: put `DriverStation.exe` in **Competition lock while running**, start the DS: lock turns on; quit the DS: lock turns off and NT reconnects.
- [ ] Close the window: HOTWire quits, nothing keeps running on the robot (no SSH sessions, no streams).

## Robot code (Rebuilt_Systemcore), needed by HOTWire features
- [ ] Publish battery voltage: `RobotController.getBatteryVoltage()` to `/HOT/Robot/BatteryVoltage` (header badge shows "n/a" until then).
- [ ] Publish each motor's setpoint and the pit test step name to NT (better pit checks).
- [ ] Give the 3 intake "Roller" motors different names (they share one NT topic now).
- [ ] Publish **units** with the signals so the Signals tab can show them: set the topic's `"unit"` property (e.g. `topic.setProperty("unit", "\"amp\"")`, or use WPILib unit topics) in `Kraken.java`. HOTWire shows no unit until then.

## Notes
- Download speed: about 0.7 MB/s (6 Mbit/s) through the VH-109 practice access point (radio limit); much faster on a direct Ethernet connection (2026-11-19).
