"""The SystemCore's own NetworkTables server (port 6810): robot state and health, no robot code needed.

The SystemCore OS runs a second NT4 server next to robot code's (5810). It's there whether
robot code runs or not, so HOTWire can see the robot's state, battery, disk and health while
robot code is stopped, crashed or being deployed. Listed on the robot 2026-11-25
(tools/nt_topics.py --port 6810). HOTWire only subscribes, never publishes (rule 6).

Topics used:
  /Netcomm/Control/ControlData   proto:mrc.proto.ProtobufControlData. Field 5 = ControlWord
                                 (uint32): bit 0 enabled, bits 1-2 mode (1 auto, 2 teleop,
                                 3 utility), bit 3 e-stop, bit 4 FMS attached, bit 5 DS attached.
                                 Bits 0-3 and 5 checked against the Driver Station's logs of the
                                 same sessions (2026-11-04 to 11-25); bit 4 (FMS) follows the same
                                 order as robot code's ControlWord struct but has NOT been seen
                                 with a real FMS yet. So FMS from here only ever ADDS the field lock.
  /sys/...                       battery (V), storage/storageavail/storagetotal (bytes), cpu (%),
                                 ramutil (%), temp (C), brownout, faults/canbus_down, emmc/status,
                                 osver
  /Netcomm/Status/HasUserCode    robot code is running

Field rule: with the FMS attached, only the ControlData subscription stays (like robot code's
DS/FMS one); the /sys/ and /Netcomm/Status/ ones are dropped until the FMS is gone.
"""

from __future__ import annotations

from dataclasses import dataclass

from hotwire.nt4 import make_client

SYSTEM_PORT = 6810
CONTROL_TOPIC = "/Netcomm/Control/ControlData"
HEALTH_PREFIXES = ["/sys/", "/Netcomm/Status/"]
MODES = {0: "unknown", 1: "autonomous", 2: "teleoperated", 3: "utility"}


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def control_word_from_proto(raw) -> int | None:
    """Field 5 (ControlWord) of a ProtobufControlData message. Missing = 0 (proto3 default)."""
    if not isinstance(raw, (bytes, bytearray)):
        return None
    b = bytes(raw)
    i, word = 0, 0
    try:
        while i < len(b):
            key, i = _varint(b, i)
            field, wt = key >> 3, key & 7
            if wt == 0:
                v, i = _varint(b, i)
                if field == 5:
                    word = v
            elif wt == 1:
                i += 8
            elif wt == 2:
                ln, i = _varint(b, i)
                i += ln
            elif wt == 5:
                i += 4
            else:
                return None
        if i != len(b):
            return None
    except IndexError:
        return None
    return word


def decode_control(word: int) -> dict:
    mode = (word >> 1) & 0x3
    return {
        "enabled": bool(word & 1),
        "autonomous": mode == 1,
        "test": False,
        "estop": bool(word >> 3 & 1),
        "fms_attached": bool(word >> 4 & 1),
        "ds_attached": bool(word >> 5 & 1),
        "mode": MODES[mode],
    }


@dataclass
class SystemStatus:
    connected: bool = False
    control: dict | None = None  # decode_control() result, None until received
    battery: float | None = None
    storage_avail: int | None = None
    storage_total: int | None = None
    cpu: float | None = None
    ram_util: float | None = None
    temp: float | None = None
    brownout: bool = False
    canbus_down: bool = False
    emmc: str = ""
    os_version: str = ""
    has_user_code: bool | None = None

    def summary(self) -> str:
        """One line for the header, e.g. 'robot code ✓ · CPU 15 % · 38 °C · disk 4.6 GB free'."""
        if not self.connected:
            return ""
        bits = []
        if self.has_user_code is not None:
            bits.append("robot code ✓" if self.has_user_code else "NO robot code")
        if self.brownout:
            bits.append("BROWNOUT")
        if self.canbus_down:
            bits.append("CAN bus down")
        if self.cpu is not None:
            bits.append(f"CPU {self.cpu:.0f} %")
        if self.temp is not None:
            bits.append(f"{self.temp:.0f} °C")
        if self.storage_avail is not None:
            bits.append(f"disk {self.storage_avail / 1e9:.1f} GB free")
        return " · ".join(bits)

    def problem(self) -> bool:
        return self.brownout or self.canbus_down or self.has_user_code is False or (self.emmc not in ("", "good"))


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class SystemReader:
    """A second, read-only NT client on the SystemCore's own server."""

    def __init__(self, backend: str = "auto", port: int = SYSTEM_PORT, client=None):
        if client is None:
            client, _desc = make_client(backend, port)
        self.nt = client
        self.nt.port = port
        self._ctl_uid = self.nt.subscribe([CONTROL_TOPIC], periodic=0.25)
        self._health_uid: int | None = None
        self.set_field_mode(False)

    def start(self, hosts: list[str]) -> None:
        self.nt.start(hosts)

    def stop(self) -> None:
        self.nt.stop()

    @property
    def field_mode(self) -> bool:
        return self._health_uid is None

    def set_field_mode(self, on: bool) -> None:
        """On the field (FMS attached): keep only the ControlData subscription."""
        if on and self._health_uid is not None:
            self.nt.unsubscribe(self._health_uid)
            self._health_uid = None
        elif not on and self._health_uid is None:
            self._health_uid = self.nt.subscribe(HEALTH_PREFIXES, prefix=True, periodic=1.0)

    def read(self) -> SystemStatus:
        s = SystemStatus(connected=self.nt.connected)
        if not s.connected:
            return s
        g = self.nt.get
        word = control_word_from_proto(g(CONTROL_TOPIC))
        s.control = decode_control(word) if word is not None else None
        if self._health_uid is None:
            return s
        s.battery = _num(g("/sys/battery"))
        avail, total = _num(g("/sys/storageavail")), _num(g("/sys/storagetotal"))
        s.storage_avail = int(avail) if avail is not None else None
        s.storage_total = int(total) if total is not None else None
        s.cpu = _num(g("/sys/cpu"))
        s.ram_util = _num(g("/sys/ramutil"))
        s.temp = _num(g("/sys/temp"))
        s.brownout = bool(_num(g("/sys/brownout")))
        s.canbus_down = bool(_num(g("/sys/faults/canbus_down")))
        s.emmc = str(g("/sys/emmc/status") or "")
        s.os_version = str(g("/sys/osver") or "")
        code = g("/Netcomm/Status/HasUserCode")
        s.has_user_code = code if isinstance(code, bool) else None
        return s
