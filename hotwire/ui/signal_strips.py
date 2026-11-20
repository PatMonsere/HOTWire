"""Plot strips for the Signals tab (pyqtgraph).

Strips are stacked in a vertical splitter and share one time axis. Each
strip has a left and a right Y axis. Mouse:

    wheel            zoom time around the mouse
    Ctrl + wheel     zoom the Y axes of this strip
    left drag        pan time
    right drag       box zoom (time + this strip's Y)
    click            cursor C1        Shift + click   cursor C2
    double-click     fit everything   right-click     strip menu
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import numpy as np
import pyqtgraph as pg
import pyqtgraph.functions as fn
from pyqtgraph import Point
from PySide6.QtCore import QMimeData, QRectF, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel, QMenu, QSplitter, QVBoxLayout, QWidget

from hotwire.signals import Layout, StripCfg, TraceCfg

PLOT_BG = "#2b2b2b"  # dark grey plot area
PLOT_FG = "#c8c8c8"  # axes, ticks, text
pg.setConfigOptions(background=PLOT_BG, foreground=PLOT_FG, antialias=False)

MIME = "application/x-hotwire-signals"
AXIS_W = 62
CURSOR_COLORS = {1: "#ff5252", 2: "#40c4ff"}
CURSOR_PENS = {1: pg.mkPen(CURSOR_COLORS[1], width=1.5), 2: pg.mkPen(CURSOR_COLORS[2], width=1.5, style=Qt.PenStyle.DashLine)}


def make_mime(names: list[str], move_from: tuple[int, str] | None = None) -> QMimeData:
    m = QMimeData()
    m.setData(MIME, json.dumps({"names": names, "from": list(move_from) if move_from else None}).encode())
    m.setText("\n".join(names))
    return m


def read_mime(m: QMimeData) -> dict | None:
    if not m.hasFormat(MIME):
        return None
    try:
        return json.loads(bytes(m.data(MIME)).decode())
    except ValueError:
        return None


@dataclass
class Trace:
    cfg: TraceCfg
    item: pg.PlotDataItem


class _RightViewBox(pg.ViewBox):
    """Second Y axis. Lets every mouse event fall through to the main view box."""

    def wheelEvent(self, ev, axis=None):
        ev.ignore()

    def mouseDragEvent(self, ev, axis=None):
        ev.ignore()

    def mouseClickEvent(self, ev):
        ev.ignore()


class _StripViewBox(pg.ViewBox):
    def __init__(self):
        super().__init__()
        self.strip: Strip | None = None
        self.setMenuEnabled(False)

    def wheelEvent(self, ev, axis=None):
        s = 1.02 ** (ev.delta() * self.state["wheelScaleFactor"])
        center = Point(fn.invertQTransform(self.childGroup.transform()).map(ev.pos()))
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.scaleBy(y=s, center=center)
            r = self.strip.vb_right
            r.scaleBy(y=s, center=Point(r.mapSceneToView(ev.scenePos())))
        else:
            self.strip.area._before_user_zoom(debounce=True)
            self.scaleBy(x=s, center=center)
            self.strip.area._user_changed_range()
        ev.accept()

    def mouseDragEvent(self, ev, axis=None):
        ev.accept()
        if ev.button() == Qt.MouseButton.LeftButton:  # pan time
            dif = (ev.pos() - ev.lastPos()) * -1
            tr = fn.invertQTransform(self.childGroup.transform())
            d = tr.map(Point(dif.x(), 0)) - tr.map(Point(0, 0))
            self._resetTarget()
            self.translateBy(x=d.x())
            self.strip.area._user_changed_range()
        elif ev.button() == Qt.MouseButton.RightButton:  # box zoom
            if ev.isFinish():
                self.rbScaleBox.hide()
                p1, p2 = ev.buttonDownPos(ev.button()), ev.pos()
                if abs(p1.x() - p2.x()) < 4:
                    return
                self.strip.box_zoom(p1, p2)
            else:
                self.updateScaleBox(ev.buttonDownPos(), ev.pos())

    def mouseClickEvent(self, ev):
        ev.accept()
        x = self.mapSceneToView(ev.scenePos()).x()
        if ev.button() == Qt.MouseButton.LeftButton:
            self.strip.area._clicked(self.strip, x, ev.double(), bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier))
        elif ev.button() == Qt.MouseButton.RightButton:
            self.strip.area._strip_menu(self.strip, ev.screenPos().toPoint())


class Strip(pg.PlotWidget):
    def __init__(self, area: "StripsArea"):
        vb = _StripViewBox()
        super().__init__(viewBox=vb)
        vb.strip = self
        self.area = area
        self.vb = vb
        self.traces: list[Trace] = []
        pi = self.getPlotItem()
        pi.hideButtons()
        pi.showGrid(x=True, y=True, alpha=0.25)
        pi.showAxis("right")
        for side in ("left", "right"):
            ax = pi.getAxis(side)
            ax.setWidth(AXIS_W)
            ax.enableAutoSIPrefix(False)
        self.vb_right = _RightViewBox()
        self.vb_right.setZValue(-1)
        pi.scene().addItem(self.vb_right)
        pi.getAxis("right").linkToView(self.vb_right)
        self.vb_right.setXLink(vb)
        vb.sigResized.connect(self._sync_right)
        for v in (vb, self.vb_right):
            v.setAutoVisible(y=True)
            v.enableAutoRange(axis="y")
        self.legend = pg.LegendItem(offset=(4, 2), colCount=8, labelTextSize="8pt", labelTextColor=PLOT_FG,
                                    brush=pg.mkBrush(30, 30, 30, 210))  # fmt: skip
        self.legend.setParentItem(vb)
        self.cursor_lines: dict[int, pg.InfiniteLine] = {}
        for i in (1, 2):
            line = pg.InfiniteLine(angle=90, movable=True, pen=CURSOR_PENS[i], label=f"C{i}",
                                   labelOpts={"position": 0.96, "color": CURSOR_COLORS[i], "fill": (30, 30, 30, 220)})  # fmt: skip
            line.setZValue(50)
            line.hide()
            vb.addItem(line, ignoreBounds=True)
            line.sigPositionChanged.connect(lambda ln, i=i: self.area._cursor_dragged(i, ln.value()))
            self.cursor_lines[i] = line
        self.setAcceptDrops(True)
        self.setMinimumHeight(70)
        self.set_active(False)

    def _sync_right(self):
        self.vb_right.setGeometry(self.vb.sceneBoundingRect())
        self.vb_right.linkedViewChanged(self.vb, self.vb_right.XAxis)

    # ------------------------------------------------------------ traces
    def has(self, name: str) -> bool:
        return any(t.cfg.name == name for t in self.traces)

    def trace(self, name: str) -> Trace | None:
        return next((t for t in self.traces if t.cfg.name == name), None)

    def add_trace(self, cfg: TraceCfg) -> Trace:
        item = pg.PlotDataItem(stepMode="left", pen=pg.mkPen(cfg.color, width=1.5))
        item.setDownsampling(auto=True, method="peak")
        item.setClipToView(True)
        tr = Trace(cfg, item)
        self.traces.append(tr)
        (self.vb_right if cfg.axis == "R" else self.vb).addItem(item)
        item.setVisible(cfg.visible)
        self._rebuild_legend()
        return tr

    def remove_trace(self, name: str) -> TraceCfg | None:
        tr = self.trace(name)
        if tr is None:
            return None
        (self.vb_right if tr.cfg.axis == "R" else self.vb).removeItem(tr.item)
        self.traces.remove(tr)
        self._rebuild_legend()
        return tr.cfg

    def set_axis(self, name: str, axis: str) -> None:
        tr = self.trace(name)
        if tr is None or tr.cfg.axis == axis:
            return
        (self.vb_right if tr.cfg.axis == "R" else self.vb).removeItem(tr.item)
        tr.cfg.axis = axis
        (self.vb_right if axis == "R" else self.vb).addItem(tr.item)
        self._rebuild_legend()

    def set_color(self, name: str, color: str) -> None:
        tr = self.trace(name)
        if tr:
            tr.cfg.color = color
            tr.item.setPen(pg.mkPen(color, width=1.5))
            self._rebuild_legend()

    def set_visible(self, name: str, on: bool) -> None:
        tr = self.trace(name)
        if tr:
            tr.cfg.visible = on
            tr.item.setVisible(on)

    def _rebuild_legend(self):
        self.legend.clear()
        for t in self.traces:
            leaf = t.cfg.name.rstrip("/").rsplit("/", 1)[-1]
            self.legend.addItem(t.item, leaf + (" ▸R" if t.cfg.axis == "R" else ""))
        self.legend.setVisible(bool(self.traces))
        # numbers only on axes that have signals (the axes stay for alignment between strips)
        pi = self.getPlotItem()
        for side, ax in (("left", "L"), ("right", "R")):
            pi.getAxis(side).setStyle(showValues=any(t.cfg.axis == ax for t in self.traces))

    # ------------------------------------------------------------ view
    def box_zoom(self, p1, p2):
        """p1, p2: corners in view-box pixel coordinates."""
        a = self.area
        a._before_user_zoom()
        r = QRectF(Point(p1), Point(p2)).normalized()
        vr = self.vb.childGroup.mapRectFromParent(r).normalized()
        a.set_x_range(vr.left(), vr.right())
        if r.height() > 6:
            self.vb.setYRange(vr.top(), vr.bottom(), padding=0)
            s1 = self.vb_right.mapSceneToView(self.vb.mapToScene(Point(p1)))
            s2 = self.vb_right.mapSceneToView(self.vb.mapToScene(Point(p2)))
            self.vb_right.setYRange(min(s1.y(), s2.y()), max(s1.y(), s2.y()), padding=0)
        a._user_changed_range()

    def fit_y(self):
        self.vb.enableAutoRange(axis="y")
        self.vb_right.enableAutoRange(axis="y")

    def set_active(self, on: bool):
        self.setStyleSheet("border: 2px solid #3a6ea5;" if on else "border: 2px solid transparent;")

    # ------------------------------------------------------------ drops
    def dragEnterEvent(self, e):
        if read_mime(e.mimeData()) is not None:
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        self.dragEnterEvent(e)

    def dropEvent(self, e):
        d = read_mime(e.mimeData())
        if d is not None:
            e.acceptProposedAction()
            self.area._dropped(self, d)


class _DropZone(QLabel):
    def __init__(self, area: "StripsArea"):
        super().__init__("Drop signals here for a new strip  ·  double-click a signal to add it to the selected strip")
        self.area = area
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAcceptDrops(True)
        self.setMinimumHeight(26)
        self._style(False)

    def _style(self, hot: bool):
        self.setStyleSheet(
            "color:#666; border:1px dashed %s; background:%s;" % (("#3a6ea5", "#e6eef7") if hot else ("#bbb", "transparent"))
        )

    def dragEnterEvent(self, e):
        if read_mime(e.mimeData()) is not None:
            e.acceptProposedAction()
            self._style(True)

    def dragLeaveEvent(self, e):
        self._style(False)

    def dropEvent(self, e):
        self._style(False)
        d = read_mime(e.mimeData())
        if d is not None:
            e.acceptProposedAction()
            self.area._dropped(None, d)


class StripsArea(QWidget):
    """All strips, the shared time axis and the two cursors."""

    layout_changed = Signal()  # strips/traces/colors/axes changed (save + rebuild the values panel)
    cursors_changed = Signal()
    user_range_changed = Signal()  # the user zoomed or panned time
    data_needed = Signal(object)  # list of trace names that need data (just added)

    def __init__(self):
        super().__init__()
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.drop_zone = _DropZone(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(self.splitter, 1)
        lay.addWidget(self.drop_zone)
        self.strips: list[Strip] = []
        self.active: Strip | None = None
        self.cursor: dict[int, float | None] = {1: None, 2: None}
        self.selected_cursor = 1
        self._prev_c1: float | None = None
        self._history: list[tuple[float, float]] = []
        self._last_hist = 0.0
        self._moving_cursor = False

    # ------------------------------------------------------------ layout
    def to_layout(self) -> Layout:
        sizes = self.splitter.sizes()
        return Layout([StripCfg([TraceCfg(**vars(t.cfg)) for t in s.traces], sizes[i] if i < len(sizes) else 0)
                       for i, s in enumerate(self.strips)])  # fmt: skip

    def set_layout(self, layout: Layout) -> None:
        for s in list(self.strips):
            self._drop_strip(s)
        for sc in layout.strips:
            s = self.add_strip(emit=False)
            for tc in sc.traces:
                if not s.has(tc.name):
                    s.add_trace(TraceCfg(**vars(tc)))
        if not self.strips:
            self.add_strip(emit=False)
        hs = [sc.height for sc in layout.strips]
        if hs and all(h > 0 for h in hs):
            self.splitter.setSizes(hs)
        self._restyle()
        self.layout_changed.emit()
        self.data_needed.emit(layout.all_names())

    def add_strip(self, after: Strip | None = None, emit: bool = True) -> Strip:
        s = Strip(self)
        idx = self.strips.index(after) + 1 if after in self.strips else len(self.strips)
        self.strips.insert(idx, s)
        self.splitter.insertWidget(idx, s)
        if self.strips[0] is not s:
            s.setXLink(self.strips[0])
        else:
            for other in self.strips[1:]:
                other.setXLink(s)
        for i, line in s.cursor_lines.items():
            if self.cursor[i] is not None:
                line.blockSignals(True)
                line.setValue(self.cursor[i])
                line.blockSignals(False)
                line.show()
        if self.strips[0] is not s:
            s.setXRange(*self.x_range(), padding=0)
        self.set_active(s)
        self._restyle()
        if emit:
            self.layout_changed.emit()
        return s

    def remove_strip(self, s: Strip) -> None:
        self._drop_strip(s)
        if not self.strips:
            self.add_strip(emit=False)
        self._restyle()
        self.layout_changed.emit()

    def _drop_strip(self, s: Strip) -> None:
        was_first = self.strips and self.strips[0] is s
        x = self.x_range() if self.strips else None
        self.strips.remove(s)
        s.setParent(None)
        s.deleteLater()
        if was_first and self.strips:
            self.strips[0].setXLink(None)
            for other in self.strips[1:]:
                other.setXLink(self.strips[0])
            if x:
                self.strips[0].setXRange(*x, padding=0)
        if self.active is s:
            self.active = None
            if self.strips:
                self.set_active(self.strips[-1])

    def _restyle(self) -> None:
        # Every strip keeps its time axis because the axis draws the vertical grid lines;
        # only the bottom strip shows the numbers and the label.
        for i, s in enumerate(self.strips):
            last = i == len(self.strips) - 1
            pi = s.getPlotItem()
            ax = pi.getAxis("bottom")
            pi.showAxis("bottom", True)
            ax.setStyle(showValues=last)
            if last:
                ax.setHeight(None)
                pi.setLabel("bottom", "time (s)")
            else:
                pi.setLabel("bottom", None)
                ax.showLabel(False)
                ax.setHeight(4)

    def set_active(self, s: Strip) -> None:
        if self.active is not None and self.active is not s and self.active in self.strips:
            self.active.set_active(False)
        self.active = s
        s.set_active(len(self.strips) > 1)

    def add_signals(self, names: list[str], strip: Strip | None = None, colors: dict | None = None) -> None:
        """Add to a strip (None = the selected strip, or a new one)."""
        if strip is None:
            strip = self.active if self.active in self.strips else (self.strips[-1] if self.strips else self.add_strip())
        added = []
        lay = self.to_layout()
        for n in names:
            if strip.has(n):
                continue
            c = (colors or {}).get(n) or lay.next_color()
            strip.add_trace(TraceCfg(n, c))
            lay.strips.append(StripCfg([TraceCfg(n, c)]))  # so next_color sees it
            added.append(n)
        if added:
            self.layout_changed.emit()
            self.data_needed.emit(added)

    def remove_signal(self, strip: Strip, name: str) -> None:
        if strip.remove_trace(name):
            self.layout_changed.emit()

    def _dropped(self, target: Strip | None, d: dict) -> None:
        names = [n for n in d.get("names", []) if isinstance(n, str)]
        if not names:
            return
        src = d.get("from")
        colors = {}
        if src:  # moving a trace from another strip
            si, name = src
            if 0 <= si < len(self.strips):
                origin = self.strips[si]
                if origin is target:
                    return
                tr = origin.trace(name)
                if tr:
                    colors[name] = tr.cfg.color
                    origin.remove_trace(name)
        if target is None:
            target = self.add_strip(emit=False)
        self.set_active(target)
        self.add_signals(names, target, colors)
        self.layout_changed.emit()

    def all_traces(self) -> list[tuple[Strip, Trace]]:
        return [(s, t) for s in self.strips for t in s.traces]

    def names(self) -> list[str]:
        return sorted({t.cfg.name for _, t in self.all_traces()})

    # ------------------------------------------------------------ data
    def set_data(self, name: str, x: np.ndarray, y: np.ndarray) -> None:
        for _, t in self.all_traces():
            if t.cfg.name == name:
                t.item.setData(x, y)

    def clear_data(self) -> None:
        for _, t in self.all_traces():
            t.item.setData([], [])

    # ------------------------------------------------------------ time range / zoom
    def x_range(self) -> tuple[float, float]:
        if not self.strips:
            return (0.0, 10.0)
        (a, b), _ = self.strips[0].viewRange()
        return a, b

    def set_x_range(self, a: float, b: float) -> None:
        if self.strips and b > a:
            self.strips[0].setXRange(a, b, padding=0)

    def fit_all(self, x_bounds: tuple[float, float] | None) -> None:
        self._before_user_zoom()
        for s in self.strips:
            s.fit_y()
        if x_bounds and x_bounds[1] > x_bounds[0]:
            self.set_x_range(*x_bounds)

    def undo_zoom(self) -> bool:
        if not self._history:
            return False
        self.set_x_range(*self._history.pop())
        self._user_changed_range()
        return True

    def _before_user_zoom(self, debounce: bool = False) -> None:
        now = time.monotonic()
        if debounce and now - self._last_hist < 0.6:
            self._last_hist = now
            return
        self._last_hist = now
        self._history.append(self.x_range())
        del self._history[:-50]

    def _user_changed_range(self) -> None:
        self.user_range_changed.emit()

    # ------------------------------------------------------------ cursors
    def set_cursor(self, i: int, x: float | None) -> None:
        self.cursor[i] = x
        self._moving_cursor = True
        for s in self.strips:
            line = s.cursor_lines[i]
            if x is None:
                line.hide()
            else:
                line.blockSignals(True)
                line.setValue(x)
                line.blockSignals(False)
                line.show()
        self._moving_cursor = False
        if x is not None:
            self.selected_cursor = i
        self.cursors_changed.emit()

    def _cursor_dragged(self, i: int, x: float) -> None:
        if self._moving_cursor:
            return
        self.set_cursor(i, x)

    def _clicked(self, strip: Strip, x: float, double: bool, shift: bool) -> None:
        self.set_active(strip)
        if double:
            self.set_cursor(1, self._prev_c1)  # undo the first click's cursor move
            self.fit_requested()
            return
        if shift:
            self.set_cursor(2, x)
        else:
            self._prev_c1 = self.cursor[1]
            self.set_cursor(1, x)

    def fit_requested(self) -> None:
        """Overridden by the tab (it knows the data's time span)."""
        self.fit_all(None)

    def step_cursor(self, times: np.ndarray, direction: int) -> None:
        i = self.selected_cursor
        x = self.cursor[i]
        if x is None or len(times) == 0:
            return
        if direction > 0:
            j = int(np.searchsorted(times, x + 1e-9, side="right"))
        else:
            j = int(np.searchsorted(times, x - 1e-9, side="left")) - 1
        if 0 <= j < len(times):
            self.set_cursor(i, float(times[j]))
            a, b = self.x_range()
            if not a <= times[j] <= b:  # keep the cursor on screen
                w = b - a
                self.set_x_range(times[j] - w / 2, times[j] + w / 2)

    # ------------------------------------------------------------ menu
    def _strip_menu(self, strip: Strip, pos) -> None:
        self.set_active(strip)
        m = QMenu(self)
        m.addAction("Fit Y axes", strip.fit_y)
        m.addAction("Add strip below", lambda: self.add_strip(after=strip))
        if strip.traces:
            m.addAction("Remove all signals from this strip", lambda: self._clear_strip(strip))
        if len(self.strips) > 1:
            m.addAction("Remove strip", lambda: self.remove_strip(strip))
        m.addSeparator()
        m.addAction("Clear cursors", lambda: (self.set_cursor(1, None), self.set_cursor(2, None)))
        m.exec(pos)

    def _clear_strip(self, strip: Strip) -> None:
        for t in list(strip.traces):
            strip.remove_trace(t.cfg.name)
        self.layout_changed.emit()

    def strip_index(self, s: Strip) -> int:
        return self.strips.index(s)


def color_icon_pixmap(color: str, size: int = 12):
    from PySide6.QtGui import QPixmap

    pm = QPixmap(size, size)
    pm.fill(QColor(color))
    return pm
