"""Robot state from NetworkTables and the field-safety gate.

The gate is the most important safety rule in HOTWire: no SSH, file listing,
downloads, Jetson polling or camera streams while the robot is on the field.
"""

from __future__ import annotations

from dataclasses import dataclass

from hotwire.nt4 import NT4Client

# Where the robot publishes its Driver Station state:
# * 2027 / SystemCore: /DriverStation/ControlWord, a struct (8 raw bytes). Schema read
#   from the robot on 2026-11-19 (/.schema/struct:ControlWord):
#     uint64 opModeHash:56; enum{unknown=0,autonomous=1,teleoperated=2,utility=3}
#     uint64 robotMode:2; bool enabled:1; bool eStop:1; bool fmsAttached:1; bool dsAttached:1;
#   Bitfields pack from the least significant bit of the little-endian uint64.
# * Older WPILib: /FMSInfo/FMSControlData, an int (bit 0 enabled ... bit 5 dsAttached).
TABLES = ["/DriverStation", "/FMSInfo"]
FIELDS = ["ControlWord", "FMSControlData", "EventName", "MatchNumber", "MatchType"]

MATCH_TYPES = {0: "", 1: "P", 2: "Q", 3: "E"}
ROBOT_MODES = {0: "unknown", 1: "autonomous", 2: "teleoperated", 3: "utility"}


@dataclass
class RobotState:
    nt_connected: bool = False
    known: bool = False  # control word received on this connection
    enabled: bool = False
    autonomous: bool = False
    test: bool = False
    estop: bool = False
    fms_attached: bool = False
    ds_attached: bool = False
    event: str = ""
    match: str = ""  # e.g. "Q12"
    mode: str = ""  # 2027: unknown / autonomous / teleoperated / utility
    robot_code: bool = True  # False: state from the SystemCore's own server, robot code not running

    @property
    def label(self) -> str:
        if not self.nt_connected:
            return "Robot not connected"
        if not self.known:
            return "Robot state unknown"
        if self.fms_attached:
            return "On field (FMS) - locked"
        m = f" ({self.mode})" if self.mode and self.mode != "unknown" else ""
        if not self.robot_code:
            m += " · no robot code"
        if self.enabled:
            return "Enabled" + m
        return "Disabled" + m


def decode_control_word_struct(raw: bytes) -> dict | None:
    """2027 struct:ControlWord (8 bytes)."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 8:
        return None
    w = int.from_bytes(bytes(raw[:8]), "little")
    mode = (w >> 56) & 0x3
    return {
        "enabled": bool(w >> 58 & 1),
        "autonomous": mode == 1,
        "test": False,
        "estop": bool(w >> 59 & 1),
        "fms_attached": bool(w >> 60 & 1),
        "ds_attached": bool(w >> 61 & 1),
        "mode": ROBOT_MODES[mode],
    }


def decode_control_word(word: int) -> dict:
    """Older WPILib FMSControlData integer."""
    word = int(word)
    return {
        "enabled": bool(word & 1),
        "autonomous": bool(word >> 1 & 1),
        "test": bool(word >> 2 & 1),
        "estop": bool(word >> 3 & 1),
        "fms_attached": bool(word >> 4 & 1),
        "ds_attached": bool(word >> 5 & 1),
    }


class RobotStateReader:
    """Keeps a tiny subscription to the DS/FMS topics on an NT4Client."""

    def __init__(self, nt: NT4Client):
        self.nt = nt
        self._uid = nt.subscribe([f"{t}/{f}" for t in TABLES for f in FIELDS], periodic=0.25)

    def read(self) -> RobotState:
        s = RobotState(nt_connected=self.nt.connected)
        if not s.nt_connected:
            return s
        decoded = decode_control_word_struct(self.nt.get("/DriverStation/ControlWord"))
        if decoded is None:
            for t in TABLES:
                word = self.nt.get(f"{t}/FMSControlData")
                if isinstance(word, int) and not isinstance(word, bool):
                    decoded = decode_control_word(word)
                    break
        if decoded is not None:
            s.known = True
            for k, v in decoded.items():
                setattr(s, k, v)
        for t in TABLES:
            ev = self.nt.get(f"{t}/EventName")
            if ev:
                s.event = str(ev)
            num = self.nt.get(f"{t}/MatchNumber")
            mtype = self.nt.get(f"{t}/MatchType")
            if num:
                s.match = f"{MATCH_TYPES.get(int(mtype or 0), '')}{int(num)}"
        return s


@dataclass
class Gate:
    allowed: bool
    reason: str
    needs_confirm: bool = False  # manual actions may proceed after the user confirms


def gate(state: RobotState, competition_lock: bool, manual: bool, transfer: bool = True) -> Gate:
    """May HOTWire talk to the robot network right now?

    transfer=True for downloads/deletes (blocked while enabled too).
    transfer=False for read-only things like streams and Jetson health.
    """
    if competition_lock:
        return Gate(False, "Competition lock is on")
    if state.nt_connected and state.known and state.fms_attached:
        return Gate(False, "Robot is on the field (FMS attached)")
    if transfer and state.known and state.enabled:
        return Gate(False, "Robot is enabled")
    if not state.known:
        if manual:
            return Gate(True, "Robot state unknown", needs_confirm=True)
        return Gate(False, "Robot state unknown (auto mode waits)")
    return Gate(True, "OK")
