# HOTWire TODO

## UI
- [x] Logs tab: **Select: All / None / New practice logs** buttons.
- [x] Open **maximized (full size)** on launch (also when restored from the tray).
- [x] Jetson tab: "USB errors" ignores the `fusb301` boot message; counts disconnects, over-current, descriptor read errors and resets after the first 60 s. Hover the number for the last one.
- [x] Jetson tab: heartbeat section (alive, vin / lowest vin, throttled, reboots, USB disconnects, event list) from `/HOTVision/heartbeat` and `/HOTVision/events` on NT. Shows "not published" until the `jetson-heartbeat` branch is on the Jetson.
- [x] Logs tab: "Modified (robot clock)" header fits; tabs scroll instead of being cut off on short screens (the window needed 833 px before).
- [x] Closing the window **quits the app** instead of hiding it in the tray ("minimize to tray" setting removed; "Start minimized" now minimizes to the taskbar).

## Release
- [x] Bump `hotwire/__init__.py` `__version__`: now 0.3.0 (release v0.2.0 was built from `signals-tab` with 0.1.0 in it).
- [x] Merge `signals-tab` into `main` (with release v0.3.0, 2026-11-24).

## Signals tab
Purpose: a quick **live** check in the pits ("look at this value while we get ready"). Match log analysis stays in AdvantageScope / CTRE Tuner X, which already load everything, so no more log-viewing features here (opening a `.wpilog` / `.csv` still works as it is).
- [x] Phase 1: pop-out/pinned signal list, strips with shared time axis and L/R Y axes, wheel / drag / right-drag box zoom, 2 cursors with Δ, colors, saved layouts, live (+ basic .wpilog/.csv opening).
- Phase 2 dropped (2026-11-24): AdvantageScope 2027 already does these, so use it instead: discrete fields (booleans, strings, enums), robot mode shading ("Show Robot Mode": auto / teleop / utility / disabled), and unit-aware graphs with conversion. AdvantageScope Lite also runs in the SystemCore web UI, with nothing to install.

## Robot test day (first test on the real robot)
Everything below has only run against `tools/fake_robot.py` so far. Write down what you see (screenshots of the Activity log help); anything that fails becomes a fix item.

