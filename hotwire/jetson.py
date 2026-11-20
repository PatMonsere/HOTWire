"""Jetson (HOTVision) snapshot and live health.

HOTVision writes no log files of its own: its output goes to the systemd
journal via hotvision.service. A "snapshot" therefore collects the journal,
kernel messages, the camera config and a system report into text files.
Nothing is deleted on the Jetson (journald rotates itself).
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hotwire.settings import Device


def snapshot_commands(dev: Device, since: str | None) -> dict[str, str]:
    svc = shlex.quote(dev.service)
    since_arg = f"--since {shlex.quote(since)}" if since else "-b"
    cfg = shlex.quote(f"{dev.hotvision_dir}/.config/camera_config.json")
    return {
        "hotvision_journal.txt": f"journalctl -u {svc} {since_arg} --no-pager -o short-iso",
        "kernel.txt": f"journalctl -k {since_arg} --no-pager -o short-iso",
        "camera_config.json": f"cat {cfg}",
        "system_report.txt": (
            "echo '== date'; date; echo '== uptime'; uptime; "
            "echo '== service'; systemctl status " + svc + " --no-pager -l | head -40; "
            "echo '== restarts'; systemctl show " + svc + " -p NRestarts -p ActiveEnterTimestamp; "
            "echo '== disk'; df -h; echo '== memory'; free -m; "
            "echo '== thermal'; for z in /sys/class/thermal/thermal_zone*; do "
            "echo \"$(cat $z/type) $(cat $z/temp)\"; done; "
            "echo '== tegrastats'; timeout 2.5 tegrastats --interval 1000 2>/dev/null | head -2; "
            "echo '== cameras'; ls -l /dev/v4l/by-id 2>/dev/null; echo '== usb'; lsusb 2>/dev/null"
        ),
    }


def take_snapshot(conn, dev: Device, save_folder: str, since: str | None) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = Path(save_folder) / dev.name / "snapshots" / stamp
    folder.mkdir(parents=True, exist_ok=True)
    for fname, cmd in snapshot_commands(dev, since).items():
        rc, out, err = conn.run(cmd, timeout=60)
        text = out if rc == 0 else out + f"\n[command failed rc={rc}]\n{err}"
        (folder / fname).write_text(text, encoding="utf-8")
    return folder


# ------------------------------------------------------------ live health
HEALTH_SCRIPT = r"""
read_cpu() { awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat; }
A=$(read_cpu); sleep 0.5; B=$(read_cpu)
echo "cpu_a=$A"; echo "cpu_b=$B"
for z in /sys/class/thermal/thermal_zone*; do echo "temp_$(cat $z/type)=$(cat $z/temp)"; done
for g in /sys/devices/platform/bus@0/17000000.gpu/load /sys/devices/gpu.0/load; do
  [ -r $g ] && { echo "gpu_load=$(cat $g)"; break; }; done
