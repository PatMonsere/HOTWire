"""Robot state from NetworkTables and the field-safety gate.

The gate is the most important safety rule in HOTWire: no SSH, file listing,
downloads, Jetson polling or camera streams while the robot is on the field.
"""

from __future__ import annotations

from dataclasses import dataclass

from hotwire.nt4 import NT4Client

# WPILib publishes the DS control word here. 2027 (SystemCore) uses
# /DriverStation; older versions used /FMSInfo. We subscribe to both.
TABLES = ["/DriverStation", "/FMSInfo"]
FIELDS = ["FMSControlData", "EventName", "MatchNumber", "MatchType"]

MATCH_TYPES = {0: "", 1: "P", 2: "Q", 3: "E"}


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

    @property
    def label(self) -> str:
        if not self.nt_connected:
            return "Robot not connected"
        if not self.known:
            return "Robot state unknown"
        if self.fms_attached:
            return "On field (FMS) - locked"
        if self.enabled:
            return "Enabled"
        return "Disabled"


def decode_control_word(word: int) -> dict:
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
        word = None
        for t in TABLES:
            tv = self.nt.get_timestamped(f"{t}/FMSControlData")
            if tv is not None:
                word = tv[1]
                break
        if word is not None:
            s.known = True
            for k, v in decode_control_word(word).items():
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
