"""Camera image tuning (UI-free): find good exposure / gamma / contrast / brightness.

Everything runs from the laptop through HOTVision's existing API, the same way
the BACK_RIGHT camera was tuned by hand in the shop (2026-11-24):

    GET  /api/cameras                     current settings, framerate, device path
    POST /api/cameras/<name>/settings     change settings live (HOTVision also saves them)

Each try is scored from the robot's NetworkTables (apriltag num_tags, capture_fps,
ambiguity) plus the picture itself (brightness, blown highlights, crushed shadows,
contrast) from HOTWire's MJPEG reader. Tags come first; the picture measures are
limits (a washed-out or very dark picture loses even if it decodes tags) and
tie-breakers.

Because HOTVision saves every change on the Jetson, the camera's settings from
before tuning are written to a "pending" file on the laptop first. Keep deletes
it, Undo sends the old settings back. If HOTWire quits or crashes before Keep or
Undo, it puts the old settings back on the next start (it asks first).
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Settings tuned, in this order (each changes the picture less than the one before).
# Gain is not tuned (fixed on the cameras we use).
TUNE_KEYS = ["exposure_time_absolute", "gamma", "contrast", "brightness"]
LABELS = {
    "exposure_time_absolute": "Exposure",
    "gamma": "Gamma",
    "contrast": "Contrast",
    "brightness": "Brightness",
}
SETTLE_S = 0.3  # after HOTVision confirms a change, wait this long before measuring
MEASURE_S = 0.8  # then measure this long (about 80-100 frames at 100-120 fps)

# Picture limits (0..255 gray, fractions of the picture)
BLOWN_MAX = 0.10  # more than 10 % nearly white = washed out (tag whites merge)
CRUSHED_MAX = 0.40  # more than 40 % nearly black = too dark
MEAN_LO, MEAN_HI = 25.0, 200.0


# ------------------------------------------------------------ ranges (v4l2-ctl -l)
Range = tuple[float, float, float]  # (min, max, step)

_CTRL_RE = re.compile(r"^\s*(\w+)\s+0x[0-9a-f]+\s+\((\w+)\)\s*:\s*(.*)$")


def parse_v4l2_controls(text: str) -> dict[str, Range]:
    """{control: (min, max, step)} from `v4l2-ctl -d <dev> -l` output."""
    out: dict[str, Range] = {}
    for line in text.splitlines():
        m = _CTRL_RE.match(line)
        if not m or m.group(2) not in ("int", "int64"):
            continue
        kv = dict(re.findall(r"(\w+)=(-?\d+)", m.group(3)))
        if "min" in kv and "max" in kv:
            out[m.group(1)] = (float(kv["min"]), float(kv["max"]), float(kv.get("step", 1)) or 1.0)
    return out


def fallback_range(key: str, current: float) -> Range:
    """Used when the camera's real ranges can't be read (SSH failed)."""
    if key == "exposure_time_absolute":
        return (1.0, max(current * 3, 200.0), 1.0)
    if key == "brightness":
        return (current - 64, current + 64, 1.0)
    span = max(abs(current), 50.0)
    return (max(0.0, current - span * 0.6), current + span * 0.6, 1.0)


def exposure_cap(framerate: float | None) -> float | None:
    """Longest exposure that still fits one frame (v4l2 exposure_time_absolute is in 100 us units).
    Longer exposures lower the capture fps and blur when the robot moves."""
    if not framerate or framerate <= 0:
        return None
    return 10000.0 / framerate


# ------------------------------------------------------------ measuring
@dataclass
class Measure:
    tags: float = 0.0  # mean detected tags per frame
    tag_samples: int = 0
    fps: float | None = None  # capture fps (NT)
    ambiguity: float | None = None
    mean: float | None = None  # picture brightness 0..255
    blown: float | None = None  # fraction nearly white
    crushed: float | None = None  # fraction nearly black
    contrast: float | None = None  # 95th - 5th percentile, 0..255

    def to_dict(self) -> dict:
        return asdict(self)


class Collector:
    """Gathers samples during one measuring window. add_* may be called from other threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.tags: list[float] = []
            self.amb: list[float] = []
            self.fps: list[float] = []
            self.img: list[tuple[float, float, float, float]] = []

    def add_tags(self, v) -> None:
        with self._lock:
            self.tags.append(float(v))

    def add_ambiguity(self, v) -> None:
        with self._lock:
            self.amb.append(float(v))

    def add_fps(self, v) -> None:
        with self._lock:
            self.fps.append(float(v))

    def add_image(self, mean: float, blown: float, crushed: float, contrast: float) -> None:
        with self._lock:
            self.img.append((mean, blown, crushed, contrast))

    def result(self) -> Measure:
        def avg(xs):
            return sum(xs) / len(xs) if xs else None

        with self._lock:
            m = Measure(
                tags=avg(self.tags) or 0.0,
                tag_samples=len(self.tags),
                fps=avg(self.fps),
                ambiguity=avg(self.amb),
            )
            if self.img:
                m.mean, m.blown, m.crushed, m.contrast = (avg([s[i] for s in self.img]) for i in range(4))
        return m


