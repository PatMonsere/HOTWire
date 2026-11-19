"""StreamReader against a local fake HOTVision MJPEG endpoint."""

from __future__ import annotations

import os
import socket
import threading
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from hotwire.settings import Device, HostEntry, Settings  # noqa: E402
from hotwire.streams import StreamReader  # noqa: E402


def jpeg(shade: int, noise_seed: int | None) -> bytes:
    img = QImage(320, 200, QImage.Format.Format_RGB32)
    img.fill(QColor(shade, shade, shade))
    if noise_seed is not None:
        import random

        r = random.Random(noise_seed)
        for _ in range(400):
            img.setPixelColor(r.randrange(320), r.randrange(200), QColor(255, 255, 255))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPG", 80)
    return bytes(ba)


def serve(port, frozen: bool, stop: threading.Event):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen()
    srv.settimeout(0.5)
    while not stop.is_set():
        try:
            c, _ = srv.accept()
        except socket.timeout:
            continue
        c.recv(4096)
        c.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\n\r\n")
        i = 0
        try:
            while not stop.is_set():
                frame = jpeg(90, None if frozen else i)
                c.sendall(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
                i += 1
                time.sleep(1 / 30)
        except OSError:
            pass
        c.close()
    srv.close()


def run_reader(port, frozen, seconds):
    app = QApplication.instance() or QApplication([])
    stop = threading.Event()
    threading.Thread(target=serve, args=(port, frozen, stop), daemon=True).start()
    time.sleep(0.3)
    dev = Device("Jetson", "jetson", "hotros", [HostEntry("127.0.0.1")], web_port=port)
    r = StreamReader(dev, Settings(), "LEFT", "apriltag")
    frames = []
    stats = []
    r.frame.connect(frames.append)
    r.stats.connect(lambda st: stats.append((st.fps, st.frozen, st.stalled, st.brightness)))
    r.start()
    t = time.time()
    while time.time() - t < seconds:
        app.processEvents()
        time.sleep(0.01)
    r.stop()
    stop.set()
    return frames, stats


def test_live_stream():
    frames, stats = run_reader(15830, frozen=False, seconds=3)
    assert len(frames) > 40
    fps, frozen, stalled, bright = stats[-1]
    assert 20 < fps < 40 and not frozen and not stalled and 60 < bright < 140


def test_frozen_stream_detected():
    frames, stats = run_reader(15831, frozen=True, seconds=5)
    assert stats[-1][1], "identical frames for >3 s should be flagged frozen"
