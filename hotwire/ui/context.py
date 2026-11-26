"""AppContext: the shared state every tab uses (settings, NT, robot state, log)."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QObject, Signal

from hotwire.logsync import Index
from hotwire.nt4 import make_client
from hotwire.robotstate import Gate, RobotState, RobotStateReader, gate
from hotwire.settings import Settings, data_dir
from hotwire.systemserver import SystemReader, SystemStatus


class AppContext(QObject):
    log_line = Signal(str)
    tick = Signal(object)  # RobotState, once per second
    robot_connected = Signal()  # NT (re)connected: tether plugged in / robot booted
    lock_changed = Signal(bool)
    settings_changed = Signal()
    notify = Signal(str, str)  # title, message -> tray balloon

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.index = Index()
        self.nt, self.nt_backend_desc = make_client(settings.nt_backend, settings.nt_port)
        self.state_reader = RobotStateReader(self.nt)
        # The SystemCore's own server (port 6810): state and health even without robot code.
        self.system = SystemReader(settings.nt_backend, settings.system_port) if settings.system_server else None
        self.system_status = SystemStatus()
        self.state = RobotState()
        self._log_file = data_dir() / "hotwire.log"
        self._last_connect_time = 0.0
        self._batt_uid = None
        self.subscribe_battery()
        self.start_nt()
        self.log(f"NetworkTables client: {self.nt_backend_desc}")

    # ---------------------------------------------------------------- NT
    def nt_hosts(self) -> list[str]:
        dev = self.settings.device(self.settings.nt_device)
        return [h.host for h in dev.hosts if not h.via] if dev else []

    def start_nt(self) -> None:
        if self.locked:
            return
        self.nt.port = self.settings.nt_port
        self.nt.start(self.nt_hosts())
        if self.system is not None:
            self.system.start(self.nt_hosts())

    def stop_nt(self) -> None:
        self.nt.stop()
        if self.system is not None:
            self.system.stop()

    def restart_nt(self) -> None:
        self.stop_nt()
        self.start_nt()

    def subscribe_battery(self) -> None:
        if self._batt_uid is not None:
            self.nt.unsubscribe(self._batt_uid)
            self._batt_uid = None
        if self.settings.battery_topic.strip():
            self._batt_uid = self.nt.subscribe([self.settings.battery_topic], periodic=0.5)

    def battery_voltage(self) -> float | None:
        if self.locked:
            return None
        if self.system_status.battery is not None:  # the SystemCore's own reading: no robot code needed
            return self.system_status.battery
        if not self.settings.battery_topic.strip() or self._batt_uid is None:
            return None
        v = self.nt.get(self.settings.battery_topic)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    # ---------------------------------------------------------------- lock
    @property
    def locked(self) -> bool:
        return self.settings.competition_lock

    def set_lock(self, on: bool) -> None:
        if self.settings.competition_lock == on:
            return
        self.settings.competition_lock = on
        self.settings.save()
        self._apply_lock()

    def _apply_lock(self) -> None:
        if self.locked:
            self.stop_nt()  # no connections at all while locked
            self.log("Competition lock ON: all robot connections stopped")
        else:
            self.start_nt()
            self.log("Competition lock off")
        self.lock_changed.emit(self.locked)

    # ---------------------------------------------------------------- state
    def read_state(self) -> RobotState:
        """Robot state from robot code's DS topics, filled in from the SystemCore's own server.

        The SystemCore server can only ADD the field lock (FMS from either one counts), and it
        stands in when robot code isn't running (state.robot_code = False).
        """
        if self.locked:
            return RobotState()
        s = self.state_reader.read()
        sys_ctl = None
        if self.system is not None:
            self.system_status = self.system.read()
            sys_ctl = self.system_status.control if self.system_status.connected else None
        if sys_ctl is not None:
            if s.known:
                s.fms_attached = s.fms_attached or sys_ctl["fms_attached"]
            else:
                s.nt_connected = True
                s.known = True
                s.robot_code = bool(self.system_status.has_user_code)
                for k, v in sys_ctl.items():
                    setattr(s, k, v)
        if self.system is not None:
            self.system.set_field_mode(s.fms_attached)
        return s

    def update_state(self) -> RobotState:
        self.state = self.read_state()
        # On the field (FMS attached) only the tiny DS/FMS subscription stays: drop the battery
        # one too (Cameras, Jetson and Signals drop theirs on this same tick).
        if self.state.fms_attached and self._batt_uid is not None:
            self.nt.unsubscribe(self._batt_uid)
            self._batt_uid = None
        elif not self.state.fms_attached and self._batt_uid is None and not self.locked:
            self.subscribe_battery()
        if self.nt.connected and self.nt.connect_time != self._last_connect_time:
            self._last_connect_time = self.nt.connect_time
            self.log(f"Robot NetworkTables connected ({self.nt.connected_host})")
            self.robot_connected.emit()
        self.tick.emit(self.state)
        return self.state

    def gate(self, manual: bool, transfer: bool = True) -> Gate:
        return gate(self.read_state(), self.locked, manual, transfer)

    # ---------------------------------------------------------------- log
    def log(self, msg: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
        try:
            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(datetime.now().strftime("%Y-%m-%d ") + line + "\n")
        except OSError:
            pass
        self.log_line.emit(line)