### Findings (2026-11-25)
- [x] Signals tab: with the signal list collapsed, the signal names are squished. Now: a narrow list hides the Type column first, the name column keeps room for the names, long names are cut in the middle ("…"), hover for the full name. **Check on the laptop** that this is the squish you saw.
- [x] Values tab: the columns can't be resized. Now they fit their contents until you drag an edge, then keep your width (double-click an edge to fit again).
- [ ] Today's `.hoot` was saved under yesterday's folder (`SystemCore/2026-11-24/2026-11-24_18-23-02/`). CTRE names the folder from the robot clock at code start, before the DS sets the time, so the SystemCore still had about the time it was last shut down. The `.wpilog` (named after the DS set the time) went to the right day. Date `.hoot` files by their download time or the matching `.wpilog` instead of the folder name, or at least flag folders whose date doesn't match the robot's file modified time.
- [x] Pit Test tab: show the robot's current pit step. Done: banner at the top of the tab (live), recorded with every run, `robot_step` in `report.json`, and checks can use `start_step: "2/6"`. Original note: 2027_Biocore publishes `/HOT/Pit/Step` (string, e.g. `2/6 Intake deploy and roll in (running)`, or `press d-pad right to start`). Show it big at the top of the Pit Test tab, live, and save it with the recording. Then let checks start on the step name (e.g. `start_when: step == "2/6 Intake deploy and roll in"`) instead of guessing from signal edges, which fixes the "steps get mismatched" problem below. Signals tab already shows it in the topic tree, but it's a string, so it can't be plotted.
- [x] **Pit test infra for the 2027 robot**: `examples/pit_checks_2027.yaml` (6 checks on `start_step`, loose limits, HOT/Pit/... readings). **Try it on the robot.** Original note: HOTWire just needs to run with Biocore's Pit Test opmode, record the signals live and grade from a checks file. Write a starter `pit_checks.yaml` for Biocore's 6 steps (`/HOT/Pit/Step`, intake pivot / roller, `Drive/TotalCurrent`) with loose placeholder limits, only to prove the path works end to end. Note: Biocore's motor readings are in the log file only (not NT), so live grading can only use `/HOT/...` values; grading from the `.wpilog` sees everything. Planned on the robot side: during the Pit Test only, Biocore publishes intake and drive readings under `/HOT/Pit/...` (see 2027_Biocore `TESTING-2026-11-25.md`), so live grading can use those.
- [x] **Pull the Driver Station logs too**: done (`hotwire/dslogs.py`): Auto mode and the **Copy Driver Station logs** button copy them into a `ds` folder next to the robot log of the same session (by name time, within 30 s), else the day's folder; originals stay; last 14 days (setting). Deleting old ones from the laptop: not done. Original note: copy `C:\Users\Public\Documents\FIRSTDriverStation\Logs\FIRST_DS_<UTC date>_<time>.wpilog` (local files, no SSH) into the same save folder as the robot logs from that session. A DS log starts at the same second as the robot log when the DS connects to new robot code (e.g. `FIRST_DS_20261125_234634` and `WPILIB_20261125_234634`), so match them by name / time. Keep the originals on the laptop (the DS manages them), or add a setting to delete copied ones older than N days. They hold the robot's console, errors and alerts, which the robot log doesn't have.
- [ ] **One folder per session**: today a session's files are spread out: `SystemCore/<date>/` has the `.wpilog` files and one `.hoot` folder per code start, the `.hoot` can land in the wrong date (see above), and DS logs aren't pulled. Group each robot code run (`.wpilog` + its `.hoot` + its DS log + pit test recording) in one folder, e.g. `SystemCore/<date>/<HH-MM-SS>/`, named from the `.wpilog` time. Match the `.hoot` by its start time relative to the `.wpilog` (robot clock may be off before DS sync: use the file modified times on the robot).
- [ ] **Jetson data next to the robot logs**: the Jetson can't be tied to one robot run, so don't put it in the session folders. Pull the Jetson's HOTVision logs and the health snapshots per day into `Jetson/<date>/` (manual and auto, like the robot), and add a "Jetson snapshot" action that saves one into the session folder when you want it next to a run (e.g. after a match or pit test). Check what HOTVision writes on the Jetson first (log files? journal only? `journalctl -u hotvision` export?). The Jetson device today has `auto: false` and the SystemCore's log_dir (`/home/systemcore/logs`), which is wrong for it.
- [x] Settings tab: red "Unsaved settings: Save / Undo changes" bar, the tab is marked "● unsaved", quitting asks Save / Discard / Cancel. Original note: changes only apply after **Save settings**, and that's easy to miss. "Auto mode pulls only FMS match logs" was ticked but never saved (`settings.json` still had `false`), so Auto kept pulling practice logs after a robot code restart. Either save each change right away, or show a red bar at the bottom ("Unsaved settings: Save / Undo") while anything differs from the saved file, and ask before leaving the tab or quitting with unsaved changes.

### Findings (2026-11-24)
Fixed (check them on the robot next time):
- [x] Signals tab: the signal list's name column can be widened (drag its edge; double-click the edge fits the longest name; the list scrolls sideways), and the popped-out list has a drag edge to make it wider. Hover a name for the full topic.
- [x] Activity log at the bottom: half the height (drag the splitter for more).
- [x] Cameras tab: each tile is about half the window wide and as tall as the camera's picture shape allows; one camera alone fills the width.
- [x] Auto log pull with **"Auto mode pulls only FMS match logs"** unchecked: it did pull practice logs later the same day, so it works (the first try was probably still waiting for its 60 s re-check or the robot state).
- [x] Logs tab: each file's row fills green as it downloads, stays green when saved, turns red if it failed.
- [x] Signals tab: the plotted signals keep recording while you're on another tab (small send-all subscription), so the plot has no gap. The topic tree still only updates while the tab is open. Everything stops on FMS, lock, NT down or quit.
- [x] Jetson tab: health in two columns, every temperature sensor on its own row (hottest first, colored).
- [x] Settings tab: two fields per row, checkboxes two per row, device table tall enough for every device.
- [x] Competition lock: **"Competition lock while running"** removed (the DS runs in practice and the pits too). The lock is the manual button; with the FMS attached the field gate already blocks SSH, downloads, deletes, streams and Signals live.
- [x] Pit Test live view: a check is graded 0.5 s after its window ends (robot NT data arrives in ~100 ms batches), so it no longer shows **fail** for a moment before passing.
- [x] With the FMS attached, HOTWire also drops its Cameras, Jetson heartbeat and battery NT subscriptions, so only the tiny DS/FMS one stays. They come back when the FMS is gone.

