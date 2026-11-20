"""NetworkTables backend using pyntcore (the official WPILib library).

Same interface as ``hotwire.nt4.NT4Client`` so the rest of the app doesn't
care which backend runs. pyntcore needs native DLLs, which Windows Smart App
Control blocks; ``make_client`` falls back to the built-in client then.
"""

from __future__ import annotations

import json
import random
import threading
import time

import ntcore

from hotwire.nt4 import Topic, ValueCallback, norm


def _close(sub) -> None:
    """MultiSubscriber has close(); GenericSubscriber unsubscribes when garbage-collected
    (its C++ destructor runs once we drop the last reference)."""
    close = getattr(sub, "close", None)
    if close is not None:
        close()


class PyntcoreClient:
    backend = "pyntcore"

    def __init__(self, port: int = 5810, name: str = "HOTWire"):
        self.port = port
        self.client_name = f"{name}-{random.randint(1000, 9999)}"
        self.hosts: list[str] = []
        self.connect_time = 0.0
        self._inst: ntcore.NetworkTableInstance | None = None
        self._lock = threading.Lock()
        self._values: dict[str, tuple[int, object]] = {}
        # uid -> subscribe() arguments; kept across stop/start (competition lock)
        self._specs: dict[int, dict] = {}
        self._subs: dict[int, tuple[list, list[int]]] = {}  # uid -> (subscribers, listener handles)
        self._uid = 0
        self._listeners: list[ValueCallback] = []
        self._conn_listener = None

    # ------------------------------------------------------------------ API
    @property
    def connected(self) -> bool:
        return self._inst is not None and self._inst.isConnected()

    @property
    def connected_host(self) -> str | None:
        if self._inst is None:
            return None
        conns = self._inst.getConnections()
        return conns[0].remote_ip if conns else None

    def start(self, hosts: list[str]) -> None:
        self.hosts = list(hosts)
        if self._inst is not None:
            return
        inst = ntcore.NetworkTableInstance.create()
        self._inst = inst
        self._conn_listener = inst.addConnectionListener(True, self._on_connection)
        inst.setServer([(h, self.port) for h in self.hosts])
        inst.startClient4(self.client_name)
        with self._lock:
            uids = list(self._specs)
        for uid in uids:
            self._materialize(uid)

    def stop(self) -> None:
        inst = self._inst
        if inst is None:
            return
        self._inst = None
        with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
            self._values.clear()
        for subscribers, handles in subs:
            for h in handles:
                inst.removeListener(h)
            for s in subscribers:
                _close(s)
        inst.stopClient()
        ntcore.NetworkTableInstance.destroy(inst)

    def subscribe(
        self,
        topics: list[str],
        prefix: bool = False,
        send_all: bool = False,
        periodic: float = 0.1,
        topics_only: bool = False,
    ) -> int:
        with self._lock:
            self._uid += 1
            uid = self._uid
            self._specs[uid] = dict(
                topics=list(topics), prefix=prefix, send_all=send_all, periodic=periodic, topics_only=topics_only
            )
        self._materialize(uid)
        return uid

    def _materialize(self, uid: int) -> None:
        inst = self._inst
        spec = self._specs.get(uid)
        if inst is None or spec is None:
            return
        topics, prefix, send_all = spec["topics"], spec["prefix"], spec["send_all"]
        periodic, topics_only = spec["periodic"], spec["topics_only"]
        opts = ntcore.PubSubOptions(sendAll=send_all, periodic=periodic, topicsOnly=topics_only, keepDuplicates=send_all)
        subscribers, handles = [], []
        mask = ntcore.EventFlags.kValueAll
        if prefix:
            prefixes = ["/" if t in ("", "/") else norm(t) for t in topics]
            ms = ntcore.MultiSubscriber(inst, prefixes, opts)
            subscribers.append(ms)
            if not topics_only:
                handles.append(inst.addListener(ms, mask, self._on_event))
        else:
            for t in topics:
                s = inst.getTopic(norm(t)).genericSubscribe(opts)
                subscribers.append(s)
                if not topics_only:
                    handles.append(inst.addListener(s, mask, self._on_event))
        with self._lock:
            self._subs[uid] = (subscribers, handles)

    def unsubscribe(self, uid: int) -> None:
        with self._lock:
            self._specs.pop(uid, None)
            entry = self._subs.pop(uid, None)
        if entry is None or self._inst is None:
            return
        subscribers, handles = entry
        for h in handles:
            self._inst.removeListener(h)
        for s in subscribers:
            _close(s)

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
        if self._inst is None:
            return []
        out = []
        for i in self._inst.getTopicInfo():
            try:
                props = json.loads(i.properties or "{}")
            except (ValueError, TypeError, AttributeError):
                props = {}
            out.append(Topic(i.name, 0, i.type_str, props if isinstance(props, dict) else {}))
        return sorted(out, key=lambda t: t.name)

    def server_time_us(self) -> int:
        off = self._inst.getServerTimeOffset() if self._inst is not None else None
        return int(ntcore._now() + (off or 0))

    # ----------------------------------------------------------- internals
    def _on_connection(self, event) -> None:
        if event.is_(ntcore.EventFlags.kConnected):
            self.connect_time = time.time()
        elif event.is_(ntcore.EventFlags.kDisconnected):
            with self._lock:
                self._values.clear()

    def _on_event(self, event) -> None:
        d = event.data
        try:
            name = d.topic.getName()
            val = d.value
            ts = int(val.server_time())
            v = val.value()
        except Exception:
            return
        self._values[name] = (ts, v)
        for cb in list(self._listeners):
            try:
                cb(name, ts, v)
            except Exception:
                pass


def available() -> tuple[bool, str]:
    """Can pyntcore load here? (False with a reason if Smart App Control etc. blocks it.)"""
    try:
        ntcore.NetworkTableInstance.getDefault()
        return True, f"pyntcore {ntcore.__version__}"
    except Exception as e:  # pragma: no cover
        return False, str(e)
