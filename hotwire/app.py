"""Main window, tray icon and the 1-second state tick."""

from __future__ import annotations

import subprocess
import sys

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from hotwire import __version__
from hotwire.settings import Settings
from hotwire.ui.cameras_tab import CamerasTab
from hotwire.ui.common import STATUS_COLORS, badge_style, make_icon
from hotwire.ui.context import AppContext
from hotwire.ui.jetson_tab import JetsonTab
from hotwire.ui.logs_tab import LogsTab
from hotwire.ui.pit_tab import PitTab
from hotwire.ui.settings_tab import SettingsTab


def running_processes() -> set[str]:
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return set()
    return {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line}


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.quitting = False
        self.setWindowTitle(f"HOTWire {__version__}")
        self.setWindowIcon(make_icon())
        self.resize(1200, 820)

        # --- header: robot state + competition lock
        head = QHBoxLayout()
        self.badge = QLabel("…")
        head.addWidget(self.badge)
        self.match = QLabel("")
        head.addWidget(self.match)
        head.addStretch(1)
        self.lock_btn = QPushButton()
        self.lock_btn.setCheckable(True)
        self.lock_btn.setMinimumWidth(220)
        self.lock_btn.clicked.connect(lambda on: ctx.set_lock(on))
        head.addWidget(self.lock_btn)

        self.tabs = QTabWidget()
        self.logs_tab = LogsTab(ctx)
        self.pit_tab = PitTab(ctx)
        self.cams_tab = CamerasTab(ctx)
        self.jetson_tab = JetsonTab(ctx)
        self.settings_tab = SettingsTab(ctx)
        self.tabs.addTab(self.logs_tab, "Logs")
        self.tabs.addTab(self.pit_tab, "Pit Test")
        self.tabs.addTab(self.cams_tab, "Cameras")
        self.tabs.addTab(self.jetson_tab, "Jetson")
        self.tabs.addTab(self.settings_tab, "Settings")

        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumBlockCount(2000)
        ctx.log_line.connect(self.activity.appendPlainText)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.tabs)
        split.addWidget(self.activity)
        split.setSizes([650, 150])

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addLayout(head)
        lay.addWidget(split, 1)
        self.setCentralWidget(central)

        # --- tray
        self.tray = QSystemTrayIcon(make_icon(), self)
        menu = QMenu()
        a_show = QAction("Show HOTWire", self)
        a_show.triggered.connect(self.show_normal)
        self.a_lock = QAction("Competition lock", self)
        self.a_lock.setCheckable(True)
        self.a_lock.triggered.connect(lambda on: ctx.set_lock(on))
        a_quit = QAction("Quit", self)
        a_quit.triggered.connect(self.quit)
        menu.addAction(a_show)
        menu.addAction(self.a_lock)
        menu.addSeparator()
        menu.addAction(a_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.show_normal() if r == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.setToolTip("HOTWire")
        self.tray.show()
        ctx.notify.connect(lambda t, m: self.tray.showMessage(t, m, make_icon(), 5000))
        ctx.lock_changed.connect(self._show_lock)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self._proc_check = 0
        self._show_lock(ctx.locked)
        ctx.log(f"HOTWire {__version__} started. Save folder: {ctx.settings.save_folder}")

    # ------------------------------------------------------------ tick
    def _tick(self):
        s = self.ctx.settings
        if s.lock_when_processes:
            self._proc_check += 1
            if self._proc_check % 5 == 1:
                running = running_processes()
                hit = any(p.lower() in running for p in s.lock_when_processes)
                self.ctx.set_process_lock(hit)
        elif self.ctx.process_lock:
            self.ctx.set_process_lock(False)
        st = self.ctx.update_state()
        if self.ctx.locked:
            text, color = "COMPETITION LOCK: no robot connections", STATUS_COLORS["fail"]
        elif not st.nt_connected:
            text, color = st.label, STATUS_COLORS["pending"]
        elif not st.known:
            text, color = st.label, STATUS_COLORS["warn"]
        elif st.fms_attached:
            text, color = st.label, STATUS_COLORS["fail"]
        elif st.enabled:
            text, color = st.label, STATUS_COLORS["warn"]
        else:
            text, color = f"{st.label} (pits): OK", STATUS_COLORS["pass"]
        self.badge.setText(text)
        self.badge.setStyleSheet(badge_style(color))
        host = self.ctx.nt.connected_host
        bits = [b for b in (st.event, st.match) if b]
        self.match.setText(("  ".join(bits) + "   " if bits else "") + (f"NT: {host}" if host else ""))
        self.tray.setToolTip(f"HOTWire: {text}")

    def _show_lock(self, on: bool):
        self.lock_btn.setChecked(self.ctx.settings.competition_lock)
        self.a_lock.setChecked(self.ctx.settings.competition_lock)
        if on:
            self.lock_btn.setText("🔒 Competition lock ON")
            self.lock_btn.setStyleSheet(badge_style(STATUS_COLORS["fail"]))
        else:
            self.lock_btn.setText("Competition lock: off")
            self.lock_btn.setStyleSheet("")

    # ------------------------------------------------------------ window
    def changeEvent(self, e):
        if e.type() == QEvent.Type.WindowStateChange:
            self.cams_tab.window_minimized(self.isMinimized())
        super().changeEvent(e)

    def show_normal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if self.ctx.settings.minimize_to_tray and not self.quitting:
            e.ignore()
            self.cams_tab.stop_all()
            self.hide()
            self.tray.showMessage("HOTWire", "Still running in the tray. Right-click the icon to quit.", make_icon(), 3000)
            return
        e.accept()
        self.quit()

    def quit(self):
        self.quitting = True
        self._shutdown()
        QApplication.quit()

    def _shutdown(self):
        self.cams_tab.stop_all()  # close camera streams -> HOTVision stops encoding
        if self.pit_tab.recorder is not None:
            self.pit_tab.stop_recording()
        self.ctx.stop_nt()
        self.tray.hide()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("HOTWire")
    app.setQuitOnLastWindowClosed(False)
    settings = Settings.load()
    ctx = AppContext(settings)
    w = MainWindow(ctx)
    app.aboutToQuit.connect(w._shutdown)
    if settings.start_minimized:
        w.hide()
    else:
        w.show()
    sys.exit(app.exec())
