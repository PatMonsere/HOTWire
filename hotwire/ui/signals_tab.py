"""Signals tab: a CANape / MDA style signal viewer.

    left   signal list (pops out on hover, or pinned): source, search, topic tree
    middle strips with a shared time axis (hotwire/ui/signal_strips.py)
    right  values table: cursor values, delta, min/max in view, colors, axes

Sources: live robot NetworkTables (read-only subscriptions: the "/" topic tree
only while this tab is visible, the plotted signals also while it's hidden so
the plot has no hole; all stopped when the FMS is attached, the competition
lock is on or NT drops), or a log file (.wpilog, HOTWire recording .csv).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from hotwire import signals as sig
from hotwire.nt4 import unit_of
from hotwire.settings import data_dir
from hotwire.ui.common import Job
from hotwire.ui.side_panel import SidePanel
from hotwire.ui.signal_strips import StripsArea, make_mime

STATE_FILE = "signals_state.json"
LAYOUT_DIR = "signal_layouts"
WINDOWS = [("10 s", 10), ("30 s", 30), ("1 min", 60), ("5 min", 300), ("10 min", 600)]
RENDER_MS = 50  # 20 fps while live
NAME_ROLE = Qt.ItemDataRole.UserRole
STRIP_ROLE = Qt.ItemDataRole.UserRole + 1
MAX_RECENT = 8


def _fmt(v: float | None) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    a = abs(v)
    if a >= 1000:
        return f"{v:.1f}"
    if a >= 1 or a == 0:
        return f"{v:.3f}".rstrip("0").rstrip(".") if v != int(v) else f"{v:.0f}"
    return f"{v:.4g}"


def _leaf(name: str) -> str:
    return name.rstrip("/").rsplit("/", 1)[-1]


# ====================================================================== signal list
class SignalTree(QTreeWidget):
    def __init__(self, panel: "SignalListPanel"):
        super().__init__()
        self.panel = panel
        self.setColumnCount(3)
        self.setHeaderLabels(["Signal", "Value", "Type"])
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setUniformRowHeights(True)
        # The Signal column fills the panel until you drag its edge; then it keeps your
        # width and the list scrolls sideways (double-click the edge: fit the longest name).
        h = self.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h.setStretchLastSection(False)
        self.setColumnWidth(1, 90)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._user_sized = False
        self._fitting = False
        h.sectionResized.connect(self._section_resized)

    def _section_resized(self, col: int, _old: int, _new: int):
        if col == 0 and not self._fitting:
            self._user_sized = True

    def _fit_name_column(self):
        if self._user_sized:
            return
        w = self.viewport().width() - self.columnWidth(1) - self.columnWidth(2)
        self._fitting = True
        self.setColumnWidth(0, max(w, 120))
        self._fitting = False

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_name_column()

    def selected_names(self) -> list[str]:
        out = []
        for it in self.selectedItems():
            n = it.data(0, NAME_ROLE)
            if n and it.data(0, STRIP_ROLE):  # STRIP_ROLE marks "plottable" here
                out.append(n)
        return out

    def mimeData(self, items):
        names = [it.data(0, NAME_ROLE) for it in items if it.data(0, NAME_ROLE) and it.data(0, STRIP_ROLE)]
        return make_mime(names)

    def mimeTypes(self):
        return ["application/x-hotwire-signals"]

    def startDrag(self, actions):
        if not self.selected_names():
            return
        self.panel.drag_started()
        super().startDrag(actions)


class SignalListPanel(QWidget):
    def __init__(self, tab: "SignalsTab"):
        super().__init__()
        self.tab = tab
        self.source = QComboBox()
        self.source.setToolTip("Where signals come from")
        self.source.activated.connect(self._source_picked)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search (words in any order)…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filter)
        self.count = QLabel("")
        self.count.setStyleSheet("color:#666;")
        self.tree = SignalTree(self)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(self.source)
        lay.addWidget(self.search)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.count)
        self._items: dict[str, QTreeWidgetItem] = {}
        self._names: tuple = ()

    # ------------------------------------------------------------ source combo
    def fill_sources(self, recent: list[str], current: str | None):
        self.source.blockSignals(True)
        self.source.clear()
        self.source.addItem("● Live robot (NetworkTables)", "live")
        for p in recent:
            self.source.addItem("📄 " + Path(p).name, p)
            self.source.setItemData(self.source.count() - 1, p, Qt.ItemDataRole.ToolTipRole)
        self.source.addItem("Open log file…", "open")
        i = self.source.findData(current or "live")
        self.source.setCurrentIndex(max(i, 0))
        self.source.blockSignals(False)

    def _source_picked(self, i: int):
        d = self.source.itemData(i)
        if d == "open":
            self.tab.open_file_dialog()
        elif d == "live":
            self.tab.use_live()
        elif d:
            self.tab.open_file(d)

    # ------------------------------------------------------------ tree
    def set_signals(self, names_types: list[tuple[str, str]]):
        key = tuple(names_types)
        if key == self._names:
            return
        self._names = key
        expanded = {it.data(0, Qt.ItemDataRole.UserRole + 2) for it in self._all_folders() if it.isExpanded()}
        self.tree.clear()
        self._items = {}
        types = dict(names_types)
        tree = sig.build_tree([n for n, _ in names_types])

        def add(parent, node: dict, path: str):
            for k in sorted(node, key=lambda s: (isinstance(node[s], str), s.lower())):
                v = node[k]
                if isinstance(v, dict):
                    it = QTreeWidgetItem([k or "(value)"])
                    it.setData(0, Qt.ItemDataRole.UserRole + 2, path + "/" + k)
                    it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    parent.addChild(it) if parent else self.tree.addTopLevelItem(it)
                    add(it, v, path + "/" + k)
                    it.setExpanded(path + "/" + k in expanded or (not expanded and path == ""))
                else:
                    typ = types.get(v, "")
                    plottable = typ in sig.PLOTTABLE_TYPES
                    unit = self.tab.unit(v)
                    it = QTreeWidgetItem([k or _leaf(v), "", f"{typ} [{unit}]" if unit else typ])
                    it.setData(0, NAME_ROLE, v)
                    it.setData(0, STRIP_ROLE, plottable)
                    it.setToolTip(0, v + ("" if plottable else "  (not plottable)"))
                    if plottable:
                        it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled)
                    else:
                        it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                        it.setForeground(0, QBrush(QColor("#999")))
                    parent.addChild(it) if parent else self.tree.addTopLevelItem(it)
                    self._items[v] = it

        add(None, tree, "")
        self.apply_filter()
        self.tree._fit_name_column()

    def _all_folders(self):
        out = []

        def walk(it):
            for i in range(it.childCount()):
                c = it.child(i)
                if c.childCount():
                    out.append(c)
                    walk(c)

        walk(self.tree.invisibleRootItem())
        return out

    def apply_filter(self):
        q = self.search.text().strip()
        shown = 0

        def walk(it) -> bool:
            nonlocal shown
            any_vis = False
            for i in range(it.childCount()):
                c = it.child(i)
                if c.childCount():
                    vis = walk(c)
                else:
                    vis = not q or sig.matches(c.data(0, NAME_ROLE) or "", q)
                    shown += vis
                c.setHidden(not vis)
                any_vis |= vis
            return any_vis

        walk(self.tree.invisibleRootItem())
        if q:
            self.tree.expandAll()
        self.count.setText(f"{shown} of {len(self._items)} signals" if q else f"{len(self._items)} signals")

    def set_values(self, get_value):
        """get_value(name) -> text, for the visible leaf rows."""
        for name, it in self._items.items():
            if not it.isHidden() and (it.parent() is None or it.parent().isExpanded()):
                it.setText(1, get_value(name))

    def _double_clicked(self, it, col):
        names = self.tree.selected_names() or ([it.data(0, NAME_ROLE)] if it.data(0, STRIP_ROLE) else [])
        if names:
            self.tab.area.add_signals(names)

    def _menu(self, pos):
        names = self.tree.selected_names()
        if not names:
            return
        m = QMenu(self)
        m.addAction(f"Add to selected strip ({len(names)})", lambda: self.tab.area.add_signals(names))
        m.addAction("Add to a new strip", lambda: self.tab.area.add_signals(names, self.tab.area.add_strip()))
        m.addAction("Each in its own strip", lambda: [self.tab.area.add_signals([n], self.tab.area.add_strip()) for n in names])
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def drag_started(self):
        self.tab.left.close()  # fold the pop-out so every strip is a drop target


# ====================================================================== values panel
class ValuesTree(QTreeWidget):
    COLS = ["", "Signal", "Unit", "C1", "C2", "Δ (C2−C1)", "Min", "Max"]

    def __init__(self):
        super().__init__()
        self.setColumnCount(len(self.COLS))
        self.setHeaderLabels(self.COLS)
        self.setRootIsDecorated(False)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setUniformRowHeights(True)
        h = self.header()
        h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setStretchLastSection(False)

    def mimeData(self, items):
        it = items[0] if items else None
        if it is None or it.data(0, NAME_ROLE) is None:
            return make_mime([])
        return make_mime([it.data(0, NAME_ROLE)], (it.data(0, STRIP_ROLE), it.data(0, NAME_ROLE)))

    def mimeTypes(self):
        return ["application/x-hotwire-signals"]


class ValuesPanel(QWidget):
    def __init__(self, tab: "SignalsTab"):
        super().__init__()
        self.tab = tab
        self.head = QLabel("")
        self.head.setTextFormat(Qt.TextFormat.RichText)
        self.head.setWordWrap(True)
        self.tree = ValuesTree()
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemChanged.connect(self._changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        hint = QLabel("Click ■ to change color · right-click for axis / remove · drag a row onto another strip")
        hint.setStyleSheet("color:#666;")
        hint.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(self.head)
        lay.addWidget(self.tree, 1)
        lay.addWidget(hint)
        self._rows: list[tuple[int, str, QTreeWidgetItem]] = []
        self._building = False

    def rebuild(self):
        self._building = True
        self.tree.clear()
        self._rows = []
        area = self.tab.area
        for si, s in enumerate(area.strips):
            top = QTreeWidgetItem(["", f"Strip {si + 1}"])
            top.setFirstColumnSpanned(False)
            f = top.font(1)
            f.setBold(True)
            top.setFont(1, f)
            top.setFlags(Qt.ItemFlag.ItemIsEnabled)
            top.setBackground(1, QBrush(QColor("#eef2f7")))
            self.tree.addTopLevelItem(top)
            if not s.traces:
                e = QTreeWidgetItem(["", "  (empty: drop signals here)"])
                e.setFlags(Qt.ItemFlag.ItemIsEnabled)
                e.setForeground(1, QBrush(QColor("#999")))
                self.tree.addTopLevelItem(e)
            for t in s.traces:
                c = t.cfg
                it = QTreeWidgetItem(["■", "  " + _leaf(c.name) + ("  ▸R" if c.axis == "R" else ""), self.tab.unit(c.name)])
                it.setForeground(0, QBrush(QColor(c.color)))
                f0 = it.font(0)
                f0.setPointSize(f0.pointSize() + 3)
                it.setFont(0, f0)
                it.setToolTip(1, c.name + ("\n(right axis)" if c.axis == "R" else ""))
                it.setData(0, NAME_ROLE, c.name)
                it.setData(0, STRIP_ROLE, si)
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
                            | Qt.ItemFlag.ItemIsDragEnabled)  # fmt: skip
                it.setCheckState(1, Qt.CheckState.Checked if c.visible else Qt.CheckState.Unchecked)
                for col in range(3, 8):
                    it.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.tree.addTopLevelItem(it)
                self._rows.append((si, c.name, it))
        self._building = False
        self.refresh()

    def selected(self) -> tuple[int, str] | None:
        it = self.tree.currentItem()
        if it is None or it.data(0, NAME_ROLE) is None:
            return None
        return it.data(0, STRIP_ROLE), it.data(0, NAME_ROLE)

    def refresh(self):
        tab = self.tab
        area = tab.area
        c1, c2 = area.cursor[1], area.cursor[2]
        two = tab.two_cursors.isChecked()
        self.tree.setColumnHidden(4, not two)
        self.tree.setColumnHidden(5, not two)
        live_now = tab.is_live() and c1 is None
        self.tree.headerItem().setText(3, "Now" if live_now else "C1")
        parts = []
        if c1 is not None:
            parts.append(f"<span style='color:#d32f2f'><b>C1</b> {c1:.3f} s</span>")
        if two and c2 is not None:
            parts.append(f"<span style='color:#0277bd'><b>C2</b> {c2:.3f} s</span>")
        if two and c1 is not None and c2 is not None:
            dt = c2 - c1
            parts.append(f"<b>Δt</b> {dt:.3f} s" + (f" &nbsp; <b>1/Δt</b> {1 / abs(dt):.2f} Hz" if dt else ""))
        if not parts:
            parts.append("<span style='color:#666'>Click a plot to place cursor C1, Shift+click for C2</span>")
        self.head.setText(" &nbsp; ".join(parts))
        a, b = area.x_range()
        for _si, name, it in self._rows:
            x, y = tab.series(name)
            if len(x) == 0:
                for col in range(3, 8):
                    it.setText(col, "")
                it.setText(3, "no data" if not tab.is_live() else "")
                continue
            v1 = float(y[-1]) if live_now else (sig.value_at(x, y, c1) if c1 is not None else None)
            v2 = sig.value_at(x, y, c2) if (two and c2 is not None) else None
            it.setText(3, _fmt(v1))
            it.setText(4, _fmt(v2))
            it.setText(5, _fmt(v2 - v1) if v1 is not None and v2 is not None else "")
            st = sig.window_stats(x, y, a, b)
            it.setText(6, _fmt(st[0]) if st else "")
            it.setText(7, _fmt(st[1]) if st else "")

    def _strip(self, si: int):
        strips = self.tab.area.strips
        return strips[si] if 0 <= si < len(strips) else None

    def _clicked(self, it, col):
        name = it.data(0, NAME_ROLE)
        if name is None:
            return
        s = self._strip(it.data(0, STRIP_ROLE))
        if s:
            self.tab.area.set_active(s)
        if col == 0:
            self.pick_color(it.data(0, STRIP_ROLE), name)

    def pick_color(self, si: int, name: str):
        s = self._strip(si)
        tr = s.trace(name) if s else None
        if tr is None:
            return
        c = QColorDialog.getColor(QColor(tr.cfg.color), self, f"Color for {_leaf(name)}")
        if c.isValid():
            s.set_color(name, c.name())
            self.tab.area.layout_changed.emit()

    def _changed(self, it, col):
        if self._building or col != 1 or it.data(0, NAME_ROLE) is None:
            return
        s = self._strip(it.data(0, STRIP_ROLE))
        if s:
            s.set_visible(it.data(0, NAME_ROLE), it.checkState(1) == Qt.CheckState.Checked)
            self.tab.save_state_soon()

    def _menu(self, pos):
        it = self.tree.itemAt(pos)
        if it is None or it.data(0, NAME_ROLE) is None:
            return
        si, name = it.data(0, STRIP_ROLE), it.data(0, NAME_ROLE)
        s = self._strip(si)
        tr = s.trace(name) if s else None
        if tr is None:
            return
        area = self.tab.area
        m = QMenu(self)
        if tr.cfg.axis == "L":
            m.addAction("Use right Y axis", lambda: (s.set_axis(name, "R"), area.layout_changed.emit()))
        else:
            m.addAction("Use left Y axis", lambda: (s.set_axis(name, "L"), area.layout_changed.emit()))
        m.addAction("Change color…", lambda: self.pick_color(si, name))
        mv = m.addMenu("Move to strip")
        for j, other in enumerate(area.strips):
            if other is not s:
                mv.addAction(f"Strip {j + 1}", lambda o=other: area._dropped(o, {"names": [name], "from": [si, name]}))
        mv.addAction("New strip", lambda: area._dropped(None, {"names": [name], "from": [si, name]}))
        m.addSeparator()
        m.addAction("Remove", lambda: area.remove_signal(s, name))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def remove_selected(self):
        sel = self.selected()
        if sel:
            s = self._strip(sel[0])
            if s:
                self.tab.area.remove_signal(s, sel[1])


# ====================================================================== the tab
class SignalsTab(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.state = self._load_state()
        self.buffer = sig.LiveBuffer()
        self.log: sig.LogData | None = None
        self.live_t0: float | None = None
        self._live_units: dict[str, str] = {}  # from NT topic properties ("unit")
        self._arr_cache: dict[str, tuple[int, np.ndarray, np.ndarray]] = {}
        self._uid_tree = None
        self._uid_plot = None
        self._plot_names: tuple = ()
        self._listening = False
        self._loading: Job | None = None
        self._tick_n = 0

        # ---- toolbar
        bar = QHBoxLayout()
        self.src_label = QLabel()
        bar.addWidget(self.src_label)
        bar.addSpacing(10)
        bar.addWidget(QLabel("Window:"))
        self.win_combo = QComboBox()
        for text, s in WINDOWS:
            self.win_combo.addItem(text, s)
        self.win_combo.setCurrentIndex(max(self.win_combo.findData(self.state.get("window", 30)), 0))
        self.win_combo.currentIndexChanged.connect(lambda _: self.save_state_soon())
        bar.addWidget(self.win_combo)
        self.pause = QPushButton("⏸ Pause")
        self.pause.setCheckable(True)
        self.pause.setToolTip("Freeze the live view (data keeps recording). Zooming or panning pauses too.")
        self.pause.toggled.connect(self._pause_toggled)
        bar.addWidget(self.pause)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setToolTip("Forget the live history")
        self.btn_clear.clicked.connect(self.clear_live)
        bar.addWidget(self.btn_clear)
        b_fit = QPushButton("Fit")
        b_fit.setToolTip("Zoom to fit everything (Home, or double-click a plot)")
        b_fit.clicked.connect(self.fit)
        bar.addWidget(b_fit)
        self.two_cursors = QCheckBox("2 cursors")
        self.two_cursors.setToolTip("Second cursor C2 with Δt and value differences (Shift+click places C2)")
        self.two_cursors.setChecked(bool(self.state.get("two_cursors", False)))
        self.two_cursors.toggled.connect(self._two_toggled)
        bar.addWidget(self.two_cursors)
        b_strip = QPushButton("+ Strip")
        b_strip.clicked.connect(lambda: self.area.add_strip())
        bar.addWidget(b_strip)
        bar.addSpacing(10)
        bar.addWidget(QLabel("Layout:"))
        self.layout_combo = QComboBox()
        self.layout_combo.setMinimumWidth(150)
        self.layout_combo.activated.connect(self._layout_picked)
        bar.addWidget(self.layout_combo)
        for text, fn, tip in (
            ("Save", self.save_layout, "Save strips, signals, colors and axes under this layout name"),
            ("Save as…", self.save_layout_as, "Save as a new layout"),
            ("Delete", self.delete_layout, "Delete this saved layout"),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        self.info = QLabel("")
        self.info.setStyleSheet("color:#b06000; font-weight:600;")
        bar.addWidget(self.info)

        # ---- body: [edge] splitter(strips) [edge]
        self.area = StripsArea()
        self.area.fit_requested = self.fit
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.area)
        self.list_panel = SignalListPanel(self)
        self.values = ValuesPanel(self)
        self.left = SidePanel(self, self.splitter, "left", "Signals", self.list_panel, int(self.state.get("left_w", 330)))
        self.right = SidePanel(self, self.splitter, "right", "Values", self.values, int(self.state.get("right_w", 470)))
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.left.edge)
        body.addWidget(self.splitter, 1)
        body.addWidget(self.right.edge)
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addLayout(body, 1)

        # ---- wiring
        self.area.layout_changed.connect(self._layout_changed)
        self.area.cursors_changed.connect(self._cursors_changed)
        self.area.user_range_changed.connect(self._user_range)
        self.area.data_needed.connect(self._data_needed)
        for key, fn in (
            (Qt.Key.Key_Left, lambda: self._step(-1)),
            (Qt.Key.Key_Right, lambda: self._step(1)),
            (Qt.Key.Key_Backspace, self.area.undo_zoom),
            (Qt.Key.Key_Home, self.fit),
            (Qt.Key.Key_Delete, self.values.remove_selected),
        ):
            sc = QShortcut(QKeySequence(key), self.area)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        for keys, fn in (("Ctrl+B", self._toggle_list_pin), ("Ctrl+F", self._focus_search)):
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        self.timer = QTimer(self)
        self.timer.setInterval(RENDER_MS)
        self.timer.timeout.connect(self._tick)
        self.ctx.tick.connect(self._background_check)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1000)
        self._save_timer.timeout.connect(self.save_state)

        # ---- restore
        self.area.set_layout(sig.Layout.from_dict(self.state.get("current", {})))
        self._fill_layout_combo(self.state.get("layout_name"))
        self.list_panel.fill_sources(self.state.get("recent", []), "live")
        self.left.set_pinned(bool(self.state.get("left_pinned", False)))
        self.right.set_pinned(bool(self.state.get("right_pinned", True)))
        self._update_source_ui()
        self.values.rebuild()

    # ================================================================== state
    def _load_state(self) -> dict:
        try:
            return json.loads((data_dir() / STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_state_soon(self):
        self._save_timer.start()

    def save_state(self):
        for p, key in ((self.left, "left_w"), (self.right, "right_w")):
            if p.pinned and p.frame.width() > 50:
                p.width = p.frame.width()
            self.state[key] = p.width
        self.state.update(
            left_pinned=self.left.pinned,
            right_pinned=self.right.pinned,
            window=self.win_combo.currentData(),
            two_cursors=self.two_cursors.isChecked(),
            current=self.area.to_layout().to_dict(),
        )
        try:
            (data_dir() / STATE_FILE).write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        except OSError:
            pass

    # ================================================================== visibility
    def showEvent(self, e):
        super().showEvent(e)
        self.timer.start()
        QTimer.singleShot(0, self.left.reposition)

    def hideEvent(self, e):
        try:
            self.timer.stop()
            # Keep recording the plotted signals (small send-all subscription) so the plot
            # has no hole when you come back; drop the big "/" tree subscription.
            self._stop_tree()
            self.left.close()
            self.right.close()
            self.save_state_soon()
        except RuntimeError:
            pass  # widgets already deleted (app shutting down)
        super().hideEvent(e)

    def shutdown(self):
        """Called by the main window before quitting."""
        self.timer.stop()
        self._stop_live()
        self.save_state()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.left.reposition()
        self.right.reposition()

    def _toggle_list_pin(self):
        self.left.set_pinned(not self.left.pinned)
        self.save_state_soon()

    def _focus_search(self):
        self.left.open()
        self.list_panel.search.setFocus()
        self.list_panel.search.selectAll()

    # ================================================================== sources
    def is_live(self) -> bool:
        return self.log is None

    def use_live(self):
        if self.log is None:
            return
        self.log = None
        self._arr_cache.clear()
        self.area.clear_data()
        self.list_panel._names = ()
        self.pause.setChecked(False)
        self._update_source_ui()
        self.values.refresh()

    def open_file_dialog(self):
        start = self.state.get("last_dir") or self.ctx.settings.save_folder
        path, _ = QFileDialog.getOpenFileName(self, "Open a robot log", start, "Robot logs (*.wpilog *.csv);;All files (*)")
        if path:
            self.open_file(path)
        else:
            self.list_panel.fill_sources(self.state.get("recent", []), self.log.path if self.log else "live")

    def open_file(self, path: str):
        if self._loading is not None:
            return
        self.info.setText(f"Loading {Path(path).name}…")
        self._stop_live()

        def done(log: sig.LogData):
            self._loading = None
            self.log = log
            self._arr_cache.clear()
            recent = [path] + [p for p in self.state.get("recent", []) if p != path]
            self.state["recent"] = recent[:MAX_RECENT]
            self.state["last_dir"] = str(Path(path).parent)
            self.save_state_soon()
            self.info.setText("")
            self._update_source_ui()
            self._push_all_data()
            self.fit()
            self.ctx.log(f"Signals: opened {Path(path).name} ({len(log.series)} signals, {log.t1 - log.t0:.0f} s)")
            self._show_missing()

        def fail(err):
            self._loading = None
            self.info.setText("")
            self.list_panel.fill_sources(self.state.get("recent", []), self.log.path if self.log else "live")
            QMessageBox.warning(self, "Can't open log", err.splitlines()[-1] if err else "Unknown error")

        self._loading = Job(lambda job: sig.load_file(path)).start(done, fail)

    def _update_source_ui(self):
        live = self.is_live()
        self.list_panel.fill_sources(self.state.get("recent", []), "live" if live else self.log.path)
        if live:
            self.src_label.setText("<b>● Live</b>")
            self.src_label.setStyleSheet("color:#2e9d4f;")
            self.list_panel._names = ()
        else:
            self.src_label.setText(f"<b>📄 {Path(self.log.path).name}</b>")
            self.src_label.setStyleSheet("")
            self.src_label.setToolTip(self.log.path)
            self.list_panel.set_signals(sorted((n, self.log.types.get(n, "double")) for n in self.log.series))
        for w in (self.win_combo, self.pause, self.btn_clear):
            w.setEnabled(live)
        if hasattr(self, "values"):
            self.values.rebuild()  # units differ per source

    def unit(self, name: str) -> str:
        """The signal's unit if the robot/log provides one (topic "unit" property), else ''. Never guessed."""
        if self.log is not None:
            return self.log.units.get(name, "")
        return self._live_units.get(name, "")

    def series(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        """(x, y) for a signal in plot time (seconds from the start of the log / live session)."""
        if self.log is not None:
            c = self._arr_cache.get(name)
            if c is None:
                t, v = self.log.series.get(name, (np.empty(0), np.empty(0)))
                c = (0, t - self.log.t0, v)
                self._arr_cache[name] = c
            return c[1], c[2]
        ver = self.buffer.version(name)
        c = self._arr_cache.get(name)
        if c is None or c[0] != ver:
            t, v = self.buffer.arrays(name)
            c = (ver, t - (self.live_t0 or 0.0), v)
            self._arr_cache[name] = c
        return c[1], c[2]

    def _push_all_data(self):
        for name in self.area.names():
            self._push(name)

    def _push(self, name: str, now: float | None = None):
        x, y = self.series(name)
        if self.is_live() and len(x) and now is not None and now > x[-1]:
            x = np.append(x, now)  # hold the last value up to "now"
            y = np.append(y, y[-1])
        self.area.set_data(name, x, y)

    def _data_needed(self, names):
        if self.is_live():
            for n in names:
                self.buffer.watch(n)
            self._resubscribe()
        else:
            for n in names:
                self._push(n)

    # ================================================================== live
    def _live_allowed(self) -> str | None:
        """None if live is allowed, else the reason it's paused."""
        if self.ctx.locked:
            return "Competition lock: live signals off"
        if getattr(self.ctx.state, "fms_attached", False):
            return "Robot on the field (FMS): live signals paused"
        if not self.ctx.nt.connected:
            return "Robot NetworkTables not connected"
        return None

    def _start_live(self):
        nt = self.ctx.nt
        if self._uid_tree is None:  # topic tree + values: only while the tab is visible
            self._uid_tree = nt.subscribe(["/"], prefix=True, periodic=0.5)
        if self._listening:
            return
        self._listening = True
        nt.add_listener(self._on_value)
        if self.live_t0 is None:
            self.live_t0 = nt.server_time_us() / 1e6
        self._resubscribe()

    def _stop_tree(self):
        if self._uid_tree is not None:
            self.ctx.nt.unsubscribe(self._uid_tree)
            self._uid_tree = None

    def _background_check(self, _state=None):
        """Once a second (app tick): while the tab is hidden, the plotted signals keep
        recording only while live is allowed (no FMS, no lock, NT up) and a live source is used."""
        if self._listening and not self.isVisible() and (not self.is_live() or self._live_allowed()):
            self._stop_live()

    def _stop_live(self):
        if not self._listening:
            return
        nt = self.ctx.nt
        nt.remove_listener(self._on_value)
        for uid in (self._uid_tree, self._uid_plot):
            if uid is not None:
                nt.unsubscribe(uid)
        self._uid_tree = self._uid_plot = None
        self._plot_names = ()
        self._listening = False

    def _resubscribe(self):
        names = tuple(self.area.names())
        for n in set(self.buffer.topics()) - set(names):
            self.buffer.unwatch(n)
        for n in names:
            self.buffer.watch(n)
        if not self._listening or names == self._plot_names:
            return
        nt = self.ctx.nt
        if self._uid_plot is not None:
            nt.unsubscribe(self._uid_plot)
            self._uid_plot = None
        if names:
            self._uid_plot = nt.subscribe(list(names), send_all=True, periodic=0.01)
            for n in names:  # seed with the current value
                tv = nt.get_timestamped(n)
                if tv is not None and not self.buffer.arrays(n)[0].size:
                    self.buffer.append(n, tv[0] / 1e6, tv[1])
        self._plot_names = names

    def _on_value(self, name, ts_us, value):
        self.buffer.append(name, ts_us / 1e6, value)  # NT thread; ignored unless the signal is plotted

    def clear_live(self):
        self.buffer.clear()
        self.live_t0 = self.ctx.nt.server_time_us() / 1e6
        self._arr_cache.clear()
        self.area.clear_data()
        self.area.set_cursor(1, None)
        self.area.set_cursor(2, None)

    def _live_now(self) -> float:
        return self.ctx.nt.server_time_us() / 1e6 - (self.live_t0 or 0.0)

    # ================================================================== render tick
    def _tick(self):
        if not self.isVisible() or self.window().isMinimized():
            return
        self._tick_n += 1
        if self.is_live():
            why = self._live_allowed()
            if why:
                self._stop_live()
                self.info.setText(why)
            else:
                self.info.setText("")
                self._start_live()
            if self._tick_n % 20 == 1:  # topic list (and units) once a second
                topics = self.ctx.nt.topics()
                units = {t.name: u for t in topics if (u := unit_of(t.properties))}
                if units != self._live_units:
                    self._live_units = units
                    self.list_panel._names = ()  # rebuild to show units
                    self.values.rebuild()
                self.list_panel.set_signals([(t.name, t.type) for t in topics])
            if self._listening and not self.pause.isChecked():
                now = self._live_now()
                win = float(self.win_combo.currentData() or 30)
                self.area.set_x_range(now - win, now)
                for name in self.area.names():
                    self._push(name, now)
            if self._tick_n % 4 == 0:
                self.values.refresh()
            if self._tick_n % 10 == 0 and self.left.is_open():
                nt = self.ctx.nt
                self.list_panel.set_values(lambda n: sig.format_value(nt.get(n), 24))
        elif self._tick_n % 10 == 0 and self.left.is_open():
            c1 = self.area.cursor[1]
            if c1 is not None:
                self.list_panel.set_values(lambda n: _fmt(sig.value_at(*self.series(n), c1)) if n in self.log.series else "")

    # ================================================================== interaction
    def _pause_toggled(self, on: bool):
        self.pause.setText("▶ Resume" if on else "⏸ Pause")

    def _user_range(self):
        if self.is_live() and not self.pause.isChecked():
            self.pause.setChecked(True)  # zoom/pan while live = pause so the view stays put
        self.values.refresh()

    def fit(self):
        if self.is_live():
            if self.pause.isChecked():
                xs = [self.series(n)[0] for n in self.area.names()]
                xs = [x for x in xs if len(x)]
                self.area.fit_all((min(x[0] for x in xs), max(x[-1] for x in xs)) if xs else None)
            else:
                self.area.fit_all(None)
        else:
            self.area.fit_all((0.0, self.log.t1 - self.log.t0))
        self.values.refresh()

    def _two_toggled(self, on: bool):
        if on:
            # show both cursors right away, inside the visible time (keep ones already on screen)
            area = self.area
            a, b = area.x_range()
            w = b - a
            c1 = area.cursor[1]
            if c1 is None or not a <= c1 <= b:
                c1 = a + w / 3
                area.set_cursor(1, c1)
            c2 = area.cursor[2]
            if c2 is None or not a <= c2 <= b or abs(c2 - c1) < w * 0.02:
                c2 = c1 + w / 3 if c1 + w / 3 <= b - w * 0.05 else c1 - w / 3
                area.set_cursor(2, c2)
        else:
            self.area.set_cursor(2, None)
        self.values.refresh()
        self.save_state_soon()

    def _cursors_changed(self):
        area = self.area
        if area.cursor[2] is not None and not self.two_cursors.isChecked():
            self.two_cursors.setChecked(True)
        if self.is_live() and not self.pause.isChecked() and (area.cursor[1] is not None or area.cursor[2] is not None):
            self.pause.setChecked(True)  # cursors on a scrolling plot would slide away
        self.values.refresh()

    def _step(self, direction: int):
        sel = self.values.selected()
        names = [sel[1]] if sel else self.area.names()[:1]
        if names:
            self.area.step_cursor(self.series(names[0])[0], direction)

    def _layout_changed(self):
        self.values.rebuild()
        if self.is_live():
            self._resubscribe()
        else:
            self._show_missing()
        self.save_state_soon()

    def _show_missing(self):
        missing = [m for m in self.area.names() if m not in self.log.series]
        self.info.setText(f"{len(missing)} plotted signal(s) aren't in this log" if missing else "")
        self.info.setToolTip("\n".join(missing))

    # ================================================================== layouts
    def _layout_dir(self) -> Path:
        return data_dir() / LAYOUT_DIR

    def _layout_names(self) -> list[str]:
        d = self._layout_dir()
        return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []

    def _fill_layout_combo(self, current: str | None):
        self.layout_combo.blockSignals(True)
        self.layout_combo.clear()
        self.layout_combo.addItem("(not saved)", None)
        for n in self._layout_names():
            self.layout_combo.addItem(n, n)
        i = self.layout_combo.findData(current) if current else 0
        self.layout_combo.setCurrentIndex(max(i, 0))
        self.layout_combo.blockSignals(False)

    def _layout_picked(self, i: int):
        name = self.layout_combo.itemData(i)
        if not name:
            return
        try:
            lay = sig.load_layout(self._layout_dir() / f"{name}.json")
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Layout", f"Can't load layout {name}: {e}")
            return
        self.state["layout_name"] = name
        self.area.set_layout(lay)
        self._arr_cache.clear()
        if not self.is_live():
            self._push_all_data()
        self.fit()

    def save_layout(self):
        name = self.layout_combo.currentData()
        if not name:
            self.save_layout_as()
            return
        sig.save_layout(self.area.to_layout(), self._layout_dir() / f"{name}.json")
        self.ctx.log(f"Signals: saved layout '{name}'")

    def save_layout_as(self):
        name, ok = QInputDialog.getText(self, "Save layout", "Layout name (e.g. Shooter tuning):")
        name = sig.safe_name(name) if ok else ""
        if not name:
            return
        path = self._layout_dir() / f"{name}.json"
        if path.exists() and QMessageBox.question(self, "Save layout", f"Replace layout '{name}'?") != (
            QMessageBox.StandardButton.Yes
        ):
            return
        sig.save_layout(self.area.to_layout(), path)
        self.state["layout_name"] = name
        self._fill_layout_combo(name)
        self.save_state_soon()
        self.ctx.log(f"Signals: saved layout '{name}'")

    def delete_layout(self):
        name = self.layout_combo.currentData()
        if not name:
            return
        if QMessageBox.question(self, "Delete layout", f"Delete saved layout '{name}'?\n(The plots stay as they are.)") != (
            QMessageBox.StandardButton.Yes
        ):
            return
        (self._layout_dir() / f"{name}.json").unlink(missing_ok=True)
        self.state["layout_name"] = None
        self._fill_layout_combo(None)
        self.save_state_soon()
