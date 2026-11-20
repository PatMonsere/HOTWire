"""Minimal pure-Python NetworkTables 4 client.

Why not pyntcore? Windows Smart App Control blocks robotpy's unsigned DLLs on
the team laptops, so this speaks the NT4 protocol directly:

* WebSocket to ws://<host>:5810/nt/<client-name>
* text frames  = JSON control messages (subscribe, announce, ...)
* binary frames = MessagePack arrays [topic_id, timestamp_us, type, value]

Only what HOTWire needs is implemented: subscribing and reading values.
This client never publishes anything to the robot.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

os.environ.setdefault("MSGPACK_PUREPYTHON", "")  # C extension is fine, pure fallback works too
import msgpack  # noqa: E402
import websocket  # websocket-client  # noqa: E402

SUBPROTOCOLS = ["v4.1.networktables.first.wpi.edu", "networktables.first.wpi.edu"]


def norm(name: str) -> str:
    """NT4 topic names start with '/'. Accept either form from users."""
    name = name.strip()
    if name.startswith("NT:"):
        name = name[3:]
    return name if name.startswith("/") else "/" + name


@dataclass
class Topic:
    name: str
    id: int
    type: str
    properties: dict = field(default_factory=dict)  # NT topic properties, e.g. {"unit": "meter"}


def unit_of(properties) -> str:
    """The unit a publisher put in the topic properties ("unit", WPILib convention), else ''."""
    if isinstance(properties, str):
        try:
            properties = json.loads(properties) if properties.strip() else {}
        except ValueError:
            return ""
    if not isinstance(properties, dict):
        return ""
    u = properties.get("unit", properties.get("units"))
    return u if isinstance(u, str) else ""


@dataclass
class _Sub:
    uid: int
    topics: list[str]
    options: dict


# value callback: (topic_name, server_time_us, value)
ValueCallback = Callable[[str, int, object], None]


def make_client(backend: str, port: int):
    """Create a NetworkTables client.

    backend: "auto" (pyntcore if it loads, else built-in), "pyntcore", "builtin".
    Returns (client, description). pyntcore is the official WPILib library;
    the built-in client works where Windows Smart App Control blocks its DLLs.
    """
    if backend in ("auto", "pyntcore"):
        try:
            from hotwire.nt_pyntcore import PyntcoreClient, available

            ok, why = available()
            if ok:
                return PyntcoreClient(port=port), why
            err = why
        except Exception as e:  # ImportError / OSError: DLL blocked or not installed
            err = f"{type(e).__name__}: {e}"
        if backend == "pyntcore":
            return NT4Client(port=port), f"built-in (pyntcore failed: {err[:120]})"
    return NT4Client(port=port), "built-in"


class NT4Client:
    """Background NT4 client with automatic reconnect.

    Call ``start(hosts)`` with a list of hosts to try in order.
    Read values with ``get(name)``; register ``add_listener`` for every value.
    """

    backend = "builtin"

    def __init__(self, port: int = 5810, name: str = "HOTWire"):
        self.port = port
        # Unique name so two laptops (or two app instances) never collide.
        self.client_name = f"{name}-{random.randint(1000, 9999)}"
        self.hosts: list[str] = []
        self.connected_host: str | None = None
        self._ws: websocket.WebSocket | None = None
        self._send_lock = threading.Lock()
        self._lock = threading.Lock()
        self._topics_by_id: dict[int, Topic] = {}
        self._topics_by_name: dict[str, Topic] = {}
        self._values: dict[str, tuple[int, object]] = {}
        self._subs: dict[int, _Sub] = {}
        self._uid = itertools.count(1)
        self._listeners: list[ValueCallback] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._offset_us = 0  # server_time - local_time
        self.connect_time = 0.0

    # ------------------------------------------------------------------ API
    @property
    def connected(self) -> bool:
        return self._ws is not None

    def start(self, hosts: list[str]) -> None:
        self.hosts = list(hosts)
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="nt4", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    def subscribe(
        self,
        topics: list[str],
        prefix: bool = False,
        send_all: bool = False,
        periodic: float = 0.1,
        topics_only: bool = False,
    ) -> int:
        uid = next(self._uid)
        opts = {"periodic": periodic, "all": send_all, "prefix": prefix, "topicsonly": topics_only}
        sub = _Sub(uid, [t if (prefix and t in ("", "/")) else norm(t) for t in topics], opts)
        with self._lock:
            self._subs[uid] = sub
        self._send_sub(sub)
        return uid

    def unsubscribe(self, uid: int) -> None:
        with self._lock:
            self._subs.pop(uid, None)
        self._send_json([{"method": "unsubscribe", "params": {"subuid": uid}}])

    def add_listener(self, cb: ValueCallback) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: ValueCallback) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def get(self, name: str, default=None):
        v = self._values.get(norm(name))
        return default if v is None else v[1]

    def get_timestamped(self, name: str):
        return self._values.get(norm(name))

    def topics(self) -> list[Topic]:
        with self._lock:
            return sorted(self._topics_by_name.values(), key=lambda t: t.name)

    def server_time_us(self) -> int:
        return int(time.time() * 1e6) + self._offset_us

    # ----------------------------------------------------------- internals
    def _run(self) -> None:
        while not self._stop.is_set():
            for host in self.hosts:
                if self._stop.is_set():
                    break
                try:
                    self._session(host)
                except Exception:
                    pass
                finally:
                    self._ws = None
                    self.connected_host = None
            self._stop.wait(2.0)

    def _session(self, host: str) -> None:
        url = f"ws://{host}:{self.port}/nt/{self.client_name}"
        ws = websocket.create_connection(url, subprotocol=None, subprotocols=SUBPROTOCOLS, timeout=2)
        ws.settimeout(1.0)
        with self._lock:
            self._topics_by_id.clear()
            self._topics_by_name.clear()
            self._values.clear()
        self._ws = ws
        self.connected_host = host
        self.connect_time = time.time()
        with self._lock:
            subs = list(self._subs.values())
        for s in subs:
            self._send_sub(s)
        unpacker = msgpack.Unpacker(raw=False, strict_map_key=False)
        last_sync = 0.0
        last_rx = time.time()
        while not self._stop.is_set():
            now = time.time()
            if now - last_sync > 3.0:
                self._time_sync()
                last_sync = now
            if now - last_rx > 6.0:
                raise ConnectionError("NT4 server silent")
            try:
                opcode, data = ws.recv_data()
            except websocket.WebSocketTimeoutException:
                continue
            last_rx = time.time()
            if opcode == websocket.ABNF.OPCODE_TEXT:
                self._on_text(data.decode("utf-8") if isinstance(data, bytes) else data)
            elif opcode == websocket.ABNF.OPCODE_BINARY:
                unpacker.feed(data)
                for msg in unpacker:
                    self._on_value(msg)
            elif opcode == websocket.ABNF.OPCODE_CLOSE:
                raise ConnectionError("closed by server")

    def _time_sync(self) -> None:
        local = int(time.time() * 1e6)
        self._send_bin([-1, 0, 2, local])

    def _send_sub(self, sub: _Sub) -> None:
        self._send_json(
            [{"method": "subscribe", "params": {"topics": sub.topics, "subuid": sub.uid, "options": sub.options}}]
        )

    def _send_json(self, msgs: list) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            with self._send_lock:
                ws.send(json.dumps(msgs))
        except Exception:
            pass

    def _send_bin(self, msg: list) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            with self._send_lock:
                ws.send_binary(msgpack.packb(msg))
        except Exception:
            pass

    def _on_text(self, text: str) -> None:
        try:
            msgs = json.loads(text)
        except ValueError:
            return
        for m in msgs if isinstance(msgs, list) else [msgs]:
            method, p = m.get("method"), m.get("params", {})
            if method == "announce":
                props = p.get("properties") if isinstance(p.get("properties"), dict) else {}
                t = Topic(p["name"], p["id"], p.get("type", ""), dict(props))
                with self._lock:
                    self._topics_by_id[t.id] = t
                    self._topics_by_name[t.name] = t
            elif method == "properties":  # property update: null deletes
                with self._lock:
                    t = self._topics_by_name.get(p.get("name", ""))
                    if t is not None and isinstance(p.get("update"), dict):
                        for k, v in p["update"].items():
                            if v is None:
                                t.properties.pop(k, None)
                            else:
                                t.properties[k] = v
            elif method == "unannounce":
                with self._lock:
                    t = self._topics_by_id.pop(p.get("id"), None)
                    if t:
                        self._topics_by_name.pop(t.name, None)

    def _on_value(self, msg) -> None:
        if not isinstance(msg, (list, tuple)) or len(msg) != 4:
            return
        tid, ts, _type, value = msg
        if tid == -1:  # time sync reply: value = our local send time
            now = int(time.time() * 1e6)
            rtt = now - int(value)
            self._offset_us = int(ts) + rtt // 2 - now
            return
        t = self._topics_by_id.get(tid)
        if t is None:
            return
        self._values[t.name] = (int(ts), value)
        for cb in list(self._listeners):
            try:
                cb(t.name, int(ts), value)
            except Exception:
                pass
