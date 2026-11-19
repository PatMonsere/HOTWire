# HOTWire

Pit laptop tool for FRC Team 67 (HOT). Plug the tether into the robot and HOTWire:

- **Logs**: finds the robot's `.wpilog` / `.hoot` files, downloads them, checks each copy against the robot's own SHA-256, and only then deletes them from the robot. Files are sorted into `<save folder>/<device>/<EVENT>/<MATCH>/`. Point the save folder at a Google Drive for Desktop folder and the whole team gets the logs.
- **Pit Test**: records robot signals from NetworkTables while you run the robot's pit test routine, then grades each step (current peaks, steady current, reaching and settling at speed or position). The checks live in an editable YAML file. **Learn limits from this run** suggests limits from a known-good robot.
- **Cameras**: live HOTVision streams from the Jetson, with checks for stalled, frozen, dark, washed-out or blurry pictures and low capture fps.
- **Jetson**: live health (temperatures, CPU/GPU, memory, disk, service restarts, USB errors) and one-click snapshots (HOTVision journal, kernel log, `camera_config.json`, system report).
- **Settings**: device IPs, a one-time password login that installs this laptop's SSH key, and a "via SystemCore" route to reach the Jetson when you're on the robot's Wi-Fi.

## Field safety

HOTWire never talks to the robot while it's on the field. It reads the robot's own Driver Station/FMS status from NetworkTables:

| Robot state | HOTWire |
|---|---|
| FMS attached | **Field lock**: no SSH, downloads, streams or Jetson polling |
| Enabled | No downloads or deletes (streams and health OK) |
| State unknown | Auto mode waits; manual actions ask "are you in the pits?" |
| **Competition lock** button | No robot connections at all, NetworkTables included |

Robot files are never deleted unless the downloaded copy matches the robot's own checksum, and the log file currently being written is never touched.

## Install (Windows)

1. Install **Python 3.13** from python.org (or `winget install Python.Python.3.13`).
2. Clone this repo, then run `powershell -ExecutionPolicy Bypass -File install.ps1`.
3. Start **HOTWire** from the Start menu or the desktop.

There's no `.exe` on purpose: Windows **Smart App Control** blocks unsigned programs and DLLs. For the same reason HOTWire has its own pure-Python NetworkTables client instead of using `pyntcore`.

## First-time setup

1. **Settings → Save logs to**: choose a folder, ideally inside the team's Google Drive (Drive for Desktop).
2. **Settings → Devices**: check the IPs (SystemCore `10.0.67.2`, Jetson `10.0.67.7`).
3. Select each device and click **Set up key (password once)…**. The password is used once to install this laptop's key and is never saved.
4. **Pit Test → Edit checks…** starts from `examples/pit_checks.yaml`. Run the pit test on a healthy robot, then **Learn limits from this run…**.

## Pit checks file

```yaml
signals:
  shooter_vel:  HOT/Shooter/Left Top/ShooterLeft TopVelocity
  shooter_amps: HOT/Shooter/Left Top/ShooterLeft TopSupply Current
checks:
  - name: Shooter Batter preset
    start_when: shooter_vel > 5          # when this step starts
    window: 3.0                          # seconds analyzed
    expect:
      shooter_vel:  { settle_to: 55, tolerance: 2, within: 1.5 }
      shooter_amps: { peak_max: 80, steady: [2, 30] }
```

Keys: `peak_max`, `peak_min`, `steady: [lo, hi]`, `final` + `tolerance`, `settle_to` + `tolerance` + `within`, `overshoot_max`, `min`, `max`. See the comments in [`examples/pit_checks.yaml`](examples/pit_checks.yaml).

**Analyze a log file…** runs the same checks on a downloaded `.wpilog` (DataLogManager records NetworkTables) or on a saved HOTWire recording.

## Development

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest            # unit, fake-robot and offscreen GUI tests
.\.venv\Scripts\python tools\fake_robot.py  # fake robot NT4 server on 127.0.0.1:5810
.\.venv\Scripts\python -m hotwire
```

To try the GUI without a robot: run `tools/fake_robot.py`, then in Settings set the SystemCore address to `127.0.0.1`. `--fms` and `--enabled` simulate a robot on the field or enabled.

See [AGENTS.md](AGENTS.md) for how the code is organized.