def picture_problem(m: Measure) -> str:
    """'' if the picture is usable, else why not (washed out / too dark)."""
    if m.mean is None:
        return ""
    if m.mean > MEAN_HI or (m.blown or 0) > BLOWN_MAX:
        return "washed out"
    if m.mean < MEAN_LO or (m.crushed or 0) > CRUSHED_MAX:
        return "too dark"
    return ""


def score(m: Measure, target_fps: float | None) -> float:
    """Higher is better. Tags first; losing capture fps or a bad picture costs a lot."""
    s = 10.0 * m.tags
    if target_fps and m.fps is not None:
        s -= 40.0 * max(0.0, 0.9 - m.fps / target_fps)  # below 90 % of the configured fps
    if m.ambiguity is not None:
        s -= 2.0 * min(m.ambiguity, 1.0)
    if picture_problem(m):
        s -= 100.0  # thrown out, whatever it decodes
    if m.mean is not None:
        s += 2.0 * (m.contrast or 0) / 255.0 - 2.0 * abs(m.mean - 110.0) / 255.0  # tie-breakers
    return s


# ------------------------------------------------------------ search
def _snap(v: float, rng: Range) -> float:
    lo, hi, step = rng
    v = min(max(v, lo), hi)
    v = lo + round((v - lo) / step) * step
    return float(round(v, 3))


