"""Main window, tray icon and the 1-second state tick."""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
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
from hotwire.ui.signals_tab import SignalsTab


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
        self.battery = QLabel("Battery: n/a")
        self.battery.setToolTip("Robot battery voltage (from the NT topic set in Settings)")
        head.addWidget(self.battery)
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
        self.signals_tab = SignalsTab(ctx)
        self.cams_tab = CamerasTab(ctx)
        self.jetson_tab = JetsonTab(ctx)
        self.settings_tab = SettingsTab(ctx)
        # Each tab sits in a scroll area, so the window can be smaller than a tab
        # (laptop screens): the tab scrolls instead of its bottom being cut off.
        self._tab_pages: dict[QWidget, QScrollArea] = {}
        for tab, name in (
            (self.logs_tab, "Logs"),
            (self.pit_tab, "Pit Test"),
            (self.signals_tab, "Signals"),
            (self.cams_tab, "Cameras"),
            (self.jetson_tab, "Jetson"),
            (self.settings_tab, "Settings"),
        ):
            page = QScrollArea()
            page.setWidgetResizable(True)
            page.setFrameShape(QFrame.Shape.NoFrame)
            page.setWidget(tab)
            self._tab_pages[tab] = page
            self.tabs.addTab(page, name)
        self.settings_tab.dirty_changed.connect(self._show_settings_dirty)

        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumBlockCount(2000)
        ctx.log_line.connect(self.activity.appendPlainText)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.tabs)
        split.addWidget(self.activity)
        split.setSizes([725, 75])  # activity log: a few lines; drag the splitter for more
        split.setStretchFactor(0, 1)  # extra height (maximized window) goes to the tabs
        split.setStretchFactor(1, 0)

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
        a_quit.triggered.connect(lambda: self.quit())
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
        self._show_lock(ctx.locked)
        ctx.log(f"HOTWire {__version__} started. Save folder: {ctx.settings.save_folder}")

    # ------------------------------------------------------------ tick
    def _tick(self):
        s = self.ctx.settings
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
        v = self.ctx.battery_voltage()
        if v is None:
            self.battery.setText("🔋 Battery: n/a")
            self.battery.setStyleSheet(badge_style(STATUS_COLORS["pending"]))
        else:
            bc = "fail" if v < s.battery_low_v else "warn" if v < s.battery_warn_v else "pass"
            self.battery.setText(f"🔋 {v:.2f} V")
            self.battery.setStyleSheet(badge_style(STATUS_COLORS[bc]))
        host = self.ctx.nt.connected_host
        bits = [b for b in (st.event, st.match) if b]
        nt = f"NT ({self.ctx.nt.backend}): {host}" if host else f"NT ({self.ctx.nt.backend})"
        self.match.setText(("  ".join(bits) + "   " if bits else "") + nt)
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

    def show_tab(self, tab: QWidget) -> None:
        self.tabs.setCurrentWidget(self._tab_pages[tab])

    # ------------------------------------------------------------ window
    def changeEvent(self, e):
        if e.type() == QEvent.Type.WindowStateChange:
            self.cams_tab.window_minimized(self.isMinimized())
        super().changeEvent(e)

    def show_normal(self):
        # un-minimize but keep maximized if it was
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def _show_settings_dirty(self, dirty: bool):
        i = self.tabs.indexOf(self._tab_pages[self.settings_tab])
        self.tabs.setTabText(i, "Settings ● unsaved" if dirty else "Settings")
        self.tabs.tabBar().setTabTextColor(i, Qt.GlobalColor.red if dirty else self.tabs.tabBar().palette().text().color())

    def _confirm_unsaved(self) -> bool:
        """Asks what to do with unsaved settings. False = don't quit."""
        if not self.settings_tab.is_dirty():
            return True
        self.show_normal()
        self.show_tab(self.settings_tab)
        b = QMessageBox.question(
            self,
            "Unsaved settings",
            "Some settings were changed but not saved. Save them before quitting?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if b == QMessageBox.StandardButton.Cancel:
            return False
        if b == QMessageBox.StandardButton.Save:
            self.settings_tab.save()
        return True

    def closeEvent(self, e):
        # Closing the window quits HOTWire (it used to hide in the tray, which
        # left it running unnoticed and blocked rebuilding the .exe).
        if not self.quitting and not self._confirm_unsaved():
            e.ignore()
            return
        e.accept()
        if not self.quitting:
            self.quit(ask=False)

    def quit(self, ask: bool = True):
        if ask and not self.quitting and not self._confirm_unsaved():
            return
        self.quitting = True
        self._shutdown()
        QApplication.quit()

    def _shutdown(self):
        self.cams_tab.stop_all()  # close camera streams -> HOTVision stops encoding
        self.signals_tab.shutdown()
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
    w.setWindowState(Qt.WindowState.WindowMaximized)
    if settings.start_minimized:
        # minimized to the taskbar (maximized again when restored), not hidden
        w.setWindowState(Qt.WindowState.WindowMaximized | Qt.WindowState.WindowMinimized)
    w.show()
    sys.exit(app.exec())