### Before you start
- [ ] Laptop on the robot network (tether or radio). Note which, because download speed differs a lot.
- [ ] Settings: SystemCore and Jetson hosts correct, SSH key installed on both (**Set up key (password once)…**), **Test connection** passes.
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

Pit test results (2026-11-24, two runs in `WPILIB_20261124_221123.wpilog`):
- [x] The old checks missed the shooter and load/fire steps: the old checks used Shooter **Left Top**, which never published anything but 0. `examples/pit_checks.yaml` now has one check per robot step (11 checks) using Left Bottom / Right Top, with limits from both runs. The live recording gave the same numbers as the robot log.
- [x] Learn made limits too tight (one run's 9 A roller peak became `peak_max: 11.2`, next run peaked at 47 A). Learn now suggests `peak_max` = 1.5 × peak (was 1.25).
- [x] Re-run the pit test with the new checks and confirm all 11 pass (passed).
- [x] Live view: the first check showed **fail** for a moment, then **pass**. Likely graded before the window's last NT samples arrived; live mode now waits 0.5 s past the window (see Findings). Confirm on the next run.
- [ ] For the 2027 robot: new topic names and steps, so write a new checks file (same layout: one check per routine step, start_when on a signal that only changes at that step).
- [ ] **Shooter Left Top** reads 0 the whole time (position, velocity, current). Is the motor there / on the CAN bus? `ShooterKraken.runVelocity` commands it.
- [ ] **Intake didn't fully extend in run 2's load/fire steps**: stopped at 0.17 (should be 0.065) and held ~15 A for 6 s. Something in the way (balls?). The new checks fail 4a/4b when this happens.
- [ ] The intake roller topic only has data from 2 of the 3 rollers' `update()` (`IntakeKraken.updateStats` skips `m_RollerKraken3`), on top of the shared name.

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
- [ ] Close the window: HOTWire quits, nothing keeps running on the robot (no SSH sessions, no streams).

## SystemCore system NT server, port 6810 (listed on the robot 2026-11-25)
`tools/nt_topics.py --port 6810` found 470 topics, served by the SystemCore OS (`limelightosr-2027.0.0-alpha17`) whether or not robot code runs. Read-only for HOTWire. Useful ones:
- [ ] **Robot state:** `/Netcomm/Control/ControlData` (`proto:mrc.proto.ProtobufControlData`) and `/Netcomm/Control/MatchInfo` (empty without FMS). The schema is on the server (`/.schema/proto:MrcComm.proto`). Decode it, then check on the robot which fields are enabled / mode / FMS attached (toggle enable while watching). If it works, use it for the field gate so it works without robot code.
- [ ] **Battery badge:** `/sys/battery` (volts). No robot code needed, so it replaces waiting for `/HOT/Robot/BatteryVoltage` (Biocore publishes `/HOT/Robot/BatteryVolts` anyway).
- [ ] **Robot disk:** `/sys/storage`, `storageavail`, `storagetotal`, `storageutil` (bytes, %). Show it on the Logs tab without SSH.
- [ ] **SystemCore health panel:** `/sys/cpu`, `ram`/`ramutil`, `temp`, `brownout`, `faults/data` (`struct:IOFaults`: brownout, io, rsl, usb, display, imu, ...), `faults/canbus_down`/`canbus_unavail` and `faultcounts/...`, `emmc/status` and lifetime, `osver`, `osslot`.
- [ ] **CAN:** `/diagnostics/canbusutil`, `canbusfps`, `canbuserr`, `canbushealth`, `canbusinfo`, and per-interface counters (`/diagnostics/can0`, `can1`, `can_s0`...). Work out the array layouts first.
- [ ] **Alerts:** `/Netcomm/Alerts/<group>/<level>/<name>/active` and `text` (WPILib, Phoenix 6, DataLogManager, Telemetry). `active` is a timestamp, 0 when not active. Show the active ones.
- [ ] **Console:** `/Netcomm/Console/ConsoleLine`, `ErrorInfo` and `ProgramCrashInfo` (protobuf): the latest error and loop overrun breakdown.
- [ ] **Other:** `/Netcomm/Status/HasUserCode`, `HasUserCodeReady` (is robot code running?), `/Netcomm/OpModeOptions` (opmode list), `/imu/data` (`struct:IMUData`).
- `/mc/...` and `/pd/...` were empty (no device), skip for now.

## Ideas from AdvantageScope 2027 (Chief Delphi post, 2026-11-24)
AdvantageScope now downloads SystemCore logs over SFTP and skips files already saved, but has no auto pull, verify-then-delete, field/enabled check or pit test grading. Those stay HOTWire's job.
- [ ] **Robot state without robot code**: the SystemCore OS runs its own NetworkTables 4 server ("Systemcore Diagnostics" in AdvantageScope) with robot state and device IO, no user code needed. From the AdvantageScope source (`NT4.ts`: `NT4_PORTS_SYSTEMCORE = [6810]`): it's plain NT4 on **port 6810** (robot code's server is 5810; the Driver Station's is 6767), so HOTWire's NT clients can read it as they are, read-only. The source doesn't list its topics (AdvantageScope shows whatever is announced). Next robot day: list the topics on `10.0.67.2:6810` with `.venv\Scripts\python tools\nt_topics.py --port 6810` and look for the control word (enabled / FMS attached / mode). If it's there, read the robot state from 6810 so the field gate works even when robot code isn't running (today: "state unknown", and manual actions ask "are you in the pits?"). Device IO could also feed health checks.
  - Where it's defined (checked 2026-11-24): the server is part of the SystemCore OS (Limelight firmware; `LimelightVision/systemcore-os-public` is only release notes and build examples, no source). WPILib's HAL connects to it as client "RobotProgram" (`allwpilib/hal/src/main/native/systemcore/SystemServer.cpp`, `ROBOT_SYSTEM_SERVER_NT_PORT 6810`), and some topics are defined in allwpilib, e.g. Expansion Hub `/rhsp/<usb>/connected` (`ExpansionHub.java`). DS data and battery voltage do **not** go over NT: the HAL gets them from **mrclib** (`org.wpilib.mrclib` maven headers, binary shipped in the OS image, source not public).
  - **Probably not usable for the field gate yet**: the SystemcoreTesting AdvantageScope notes list "Robot mode and joystick data is not available in Systemcore diagnostics mode" as a known issue, to be fixed in a future release. Check again after OS / WPILib updates.
- [ ] **More SystemCore addresses**: AdvantageScope now connects to `robot.local` by default, and its source has SystemCore's fixed addresses: USB `172.26.0.1` on Windows (`172.27.0.1` on Mac/Linux) and its own Wi-Fi access point `172.30.0.1` (`Preferences.ts: getRobotAddress`). Add `robot.local`, `172.26.0.1` and `172.30.0.1` after `10.0.67.2` in the default SystemCore addresses (they're tried in order), and check each one on the robot.
- AdvantageScope's log download (for reference): SFTP as `systemcore` with the default password `systemcore`, refreshes the file list every 5 s, never deletes. HOTWire uses a key instead of a password, verifies on the robot and can delete.
- [ ] **2027 robot code: WPILib Telemetry API**: makes it easy to publish units, setpoints and the pit test step name (the items below). With setpoints on NT, the 2027 pit checks can compare against real targets instead of fixed numbers.

## Robot code (Rebuilt_Systemcore), needed by HOTWire features
- [ ] Publish battery voltage: `RobotController.getBatteryVoltage()` to `/HOT/Robot/BatteryVoltage` (header badge shows "n/a" until then).
- [ ] Publish each motor's setpoint and the pit test step name to NT (better pit checks).
- [ ] Give the 3 intake "Roller" motors different names (they share one NT topic now).
- [ ] Publish **units** with the signals so the Signals tab can show them: set the topic's `"unit"` property (e.g. `topic.setProperty("unit", "\"amp\"")`, or use WPILib unit topics) in `Kraken.java`. HOTWire shows no unit until then.

## Notes
- Download speed: about 0.7 MB/s (6 Mbit/s) through the VH-109 practice access point (radio limit); much faster on a direct Ethernet connection (2026-11-19).
