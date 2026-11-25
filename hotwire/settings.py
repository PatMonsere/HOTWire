"""App settings: devices, folders and safety options, stored as JSON.

The settings file lives in the app data folder (see ``data_dir``). Every
field has a default, so a missing or partial file still loads.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


def data_dir() -> Path:
    """Folder for settings.json, known_hosts and the download index.

    Uses HOTWIRE_HOME if set (handy for tests), else %APPDATA%\\HOTWire.
    """
    env = os.environ.get("HOTWIRE_HOME")
    if env:
        p = Path(env)
    elif sys.platform == "win32":
        p = Path(os.environ.get("APPDATA", Path.home())) / "HOTWire"
    else:
        p = Path.home() / ".hotwire"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class HostEntry:
    """One way to reach a device: an IP/hostname, optionally through another device (SSH jump)."""

    host: str
    via: str = ""  # name of another device to tunnel through, "" = direct


@dataclass
class Device:
    name: str
    kind: str  # "systemcore" | "jetson" | "other"
    user: str
    hosts: list[HostEntry] = field(default_factory=list)
    auto: bool = False  # include in Auto mode
    # SystemCore / log devices
    log_dir: str = "/home/systemcore/logs"
    log_exts: list[str] = field(default_factory=lambda: [".wpilog", ".hoot"])
    # Jetson
    hotvision_dir: str = "/home/hotros/HOTVision"
    service: str = "hotvision.service"
    web_port: int = 5800

    @staticmethod
    def from_dict(d: dict) -> "Device":
        d = dict(d)
        d["hosts"] = [HostEntry(**h) for h in d.get("hosts", [])]
        known = Device.__dataclass_fields__.keys()
        return Device(**{k: v for k, v in d.items() if k in known})


def default_devices() -> list[Device]:
    return [
        Device(
            name="SystemCore",
            kind="systemcore",
            user="systemcore",
            hosts=[HostEntry("10.0.67.2")],
            auto=True,
        ),
        Device(
            name="Jetson",
            kind="jetson",
            user="hotros",
            hosts=[HostEntry("10.0.67.7"), HostEntry("10.0.67.7", via="SystemCore")],
            auto=False,
        ),
    ]


@dataclass
class Settings:
    save_folder: str = str(Path.home() / "HOTWire Logs")
    poll_seconds: int = 5
    # Manual lock: no robot connections at all. With the FMS attached, HOTWire
    # already blocks everything except its NT subscription (robotstate.gate).
    competition_lock: bool = False
    speed_limit_kbps: int = 0  # 0 = unlimited
    delete_after_verify: bool = True
    # Auto mode pulls only FMS match logs (event + match in the file name).
    # Practice logs stay on the robot until pulled in Manual mode.
    auto_match_logs_only: bool = True
    in_use_seconds: int = 60  # a robot file modified this recently is "in use"
    start_minimized: bool = False  # minimized to the taskbar; closing the window quits
    pit_config: str = ""  # "" = <save_folder>/pit_checks.yaml
    key_path: str = str(Path.home() / ".ssh" / "id_ed25519")
    nt_device: str = "SystemCore"  # NetworkTables server = this device's hosts
    nt_port: int = 5810
    # "auto" = pyntcore (official WPILib library) if it loads, else the built-in
    # pure-Python client; "pyntcore" or "builtin" to force one. Restart to apply.
    nt_backend: str = "auto"
    # Battery voltage shown in the header. The robot must publish it, e.g.
    # RobotController.getBatteryVoltage() to /HOT/Robot/BatteryVoltage.
    battery_topic: str = "/HOT/Robot/BatteryVoltage"
    battery_warn_v: float = 12.3  # yellow below this (resting voltage)
    battery_low_v: float = 12.0  # red below this: swap the battery
    jetson_snapshot_on_connect: bool = False
    stream_sharpness_min: float = 0.0  # 0 = don't judge sharpness
    devices: list[Device] = field(default_factory=default_devices)

    # ---------- helpers ----------
    def device(self, name: str) -> Device | None:
        return next((d for d in self.devices if d.name == name), None)

    def devices_of(self, kind: str) -> list[Device]:
        return [d for d in self.devices if d.kind == kind]

    def pit_config_path(self) -> Path:
        if self.pit_config:
            return Path(self.pit_config)
        return Path(self.save_folder) / "pit_checks.yaml"

    # ---------- load / save ----------
    @staticmethod
    def path() -> Path:
        return data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        p = cls.path()
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        s = cls()
        for k, v in raw.items():
            if k == "devices":
                s.devices = [Device.from_dict(d) for d in v]
            elif k in cls.__dataclass_fields__:
                setattr(s, k, v)
        return s

    def save(self) -> None:
        p = self.path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(p)