awk '/MemTotal/{print "mem_total="$2} /MemAvailable/{print "mem_avail="$2}' /proc/meminfo
df -k / | tail -1 | awk '{print "disk_used="$3; print "disk_total="$2}'
echo "uptime=$(cut -d' ' -f1 /proc/uptime)"
echo "svc_active=$(systemctl is-active SERVICE)"
echo "svc_restarts=$(systemctl show SERVICE -p NRestarts --value)"
echo "usb_ok=1"
journalctl -k -b --no-pager -o short-monotonic 2>/dev/null | grep -iE 'usb|over-?current' | tail -n 400 | sed 's/^/usbk=/'
"""

# Kernel messages that look like USB errors but are harmless.
USB_IGNORE = re.compile(r"fusb301", re.I)  # dev kit USB-C controller probe fails on every boot (error -22)
USB_PROBLEM = re.compile(
    r"\bUSB disconnect|over-?current|descriptor read|device not accepting address|"
    r"unable to enumerate|cannot enable|error -\d+|reset .*USB device",
    re.I,
)
# Devices are reset while they're first set up at boot; count resets only after this.
USB_BOOT_SETTLE_S = 60.0


def count_usb_errors(lines: list[str]) -> tuple[int, str]:
    """(number of real USB problems this boot, the last one) from `journalctl -k -o short-monotonic` lines."""
    n, last = 0, ""
    for line in lines:
        if USB_IGNORE.search(line) or not USB_PROBLEM.search(line):
            continue
        m = re.match(r"\s*\[\s*(\d+(?:\.\d+)?)\]", line)
        t = float(m.group(1)) if m else None
        if re.search(r"\breset\b", line, re.I) and not re.search(r"error|disconnect|over-?current", line, re.I):
            if t is None or t < USB_BOOT_SETTLE_S:
                continue
        n += 1
        last = line.strip()
    return n, last


def health(conn, dev: Device) -> dict:
    script = HEALTH_SCRIPT.replace("SERVICE", shlex.quote(dev.service))
    rc, out, _ = conn.run(script, timeout=15)
    return parse_health(out)


def parse_health(text: str) -> dict:
    kv = {}
    usb_lines = []
    for line in text.splitlines():
        if line.startswith("usbk="):
            usb_lines.append(line[5:])
        elif "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    h: dict = {"temps": {}}
    try:
        a_tot, a_idle = (float(x) for x in kv["cpu_a"].split())
        b_tot, b_idle = (float(x) for x in kv["cpu_b"].split())
        dt = b_tot - a_tot
        h["cpu_pct"] = round(100 * (1 - (b_idle - a_idle) / dt), 1) if dt > 0 else None
    except (KeyError, ValueError):
        h["cpu_pct"] = None
    for k, v in kv.items():
        if k.startswith("temp_") and re.fullmatch(r"-?\d+", v):
            h["temps"][k[5:]] = int(v) / 1000.0
    try:
        h["gpu_pct"] = round(int(kv["gpu_load"]) / 10.0, 1)  # file is in 0.1 %
    except (KeyError, ValueError):
        h["gpu_pct"] = None
    try:
        h["mem_pct"] = round(100 * (1 - int(kv["mem_avail"]) / int(kv["mem_total"])), 1)
    except (KeyError, ValueError, ZeroDivisionError):
        h["mem_pct"] = None
    try:
        h["disk_pct"] = round(100 * int(kv["disk_used"]) / int(kv["disk_total"]), 1)
    except (KeyError, ValueError, ZeroDivisionError):
        h["disk_pct"] = None
    h["uptime_s"] = float(kv["uptime"]) if kv.get("uptime", "").replace(".", "", 1).isdigit() else None
    h["service"] = kv.get("svc_active", "?")
    h["restarts"] = kv.get("svc_restarts", "?")
    if "usb_ok" in kv:
        n, last = count_usb_errors(usb_lines)
        h["usb_errors"], h["usb_last"] = str(n), last
    else:
        h["usb_errors"], h["usb_last"] = "?", ""
    return h


# ------------------------------------------------------------ heartbeat (NT)
HEARTBEAT_STALE_S = 1.5  # HOTVision publishes heartbeat/seq every 250 ms
VIN_DIP_WARN = 0.85  # warn when the input voltage dips below 85 % of the highest seen


@dataclass
class HeartbeatView:
    alive_text: str
    alive_status: str
    vin_text: str
    vin_status: str
    vin_min_text: str
    vin_min_status: str
    reboots: int
    restarts: int
    reboots_text: str


@dataclass
class HeartbeatWatch:
    """Turns HOTVision heartbeat values into display text; remembers dips and reboots between updates."""

    vin_high: float | None = None
    vin_low: float | None = None
    vin_low_at: str = ""
    last_jetson_uptime: float | None = None
    last_app_uptime: float | None = None
    reboots: int = 0
    restarts: int = 0

    def update(self, seq_ts_us, now_us, jetson_uptime, app_uptime, vin, vin_min, clock: str = "") -> HeartbeatView:
        age = (now_us - seq_ts_us) / 1e6 if seq_ts_us and now_us else None
        if age is None:
            alive = ("?", "info")
        elif age < HEARTBEAT_STALE_S:
            alive = ("alive", "pass")
        else:
            alive = (f"STOPPED: nothing for {age:.0f} s (HOTVision frozen, Jetson off, or network drop)", "fail")

        if _num(jetson_uptime):
            if self.last_jetson_uptime is not None and jetson_uptime < self.last_jetson_uptime - 5:
                self.reboots += 1
            self.last_jetson_uptime = jetson_uptime
        if _num(app_uptime):
            if self.last_app_uptime is not None and app_uptime < self.last_app_uptime - 5:
                self.restarts += 1
            self.last_app_uptime = app_uptime

        if _num(vin):
            self.vin_high = max(self.vin_high or vin, vin)
        low = vin_min if _num(vin_min) else vin if _num(vin) else None
        if low is not None and (self.vin_low is None or low < self.vin_low):
            self.vin_low = low
            self.vin_low_at = clock or datetime.now().strftime("%H:%M:%S")

        if not _num(vin):
            vin_t, vin_s = "not published (no INA3221 on this board?)", "info"
        else:
            vin_t, vin_s = f"{vin:.2f} V", "pass"
        if self.vin_low is None:
            low_t, low_s = "?", "info"
        else:
            dip = self.vin_high is not None and self.vin_low < VIN_DIP_WARN * self.vin_high
            low_t = f"{self.vin_low:.2f} V at {self.vin_low_at}" + (" (DIP)" if dip else "")
            low_s = "warn" if dip else "pass"
        rb = f"Jetson rebooted {self.reboots}x, HOTVision restarted {self.restarts}x"
        return HeartbeatView(*alive, vin_t, vin_s, low_t, low_s, self.reboots, self.restarts, rb)


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def read_camera_config(conn, dev: Device) -> dict:
    """{camera name: configured framerate} from the Jetson's camera_config.json."""
    rc, out, _ = conn.run(f"cat {shlex.quote(dev.hotvision_dir + '/.config/camera_config.json')}")
    if rc != 0:
        return {}
    try:
        cfg = json.loads(out)
    except ValueError:
        return {}
    cams = cfg.get("cameras", cfg) if isinstance(cfg, dict) else {}
    result = {}
    items = cams.items() if isinstance(cams, dict) else ((c.get("name"), c) for c in cams if isinstance(c, dict))
    for name, c in items:
        if isinstance(c, dict) and name:
            fr = c.get("framerate") or c.get("fps")
            if fr:
                result[str(name)] = float(fr)
    return result
