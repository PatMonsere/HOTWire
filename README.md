# HOTWire

Pit laptop tool for FRC Team 67 (HOT). Plug the tether into the robot and HOTWire:

- **Logs**: finds the robot's `.wpilog` / `.hoot` files, downloads them, checks each copy against the robot's own SHA-256, and only then deletes them from the robot. Files are sorted into `<save folder>/<device>/<EVENT>/<MATCH>/`. Point the save folder at a Google Drive for Desktop folder and the whole team gets the logs. **Select: All / None / New practice logs** ticks files for a manual download. It also copies this laptop's **Driver Station logs** (`FIRST_DS_*.wpilog`: the robot's console output, errors and alerts, which the robot log doesn't have) into a `ds` folder next to the robot log of the same session (Auto mode, or **Copy Driver Station logs**). The originals stay where the Driver Station keeps them.
- **Pit Test**: records robot signals from NetworkTables while you run the robot's pit test routine, then grades each step (current peaks, steady current, reaching and settling at speed or position). The checks live in an editable YAML file. **Learn limits from this run** suggests limits from a known-good robot.
- **Signals**: a signal viewer in the style of CANape / INCA MDA, for live robot NetworkTables or a `.wpilog` / recording file. See [Signals tab](#signals-tab).
- **Cameras**: live HOTVision streams from the Jetson, with checks for stalled, frozen, dark, washed-out or blurry pictures and low capture fps.
- **Jetson**: live health (temperatures, CPU/GPU, memory, disk, service restarts, USB errors after boot), HOTVision's heartbeat from NetworkTables (alive, input voltage and the lowest dip seen, throttling, reboots, camera USB disconnects, and a list of events) and one-click snapshots (HOTVision journal, kernel log, `camera_config.json`, system report).
- **SystemCore status** (header): the SystemCore's own NetworkTables server (port 6810) runs even when robot code doesn't, so HOTWire shows the robot state (with "no robot code"), battery voltage, free robot disk, CPU and temperature, and warns on a brownout, CAN bus down or no robot code. Auto log pull works without robot code too. Turn it off in **Settings** if needed.
- **Settings**: device IPs, a one-time password login that installs this laptop's SSH key, and a "via SystemCore" route to reach the Jetson when you're on the robot's Wi-Fi.

## Field safety

HOTWire never talks to the robot while it's on the field. It reads the robot's own Driver Station/FMS status from NetworkTables:

| Robot state | HOTWire |
|---|---|
| FMS attached | **Field lock**: no SSH, downloads, streams, Jetson polling or live Signals plotting. NetworkTables stays connected but only reads the robot state |
| Enabled | No downloads or deletes (streams and health OK) |
| State unknown | Auto mode waits; manual actions ask "are you in the pits?" |
| **Competition lock** button | No robot connections at all, NetworkTables included |

The robot state comes from robot code's Driver Station topics and from the SystemCore's own server. The SystemCore server fills in when robot code isn't running, but it can only **add** the field lock: if either one says the FMS is attached, HOTWire treats the robot as on the field.

Robot files are never deleted unless the downloaded copy matches the robot's own checksum, and the log file currently being written is never touched.

**Closing HOTWire leaves nothing running on the robot.** Closing the window quits the app. Camera streams stop (HOTVision stops encoding them), and the NetworkTables connection and all its subscriptions go away. HOTWire never publishes to NetworkTables or changes robot settings, and SSH is only used for short tasks, so this holds even if the app crashes. If you close it during a log download, the robot's file is kept and only a partial `.part` file is left on the laptop.

## Install (Windows)

**Easiest:** download `HOTWire.exe` from [Releases](https://github.com/PatMonsere/HOTWire/releases) and run it. You don't need to install anything. Windows may show "Windows protected your PC" because the exe isn't signed: click **More info → Run anyway**. If the laptop has **Smart App Control** on, Windows blocks the exe completely. Use the Python install below instead.

**From source (works even with Smart App Control on):**

1. Install **Python 3.13** from python.org (or `winget install Python.Python.3.13`).
2. Clone this repo, then double-click **`install.bat`**. Or run `powershell -ExecutionPolicy Bypass -File install.ps1`: running `install.ps1` directly fails if Windows blocks scripts, which it does by default.
3. Start **HOTWire** from the Start menu or the desktop.

HOTWire uses the official WPILib NetworkTables library (`pyntcore`). If Windows **Smart App Control** is on, it blocks pyntcore's DLLs, so HOTWire switches automatically to its own pure-Python NetworkTables client. **Settings → NetworkTables client** shows which one is in use and lets you force either one. For the same reason the unsigned `.exe` won't run on those laptops.

On a programming laptop, we recommend turning Smart App Control off (Windows Security → App & browser control). It also blocks other robot tools.

## First-time setup

1. **Settings → Save logs to**: choose a folder, ideally inside the team's Google Drive (Drive for Desktop).
2. **Settings → Devices**: check the IPs (SystemCore `10.0.67.2`, Jetson `10.0.67.7`).
3. Select each device and click **Set up key (password once)…**. The password is used once to install this laptop's key and is never saved.
4. **Pit Test → Edit checks…** starts from `examples/pit_checks.yaml` (the 2026 robot). For the 2027 robot code, use `examples/pit_checks_2027.yaml` (**Settings → Pit checks file**, or copy it over `<save folder>\pit_checks.yaml`). Run the pit test on a healthy robot, then **Learn limits from this run…**.

## Signals tab

Three panes: the **signal list** on the left, **strips** (plots that share one time axis) in the middle, and the **values** table on the right.

- **Signal list**: hover over the **Signals** tab at the left edge and it slides out over the plot. It folds away when the mouse leaves. Click the tab (or 📌) to pin it open. The box at the top picks the source: **Live robot**, a recent file, or **Open log file…** (`.wpilog` or a HOTWire `recording.csv`). Search with words in any order (`shoot curr`). Drag signals onto a strip, onto the dashed bar for a new strip, or double-click to add them to the selected strip.
- **Values**: each signal's unit (only when the robot publishes one: the NT topic's `"unit"` property, which WPILib unit topics set and DataLogManager stores in the `.wpilog`. Otherwise it's left blank, never guessed), value at cursor C1 and C2, Δ (C2−C1), and min/max over the visible time. Click ■ to change a color, untick to hide, right-click for the right Y axis / move / remove, drag a row onto another strip.

| Mouse / key | Does |
|---|---|
| Wheel | Zoom time around the mouse |
| Ctrl + wheel | Zoom this strip's Y axes |
| Left drag | Pan time |
| Right drag | Box zoom |
| Click / Shift + click | Cursor C1 / C2. Ticking **2 cursors** puts both on screen (Δt, 1/Δt and value deltas); drag the lines to move them |
| ← / → | Step the last-placed cursor one sample (of the signal selected in Values) |
| Double-click, Home | Fit everything |
| Backspace | Undo zoom |
| Ctrl+B / Ctrl+F | Pin the signal list / search |
| Right-click a strip | Fit Y, add strip below, clear, remove |

**Live**: the view follows the last 10 s – 10 min (**Window**). Zooming, panning or placing a cursor pauses it; **Resume** jumps back to now. Only the plotted signals are subscribed at full rate. They keep recording while you look at another tab, so the plot has no gap when you come back (the full topic list only updates while the tab is open). Live plotting stops when the FMS is attached or the competition lock is on.

**Layouts**: strips, signals, colors and axes. **Save as…** stores a named layout (e.g. "Shooter tuning") to reuse with any live session or log. The last view comes back when HOTWire starts.

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

**Robot pit step.** The 2027 robot code publishes the step its Pit Test is on (`HOT/Pit/Step`, e.g. `2/6 Intake deploy and roll in (running)`). The Pit Test tab shows it live at the top, records it with every run (`recording.csv`, and `robot_step` in `report.json`), and a check can start on it instead of a signal edge:

```yaml
step_signal: HOT/Pit/Step     # optional, this is the default
checks:
  - name: Intake deploy
    start_step: "2/6"          # starts when the step name begins with "2/6"
    window: 4.0
    expect: ...
```

The 2027 robot also sends a few readings to NetworkTables **only while its Pit Test runs** (`HOT/Pit/Intake/...`, `HOT/Pit/Drive/...`), so live grading uses those. See [`examples/pit_checks_2027.yaml`](examples/pit_checks_2027.yaml).

**Analyze a log file…** runs the same checks on a downloaded `.wpilog` (DataLogManager records NetworkTables) or on a saved HOTWire recording.

## Development

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest            # unit, fake-robot and offscreen GUI tests
.\.venv\Scripts\python tools\fake_robot.py  # fake robot NT4 server on 127.0.0.1:5810
.\.venv\Scripts\python -m hotwire
powershell -ExecutionPolicy Bypass -File build_exe.ps1   # -> dist\HOTWire.exe
```

To try the GUI without a robot: run `tools/fake_robot.py`, then in Settings set the SystemCore address to `127.0.0.1`. `--fms` and `--enabled` simulate a robot on the field or enabled.

See [AGENTS.md](AGENTS.md) for how the code is organized.
