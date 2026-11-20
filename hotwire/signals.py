"""Signal viewer data: live buffer, file loading, sample-and-hold lookups.

Everything here is UI-free so it can be tested. Times are seconds; live
data uses the robot's (NT server) clock, files use their own timestamps.
"""

from __future__ import annotations

import bisect
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from hotwire.nt4 import norm, unit_of

PLOTTABLE_TYPES = {"double", "float", "int", "int64", "boolean"}
LIVE_KEEP_S = 600.0  # live history kept per signal


def to_number(value) -> float | None:
    """Plot value for an NT value: numbers as-is, booleans 0/1, everything else None."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


def format_value(value, max_len: int = 60) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (list, tuple)):
        inner = ", ".join(format_value(v, 12) for v in value[:8])
        s = f"[{inner}{', …' if len(value) > 8 else ''}]"
    else:
        s = str(value)
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


class LiveBuffer:
    """Rolling per-topic history, fed from the NT thread and read by the UI thread."""

    def __init__(self, keep_s: float = LIVE_KEEP_S):
        self.keep_s = keep_s
        self._lock = threading.Lock()
        self._t: dict[str, list[float]] = {}
        self._v: dict[str, list[float]] = {}
        self._ver: dict[str, int] = {}

    def version(self, topic: str) -> int:
        """Goes up whenever the topic gets data (cheap 'has it changed?' check)."""
        return self._ver.get(topic, 0)

    def topics(self) -> set[str]:
        with self._lock:
            return set(self._t)

    def watch(self, topic: str) -> None:
        with self._lock:
            self._t.setdefault(norm(topic), [])
            self._v.setdefault(norm(topic), [])

    def unwatch(self, topic: str) -> None:
        with self._lock:
            self._t.pop(norm(topic), None)
            self._v.pop(norm(topic), None)

    def clear(self) -> None:
        with self._lock:
            for k in self._t:
                self._t[k] = []
                self._v[k] = []
                self._ver[k] = self._ver.get(k, 0) + 1

    def append(self, topic: str, t: float, value) -> None:
        v = to_number(value)
        if v is None:
            return
        with self._lock:
            ts = self._t.get(topic)
            if ts is None:
                return  # not watched
            vs = self._v[topic]
            if ts and t < ts[-1]:  # out of order (rare): insert in place
                i = bisect.bisect_right(ts, t)
                ts.insert(i, t)
                vs.insert(i, v)
            else:
                ts.append(t)
                vs.append(v)
            self._ver[topic] = self._ver.get(topic, 0) + 1
            if ts[-1] - ts[0] > self.keep_s * 1.2:  # trim in chunks, not on every append
                cut = bisect.bisect_left(ts, ts[-1] - self.keep_s)
                del ts[:cut]
                del vs[:cut]

    def arrays(self, topic: str) -> tuple[np.ndarray, np.ndarray]:
        with self._lock:
            return np.array(self._t.get(topic, ()), dtype=float), np.array(self._v.get(topic, ()), dtype=float)


@dataclass
class LogData:
    """A loaded file: {topic: (times, values)} plus {topic: type}."""

    path: str
    series: dict[str, tuple[np.ndarray, np.ndarray]]
    types: dict[str, str]
    t0: float
    t1: float
    units: dict[str, str] = field(default_factory=dict)  # only where the log says (topic "unit" property)


def load_file(path: str | Path) -> LogData:
    """Load a .wpilog (numeric entries) or a HOTWire recording .csv."""
    p = Path(path)
    units: dict[str, str] = {}
    if p.suffix.lower() == ".wpilog":
        from hotwire.pitcheck.wpilog import read_wpilog

        meta: dict[str, str] = {}
        rec, types = read_wpilog(p, metadata=meta)
        units = {k: u for k, m in meta.items() if (u := unit_of(m))}
    elif p.suffix.lower() == ".csv":
        from hotwire.pitcheck.recorder import load_csv

        rec = load_csv(p)
        types = {k: "double" for k in rec}
    else:
        raise ValueError(f"Can't open {p.name}: use a .wpilog robot log or a HOTWire recording .csv")
    series = {}
    for topic, pts in rec.items():
        if pts:
            a = np.asarray(pts, dtype=float)
            series[topic] = (a[:, 0], a[:, 1])
    if not series:
        raise ValueError(f"{p.name} has no numeric signals")
    t0 = min(t[0] for t, _ in series.values())
    t1 = max(t[-1] for t, _ in series.values())
    return LogData(str(p), series, types, t0, t1, units)


def value_at(ts: np.ndarray, vs: np.ndarray, t: float) -> float | None:
    """Sample-and-hold value at time t (None before the first sample)."""
    i = int(np.searchsorted(ts, t, side="right")) - 1
    return None if i < 0 else float(vs[i])


def window_stats(ts: np.ndarray, vs: np.ndarray, t_from: float, t_to: float) -> tuple[float, float] | None:
    """(min, max) of the values shown between t_from and t_to, including the held value at t_from."""
    if len(ts) == 0:
        return None
    a = max(int(np.searchsorted(ts, t_from, side="right")) - 1, 0)
    b = int(np.searchsorted(ts, t_to, side="right"))
    seg = vs[a:b]
    if len(seg) == 0:
        return None
    return float(np.nanmin(seg)), float(np.nanmax(seg))


def build_tree(names: list[str]) -> dict:
    """Nested dict for a topic tree: {'HOT': {'Drive': {'TotalCurrent': '/HOT/Drive/TotalCurrent'}}}.

    Leaves hold the full topic name. A name that's both a leaf and a folder keeps
    the leaf under the key '' inside the folder.
    """
    root: dict = {}
    for name in sorted(names):
        parts = [p for p in name.split("/") if p]
        node = root
        for part in parts[:-1]:
            nxt = node.get(part)
            if isinstance(nxt, str):  # leaf that is also a folder
                nxt = {"": nxt}
                node[part] = nxt
            node = node.setdefault(part, {})
        last = parts[-1] if parts else name
        if isinstance(node.get(last), dict):
            node[last][""] = name
        else:
            node[last] = name
    return root


def matches(name: str, query: str) -> bool:
    """Filter: every space-separated word must appear in the topic name (case-insensitive)."""
    n = name.lower()
    return all(w in n for w in query.lower().split())


# ------------------------------------------------------------ layouts (strips + signals)
# Bright colors that read well on the dark grey plot background
PALETTE = [
    "#4fc3f7", "#ffb74d", "#81c784", "#f06292", "#ba68c8",
    "#fff176", "#4db6ac", "#ff8a65", "#9fa8da", "#e0e0e0",
    "#aed581", "#f48fb1", "#80deea", "#ffd54f", "#bcaaa4",
]  # fmt: skip


@dataclass
class TraceCfg:
    name: str
    color: str
    axis: str = "L"  # "L" or "R"
    visible: bool = True


@dataclass
class StripCfg:
    traces: list[TraceCfg] = field(default_factory=list)
    height: int = 0  # 0 = share evenly


@dataclass
class Layout:
    strips: list[StripCfg] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Layout":
        strips = []
        for s in d.get("strips", []):
            traces = [
                TraceCfg(str(t["name"]), str(t.get("color", PALETTE[0])), t.get("axis", "L"), bool(t.get("visible", True)))
                for t in s.get("traces", [])
                if t.get("name")
            ]
            strips.append(StripCfg(traces, int(s.get("height", 0))))
        return Layout(strips)

    def all_names(self) -> list[str]:
        return [t.name for s in self.strips for t in s.traces]

    def next_color(self) -> str:
        used = {t.color for s in self.strips for t in s.traces}
        for c in PALETTE:
            if c not in used:
                return c
        return PALETTE[len(self.all_names()) % len(PALETTE)]


def save_layout(layout: Layout, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(layout.to_dict(), indent=1), encoding="utf-8")


def load_layout(path: Path) -> Layout:
    return Layout.from_dict(json.loads(path.read_text(encoding="utf-8")))


def safe_name(name: str) -> str:
    """Layout name -> file name."""
    return re.sub(r"[^A-Za-z0-9 _.-]+", "_", name).strip() or "layout"
