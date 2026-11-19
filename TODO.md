# HOTWire TODO

## UI
- [ ] Logs tab: **Select all / Select none** buttons (and maybe "select all practice logs").
- [ ] Open **maximized (full size)** on launch.
- [ ] Logs tab: the "Modified (robot clock)" column header gets cut off; the status line at the bottom can be hidden when the window is short.

## Robot code (Rebuilt_Systemcore), needed by HOTWire features
- [ ] Publish battery voltage: `RobotController.getBatteryVoltage()` to `/HOT/Robot/BatteryVoltage` (header badge shows "n/a" until then).
- [ ] Publish each motor's setpoint and the pit test step name to NT (better pit checks).
- [ ] Give the 3 intake "Roller" motors different names (they share one NT topic now).

## Verify on the robot
- [ ] Real FMS match log names (2027): expected `WPILIB_<date>_<time>_<EVENT>_<MATCH>.wpilog`; check hoot names too. Auto mode only pulls files that `logsync.is_match_log` recognizes.
- [ ] Pit test recording + Learn on the `pit_test_mode` robot code.

## Notes
- Download speed: about 0.7 MB/s (6 Mbit/s) through the VH-109 practice access point (radio limit); much faster on a direct Ethernet connection (2026-11-19).
