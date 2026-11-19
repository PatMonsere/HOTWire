"""AppContext: the shared state every tab uses (settings, NT, robot state, log)."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QObject, Signal

from hotwire.logsync import Index
from hotwire.nt4 import NT4Client
from hotwire.robotstate import Gate, RobotState, RobotStateReader, gate
from hotwire.settings import Settings, data_dir


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
        self.nt = NT4Client(port=settings.nt_port)
        self.state_reader = RobotStateReader(self.nt)
        self.state = RobotState()
        self.process_lock = False  # set by MainWindow when a lock_when_processes program runs
        self._log_file = data_dir() / "hotwire.log"
        self._last_connect_time = 0.0
        self.start_nt()

    # ---------------------------------------------------------------- NT
    def nt_hosts(self) -> list[str]:
        dev = self.settings.device(self.settings.nt_device)
        return [h.host for h in dev.hosts if not h.via] if dev else []

    def start_nt(self) -> None:
        if self.locked:
            return
        self.nt.port = self.settings.nt_port
        self.nt.start(self.nt_hosts())

    def stop_nt(self) -> None:
        self.nt.stop()

    def restart_nt(self) -> None:
        self.stop_nt()
        self.start_nt()

    # ---------------------------------------------------------------- lock
    @property
    def locked(self) -> bool:
        return self.settings.competition_lock or self.process_lock

    def set_lock(self, on: bool) -> None:
        if self.settings.competition_lock == on:
            return
        self.settings.competition_lock = on
        self.settings.save()
        self._apply_lock()

    def set_process_lock(self, on: bool) -> None:
        if self.process_lock != on:
            self.process_lock = on
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
    def update_state(self) -> RobotState:
        self.state = self.state_reader.read() if not self.locked else RobotState()
        if self.nt.connected and self.nt.connect_time != self._last_connect_time:
            self._last_connect_time = self.nt.connect_time
            self.log(f"Robot NetworkTables connected ({self.nt.connected_host})")
            self.robot_connected.emit()
        self.tick.emit(self.state)
        return self.state

    def gate(self, manual: bool, transfer: bool = True) -> Gate:
        return gate(self.state_reader.read() if not self.locked else RobotState(), self.locked, manual, transfer)

    # ---------------------------------------------------------------- log
    def log(self, msg: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
        try:
            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(datetime.now().strftime("%Y-%m-%d ") + line + "\n")
        except OSError:
            pass
        self.log_line.emit(line)
