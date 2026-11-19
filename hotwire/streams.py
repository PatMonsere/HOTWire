"""MJPEG stream reading and stream-quality checks for HOTVision cameras.

Uses the same URL as HOTVision's "View All Cameras" page:
    http://<jetson>:5800/stream/<camera>/<pipeline>
That stream is capped at ~30 fps and HOTVision counts it as a viewer. When
the connection closes, HOTVision stops encoding it. (Avoid /stream/<cam>/raw:
it has no fps cap and isn't counted as a viewer.)
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage

from hotwire import sshconn
from hotwire.settings import Device, Settings


# ------------------------------------------------------------ transport
class TcpLink:
    """A TCP connection to device:port, direct or tunneled through another device."""

    def __init__(self, device: Device, settings: Settings, port: int, timeout: float = 3.0):
        self.sock = None
        self._conn = None
        errors = []
        for h in device.hosts:
            try:
                if h.via:
                    via = settings.device(h.via)
                    if via is None:
                        raise OSError(f"via device {h.via} not found")
                    self._conn = sshconn.connect(via, settings)
                    ch = self._conn.open_tcp(h.host, port)
                    ch.settimeout(timeout)
                    self.sock = ch
                else:
                    s = socket.create_connection((h.host, port), timeout=timeout)
                    s.settimeout(timeout)
                    self.sock = s
                self.host = h.host
                return
            except Exception as e:
                if self._conn:
                    self._conn.close()
                    self._conn = None
                errors.append(f"{h.host}: {e}")
        raise OSError("; ".join(errors) or "no hosts")

    def send(self, data: bytes):
        self.sock.sendall(data)

    def recv(self, n: int) -> bytes:
        return self.sock.recv(n)

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        finally:
            if self._conn:
                self._conn.close()


def http_get_json(device: Device, settings: Settings, path: str, port: int | None = None) -> dict:
    link = TcpLink(device, settings, port or device.web_port)
    try:
        link.send(f"GET {path} HTTP/1.0\r\nHost: {link.host}\r\nConnection: close\r\n\r\n".encode())
        buf = b""
        while True:
            chunk = link.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        link.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0]
    if b" 200 " not in status + b" ":
        raise OSError(status.decode("latin-1"))
    return json.loads(body.decode("utf-8"))


# ------------------------------------------------------------ quality
@dataclass
class StreamStats:
    fps: float = 0.0
    stalled: bool = False  # no frame for > 1.5 s
    frozen: bool = False  # frames arrive but the picture is identical
    brightness: float = 0.0  # mean 0..255
    dark: bool = False
    washed: bool = False
    sharpness: float = 0.0  # variance of Laplacian on a thumbnail
    frames: int = 0
    reconnects: int = 0
    error: str = ""


def _gray_thumb(img: QImage, w: int = 160, h: int = 100) -> np.ndarray:
    small = img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
    g = small.convertToFormat(QImage.Format.Format_Grayscale8)
    bpl = g.bytesPerLine()
    arr = np.frombuffer(g.constBits(), dtype=np.uint8, count=bpl * g.height()).reshape(g.height(), bpl)
    return arr[:, : g.width()].astype(np.float32)


def laplacian_var(a: np.ndarray) -> float:
    lap = -4 * a[1:-1, 1:-1] + a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:]
    return float(lap.var())


class StreamReader(QObject):
    """Reads one MJPEG stream in a thread; emits frames and stats."""

    frame = Signal(QImage)
    stats = Signal(object)

    def __init__(self, device: Device, settings: Settings, camera: str, pipeline: str):
        super().__init__()
        self.device, self.settings = device, settings
        self.camera, self.pipeline = camera, pipeline
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.st = StreamStats()

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"mjpeg-{self.camera}", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as e:
                self.st.error = str(e)[:120]
                self.st.stalled = True
                self.stats.emit(self.st)
            if not self._stop.wait(2.0):
                self.st.reconnects += 1

    def _session(self):
        link = TcpLink(self.device, self.settings, self.device.web_port)
        try:
            path = f"/stream/{self.camera}/{self.pipeline}"
            link.send(f"GET {path} HTTP/1.0\r\nHost: {link.host}\r\n\r\n".encode())
            buf = bytearray()
            times: list[float] = []
            prev = None
            same_since = None
            last_frame = time.time()
            last_emit = 0.0
            self.st.error = ""
            while not self._stop.is_set():
                try:
                    chunk = link.recv(65536)
                except (socket.timeout, TimeoutError):
                    chunk = None
                now = time.time()
                if chunk == b"":
                    raise OSError("stream closed")
                if chunk:
                    buf += chunk
                # extract complete JPEGs (SOI ... EOI)
                while True:
                    s = buf.find(b"\xff\xd8")
                    if s < 0:
                        if len(buf) > 2:
                            del buf[:-2]
                        break
                    e = buf.find(b"\xff\xd9", s + 2)
                    if e < 0:
                        if s > 0:
                            del buf[:s]
                        break
                    jpg = bytes(buf[s : e + 2])
                    del buf[: e + 2]
                    img = QImage.fromData(jpg, "JPG")
                    if img.isNull():
                        continue
                    last_frame = now
                    times.append(now)
                    self.st.frames += 1
                    if self.st.frames % 5 == 0:  # analyze every 5th frame (cheap)
                        g = _gray_thumb(img)
                        self.st.brightness = float(g.mean())
                        self.st.dark = self.st.brightness < 12
                        self.st.washed = self.st.brightness > 235 or float((g >= 250).mean()) > 0.5
                        self.st.sharpness = laplacian_var(g)
                        if prev is not None and float(np.abs(g - prev).mean()) < 0.05:
                            same_since = same_since or now
                        else:
                            same_since = None
                        prev = g
                    self.frame.emit(img)
                if now - last_emit >= 0.5:
                    last_emit = now
                    times = [t for t in times if now - t < 2.0]
                    self.st.fps = len(times) / 2.0
                    self.st.stalled = now - last_frame > 1.5
                    self.st.frozen = same_since is not None and now - same_since > 3.0
                    self.stats.emit(self.st)
        finally:
            link.close()