class TuneRun:
    """Step-by-step search for one camera.

    Loop: t = next_trial(); if t is None: done. Otherwise apply t (all four settings),
    wait SETTLE_S, measure MEASURE_S, then report(score, measure).
    The first trial is the camera's current settings (the "before" measurement);
    the last one re-applies the best settings (the "after" measurement).
    """

    def __init__(self, before: dict[str, float], ranges: dict[str, Range], framerate: float | None = None):
        self.before = {k: float(before[k]) for k in TUNE_KEYS if k in before}
        self.ranges: dict[str, Range] = {}
        for k in self.before:
            rng = ranges.get(k) or fallback_range(k, self.before[k])
            if k == "exposure_time_absolute" and (cap := exposure_cap(framerate)):
                rng = (rng[0], max(rng[0], min(rng[1], cap)), rng[2])
            self.ranges[k] = rng
        self.framerate = framerate
        self.total = 2 + sum(self._planned(k) for k in self.before)
        self.done_trials = 0
        self.key = ""  # setting being tuned now (for the progress text)
        self.best: dict[str, float] = dict(self.before)
        self.before_measure: Measure | None = None
        self.after_measure: Measure | None = None
        self.before_score: float | None = None
        self.after_score: float | None = None
        self._gen = self._search()
        self._pending: dict | None = None
        self._started = False
        self.finished = False

    @staticmethod
    def _planned(key: str) -> int:
        return 6 if key == "exposure_time_absolute" else 2 if key == "brightness" else 4

    # -- driver API
    def next_trial(self) -> dict[str, float] | None:
        if self.finished:
            return None
        try:
            self._pending = next(self._gen) if not self._started else self._pending
        except StopIteration:
            self.finished = True
            return None
        self._started = True
        return dict(self._pending)

    def report(self, sc: float, m: Measure) -> None:
        self.done_trials += 1
        self._last = (sc, m)
        try:
            self._pending = self._gen.send((sc, m))
        except StopIteration:
            self.finished = True
            self._pending = None

    @property
    def progress(self) -> str:
        what = LABELS.get(self.key, self.key) if self.key else "measuring"
        return f"{what} ({min(self.done_trials + 1, self.total)}/{self.total})"

    # -- the search (a generator: yields settings, receives (score, measure))
    def _search(self):
        cur = dict(self.before)
        sc, m = yield dict(cur)
        self.before_score, self.before_measure = sc, m
        best_sc = sc
        for key in TUNE_KEYS:
            if key not in cur:
                continue
            self.key = key
            rng = self.ranges[key]
            lo, hi, _ = rng
            results: dict[float, float] = {cur[key]: best_sc}

            def trial(v):
                v = _snap(v, rng)
                if v in results:
                    return v, None
                return v, {**cur, key: v}

            # coarse
            if key == "exposure_time_absolute":
                b = max(hi, 1.0)
                a = max(lo, 1.0, b / 20)  # 1/20 of the longest is already very dark
                coarse = [a * (b / a) ** (i / 3) for i in range(4)] if b > a else [a]
            else:
                d = 0.2 * (hi - lo)
                coarse = [cur[key] - d, cur[key] + d] if key != "brightness" else [cur[key] - d / 2, cur[key] + d / 2]
            for v in coarse:
                v, t = trial(v)
                if t is not None:
                    s, _ = yield t
                    results[v] = s
            # refine around the best: halfway to its neighbours
            if key != "brightness":
                vals = sorted(results)
                bv = max(results, key=results.get)
                i = vals.index(bv)
                for j in (i - 1, i + 1):
                    if 0 <= j < len(vals):
                        v, t = trial((bv + vals[j]) / 2)
                        if t is not None:
                            s, _ = yield t
                            results[v] = s
            bv = max(results, key=results.get)
            cur[key] = bv
            best_sc = results[bv]
        self.key = ""
        self.best = dict(cur)
        # final: apply the best settings and measure them (the "after")
        sc, m = yield dict(cur)
        self.after_score, self.after_measure = sc, m


# ------------------------------------------------------------ pending (crash safety)
class PendingStore:
    """{device: {camera: {"before": {...}, "time": epoch}}} saved on the laptop until Keep/Undo."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, d: dict) -> None:
        d = {dev: cams for dev, cams in d.items() if cams}
        if not d:
            self.path.unlink(missing_ok=True)
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, device: str, camera: str, before: dict[str, float]) -> None:
        """Remember the settings from before the first change (a second change keeps the first 'before')."""
        d = self.load()
        cams = d.setdefault(device, {})
        if camera not in cams:
            cams[camera] = {"before": {k: float(v) for k, v in before.items()}, "time": time.time()}
            self._save(d)

    def get(self, device: str, camera: str) -> dict[str, float] | None:
        e = self.load().get(device, {}).get(camera)
        return dict(e["before"]) if e else None

    def remove(self, device: str, camera: str) -> None:
        d = self.load()
        if d.get(device, {}).pop(camera, None) is not None:
            self._save(d)

    def cameras(self, device: str) -> dict[str, dict]:
        return {c: dict(e["before"]) for c, e in self.load().get(device, {}).items()}


def tuned_settings(settings: dict) -> dict[str, float]:
    """Just the tuned keys, as floats."""
    out = {}
    for k in TUNE_KEYS:
        v = settings.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            out[k] = float(v)
    return out


@dataclass
class CameraInfo:
    name: str
    index: str = ""  # device path on the Jetson
    framerate: float | None = None
    settings: dict[str, float] = field(default_factory=dict)


def parse_cameras(api: dict) -> dict[str, CameraInfo]:
    """HOTVision GET /api/cameras -> {name: CameraInfo}."""
    out = {}
    for name, c in (api or {}).items():
        if not isinstance(c, dict):
            continue
        fr = c.get("framerate")
        out[name] = CameraInfo(
            name=name,
            index=str(c.get("index") or ""),
            framerate=float(fr) if isinstance(fr, (int, float)) and fr > 0 else None,
            settings=tuned_settings(c.get("settings") or {}),
        )
    return out
