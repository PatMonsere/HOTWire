"""Record NetworkTables signals during a pit test.

Subscribes with send-all so every value change arrives (not just the latest
every 100 ms). Timestamps are the robot's own (server) timestamps.
"""

from __future__ import annotations

import csv
import threading
from pathlib import Path

from hotwire.nt4 import NT4Client, norm


class Recorder:
    def __init__(self, nt: NT4Client, topics: list[str]):
        self.nt = nt
        self.topics = {norm(t) for t in topics}
        self._lock = threading.Lock()
        self.data: dict[str, list[tuple[float, float]]] = {t: [] for t in self.topics}
        self._uid: int | None = None
        self.t_start_us: int | None = None
        self.t_end_us: int | None = None

    def start(self) -> None:
        self.t_start_us = self.nt.server_time_us()
        self.nt.add_listener(self._on_value)
        self._uid = self.nt.subscribe(sorted(self.topics), send_all=True, periodic=0.005)
        # seed with current values so held values exist from the start
        for t in self.topics:
            tv = self.nt.get_timestamped(t)
            if tv is not None:
                self._append(t, self.t_start_us, tv[1])

    def stop(self) -> None:
        self.nt.remove_listener(self._on_value)
        if self._uid is not None:
            self.nt.unsubscribe(self._uid)
            self._uid = None
        self.t_end_us = self.nt.server_time_us()

    def _append(self, topic: str, ts_us: int, value) -> None:
        if isinstance(value, bool):
            value = 1.0 if value else 0.0
        if not isinstance(value, (int, float)):
            return
        with self._lock:
            self.data[topic].append((ts_us / 1e6, float(value)))

    def _on_value(self, topic: str, ts_us: int, value) -> None:
        if topic in self.topics:
            self._append(topic, ts_us, value)

    def snapshot(self) -> dict[str, list[tuple[float, float]]]:
        with self._lock:
            return {k: sorted(v) for k, v in self.data.items()}

    def now(self) -> float:
        return self.nt.server_time_us() / 1e6


def save_csv(rec: dict[str, list[tuple[float, float]]], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time_s", "topic", "value"])
        rows = sorted((t, topic, v) for topic, s in rec.items() for t, v in s)
        w.writerows(rows)


def load_csv(path: Path) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.setdefault(row["topic"], []).append((float(row["time_s"]), float(row["value"])))
    for s in out.values():
        s.sort()
    return out
