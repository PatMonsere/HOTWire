"""Side panels that pop out on hover (like ETAS MDA) or stay pinned.

Unpinned: a thin vertical tab sits at the edge. Hovering over it slides the
panel out *over* the plot (the plot doesn't resize); it folds back shortly
after the mouse leaves. Clicking the tab, or the pin button, pins it: the
panel then sits in the splitter next to the plot.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QSplitter, QToolButton, QVBoxLayout, QWidget

CLOSE_DELAY_MS = 450


class EdgeTab(QWidget):
    """Thin vertical tab with rotated text. Hover = open, click = pin."""

    hovered = Signal()
    clicked = Signal()

    def __init__(self, text: str, side: str):
        super().__init__()
        self.text = text
        self.side = side
        self.setFixedWidth(22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{text}: hover to open, click to pin")

    def enterEvent(self, e):
        self.hovered.emit()
        super().enterEvent(e)

    def mousePressEvent(self, e):
        self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().color(self.backgroundRole()).darker(108))
        p.setPen(QColor("#3a6ea5"))
        x = 0 if self.side == "right" else self.width() - 3
        p.fillRect(x, 0, 3, self.height(), QColor("#3a6ea5"))
        p.setPen(self.palette().color(self.foregroundRole()))
        f = p.font()
        f.setBold(True)
        p.setFont(f)
        p.translate(self.width() / 2 + 5, 12)
        p.rotate(90)
        p.drawText(0, 0, self.text)
        p.end()


class PanelFrame(QFrame):
    """The panel's box: title bar with a pin button, then the content."""

    pin_toggled = Signal(bool)

    def __init__(self, title: str, content: QWidget):
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.content = content
        head = QHBoxLayout()
        head.setContentsMargins(6, 3, 3, 3)
        lab = QLabel(f"<b>{title}</b>")
        head.addWidget(lab)
        head.addStretch(1)
        self.pin = QToolButton()
        self.pin.setCheckable(True)
        self.pin.setAutoRaise(True)
        self.pin.setText("📌")
        self.pin.toggled.connect(self._pin_changed)
        head.addWidget(self.pin)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        lay.addLayout(head)
        lay.addWidget(content, 1)
        self._pin_changed(False)

    def _pin_changed(self, on: bool):
        self.pin.setToolTip("Unpin (pop out on hover)" if on else "Pin (keep open next to the plot)")
        self.pin_toggled.emit(on)


class SidePanel:
    """Manages one panel (left or right) for a host with a splitter."""

    def __init__(self, host: QWidget, splitter: QSplitter, side: str, title: str, content: QWidget, width: int):
        self.host = host
        self.splitter = splitter
        self.side = side
        self.width = width
        self.pinned = False
        self.frame = PanelFrame(title, content)
        self.edge = EdgeTab(title, side)
        self.edge.hovered.connect(self.open)
        self.edge.clicked.connect(lambda: self.set_pinned(True))
        self.frame.pin_toggled.connect(self._pin_from_button)
        self._timer = QTimer(host)  # watches the mouse while popped out
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._watch)
        self._outside_ms = 0
        self.frame.setParent(host)
        self.frame.hide()

    # ------------------------------------------------------------ state
    def is_open(self) -> bool:
        return self.pinned or self.frame.isVisible()

    def set_pinned(self, on: bool) -> None:
        if on == self.pinned:
            return
        self.pinned = on
        self.frame.pin.blockSignals(True)
        self.frame.pin.setChecked(on)
        self.frame.pin.blockSignals(False)
        self._timer.stop()
        if on:
            self.frame.hide()
            idx = 0 if self.side == "left" else self.splitter.count()
            self.splitter.insertWidget(idx, self.frame)
            self.frame.show()
            sizes = self.splitter.sizes()
            i = self.splitter.indexOf(self.frame)
            center = 1 if self.side == "left" else i - 1
            if sum(sizes) - sizes[i] < 400:  # not laid out yet: sizes are placeholders
                sizes = [max(s, 1) for s in sizes]
                sizes[center] = 2000
                take = self.width
            else:
                take = min(self.width, max(sizes[center] - 200, 150))
            sizes[i] = take
            sizes[center] = max(sizes[center] - take, 100)
            self.splitter.setSizes(sizes)
            self.splitter.setStretchFactor(i, 0)
            self.splitter.setStretchFactor(self.splitter.indexOf(self.splitter.widget(center)), 1)
            self.edge.hide()
        else:
            w = self.frame.width()
            if w > 50:
                self.width = w
            self.frame.setParent(self.host)  # out of the splitter
            self.frame.hide()
            self.edge.show()

    def _pin_from_button(self, on: bool):
        self.set_pinned(on)

    # ------------------------------------------------------------ pop-out
    def open(self) -> None:
        if self.pinned or self.frame.isVisible():
            return
        self._place()
        self.frame.show()
        self.frame.raise_()
        self._outside_ms = 0
        self._timer.start()

    def close(self) -> None:
        if not self.pinned:
            self._timer.stop()
            self.frame.hide()

    def reposition(self) -> None:
        if not self.pinned and self.frame.isVisible():
            self._place()

    def _place(self) -> None:
        sg = self.splitter.geometry()
        w = min(self.width, max(sg.width() - 60, 150))
        x = sg.left() if self.side == "left" else sg.right() - w + 1
        self.frame.setGeometry(QRect(x, sg.top(), w, sg.height()))

    def _watch(self) -> None:
        if not self.frame.isVisible():
            self._timer.stop()
            return
        if QApplication.activePopupWidget() is not None or QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            self._outside_ms = 0  # a combo box / menu is open, or a drag is going on
            return
        pos = QCursor.pos()
        inside = self.frame.rect().contains(self.frame.mapFromGlobal(pos)) or self.edge.rect().contains(
            self.edge.mapFromGlobal(pos)
        )
        if inside:
            self._outside_ms = 0
            return
        self._outside_ms += self._timer.interval()
        if self._outside_ms >= CLOSE_DELAY_MS:
            self.close()
