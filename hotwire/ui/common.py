"""Shared UI helpers: colors, background jobs, the app icon."""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

STATUS_COLORS = {
    "pass": "#2e9d4f",
    "info": "#5b6b7a",
    "pending": "#8a8f98",
    "waiting": "#8a8f98",
    "running": "#2f74c0",
    "warn": "#d08b00",
    "fail": "#c73636",
    "missing": "#c73636",
}


def status_color(status: str) -> QColor:
    return QColor(STATUS_COLORS.get(status, "#5b6b7a"))


def badge_style(color: str) -> str:
    return f"background:{color}; color:white; border-radius:6px; padding:4px 10px; font-weight:600;"


# The HOT 67 logo. In the .exe (PyInstaller) bundled files live under sys._MEIPASS.
ICON_FILE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "assets" / "hotwire.ico"


def make_icon() -> QIcon:
    if ICON_FILE.exists():
        icon = QIcon(str(ICON_FILE))
        if not icon.isNull():
            return icon
    # Fallback if the logo file is missing: a red "HW" tile
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#c73636"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 12, 12)
    p.setPen(QColor("white"))
    f = QFont("Segoe UI", 22)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "HW")
    p.end()
    return QIcon(pm)


class Job(QObject):
    """Run a function in a background thread; deliver result/error on the UI thread."""

    done = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    _alive: set["Job"] = set()  # keep references until finished

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.cancel = threading.Event()

    def start(self, on_done=None, on_fail=None, on_progress=None) -> "Job":
        if on_done:
            self.done.connect(on_done)
        if on_fail:
            self.failed.connect(on_fail)
        if on_progress:
            self.progress.connect(on_progress)
        # Released on the UI thread, after the callbacks above ran. (Releasing it in the worker
        # thread freed the Job before Qt delivered done/failed, so callers that didn't keep a
        # reference, like fire-and-forget jobs, could lose their callback.)
        self.done.connect(self._release)
        self.failed.connect(self._release)
        Job._alive.add(self)
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def _release(self, *_):
        Job._alive.discard(self)

    def _run(self):
        try:
            res = self.fn(self, *self.args, **self.kwargs)
            self.done.emit(res)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if not str(e):
                msg += "\n" + traceback.format_exc(limit=3)
            self.failed.emit(msg)
