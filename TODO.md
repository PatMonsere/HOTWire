# HOTWire TODO

## UI
- [x] Logs tab: **Select: All / None / New practice logs** buttons.
- [x] Open **maximized (full size)** on launch (also when restored from the tray).
- [x] Jetson tab: "USB errors" ignores the `fusb301` boot message; counts disconnects, over-current, descriptor read errors and resets after the first 60 s. Hover the number for the last one.
- [x] Jetson tab: heartbeat section (alive, vin / lowest vin, throttled, reboots, USB disconnects, event list) from `/HOTVision/heartbeat` and `/HOTVision/events` on NT. Shows "not published" until the `jetson-heartbeat` branch is on the Jetson.
- [x] Logs tab: "Modified (robot clock)" header fits; tabs scroll instead of being cut off on short screens (the window needed 833 px before).
- [x] Closing the window **quits the app** instead of hiding it in the tray ("minimize to tray" setting removed; "Start minimized" now minimizes to the taskbar).

## Signals tab
Purpose: a quick **live** check in the pits ("look at this value while we get ready"). Match log analysis stays in AdvantageScope / CTRE Tuner X, which already load everything, so no more log-viewing features here (opening a `.wpilog` / `.csv` still works as it is).
- [x] Phase 1: pop-out/pinned signal list, strips with shared time axis and L/R Y axes, wheel / drag / right-drag box zoom, 2 cursors with Δ, colors, saved layouts, live (+ basic .wpilog/.csv opening).
- [ ] Phase 2: a special **discrete strip** (like AdvantageScope) for booleans, strings and enums: one lane per signal, colored bars for true/false or each state, with the text shown in the bar (enabled, mechanism states, HOTVision events).
- [ ] Phase 2: robot mode shading (disabled / auto / teleop) behind the live plots.
- [ ] Phase 2: unit conversion per signal (e.g. rot/s → rpm), like AdvantageScope's "Convert Units…".
- [ ] Live plotting on the real robot: rate of send-all samples, CPU on the pit laptop.

## Verify on the robot (new)
- [ ] Heartbeat section with the `jetson-heartbeat` branch running on the Jetson (fake robot only so far).
- [ ] USB error count on the real Jetson: is it 0 after a clean boot?

## Robot code (Rebuilt_Systemcore), needed by HOTWire features
- [ ] Publish battery voltage: `RobotController.getBatteryVoltage()` to `/HOT/Robot/BatteryVoltage` (header badge shows "n/a" until then).
- [ ] Publish each motor's setpoint and the pit test step name to NT (better pit checks).
- [ ] Give the 3 intake "Roller" motors different names (they share one NT topic now).
- [ ] Publish **units** with the signals so the Signals tab can show them: set the topic's `"unit"` property (e.g. `topic.setProperty("unit", "\"amp\"")`, or use WPILib unit topics) in `Kraken.java`. HOTWire shows no unit until then.

## Verify on the robot
- [ ] Real FMS match log names (2027): expected `WPILIB_<date>_<time>_<EVENT>_<MATCH>.wpilog`; check hoot names too. Auto mode only pulls files that `logsync.is_match_log` recognizes.
- [ ] Pit test recording + Learn on the `pit_test_mode` robot code.

## Notes
- Download speed: about 0.7 MB/s (6 Mbit/s) through the VH-109 practice access point (radio limit); much faster on a direct Ethernet connection (2026-11-19).
