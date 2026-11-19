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
echo "usb_errors=$(journalctl -k -b --no-pager 2>/dev/null | grep -ciE 'usb.*(disconnect|error|reset)')"
"""


def health(conn, dev: Device) -> dict:
    script = HEALTH_SCRIPT.replace("SERVICE", shlex.quote(dev.service))
    rc, out, _ = conn.run(script, timeout=15)
    return parse_health(out)


def parse_health(text: str) -> dict:
    kv = {}
    for line in text.splitlines():
        if "=" in line:
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
    h["usb_errors"] = kv.get("usb_errors", "?")
    return h


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
