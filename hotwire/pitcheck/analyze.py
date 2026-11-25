"""Analyze a recording against a PitConfig.

A recording is ``{topic: [(t_seconds, value), ...]}`` sorted by time. Values
are held between samples (sample-and-hold), like NetworkTables values are.

The same ``analyze`` runs live (``final=False``, called repeatedly while
recording) and after the test (``final=True``). ``learn`` proposes limits
from a known-good run.
"""

from __future__ import annotations

import bisect
import io
import math
from dataclasses import dataclass, field

import numpy as np

from hotwire.pitcheck.config import Check, PitConfig, yaml_rt

Recording = dict[str, list[tuple[float, float]]]
DT = 0.01  # resample step, seconds
ORDER = {"pass": 0, "info": 0, "pending": 1, "warn": 2, "fail": 3}


@dataclass
class Metric:
    signal: str
    kind: str
    value: float | None
    limit: str
    status: str  # pass | warn | fail | pending | info
    detail: str = ""


@dataclass
class CheckResult:
    check: Check
    status: str  # waiting | running | pass | warn | fail | missing
    start: float | None = None
    end: float | None = None
    metrics: list[Metric] = field(default_factory=list)
    note: str = ""


# ------------------------------------------------------------ helpers
def _value_at(ser, t):
    i = bisect.bisect_right(ser, (t, math.inf)) - 1
    return ser[i][1] if i >= 0 else None


def resample(ser, t0: float, t1: float) -> np.ndarray:
    """Sample-and-hold values on a DT grid from t0 to t1 (inclusive)."""
    if not ser or t1 < t0:
        return np.array([])
    times = [p[0] for p in ser]
    grid = np.arange(t0, t1 + DT / 2, DT)
    idx = np.searchsorted(np.asarray(times), grid, side="right") - 1
    vals = np.asarray([p[1] for p in ser], dtype=float)
    out = np.where(idx >= 0, vals[np.clip(idx, 0, None)], np.nan)
    return out[~np.isnan(out)]


def find_start(ser, cond, after: float) -> float | None:
    """First rising edge of cond strictly after `after` (or first sample if already true)."""
    prev = _value_at(ser, after)
    prev_ok = prev is not None and cond.test(prev)
    i = bisect.bisect_right(ser, (after, math.inf))
    for t, v in ser[i:]:
        ok = cond.test(v)
        if ok and not prev_ok:
            return t
        prev_ok = ok
    return None


def _upper(v, lim, m):
    if v <= lim:
        return "pass"
    return "warn" if v <= lim + m * max(abs(lim), 1e-9) else "fail"


def _lower(v, lim, m):
    if v >= lim:
        return "pass"
    return "warn" if v >= lim - m * max(abs(lim), 1e-9) else "fail"


def _band(v, target, tol, m):
    err = abs(v - target)
    if err <= tol:
        return "pass"
    return "warn" if err <= tol * (1 + m) + 1e-12 else "fail"


def _fmt(x):
    return "-" if x is None else f"{x:.3g}"


def worst(statuses) -> str:
    return max(statuses, key=lambda s: ORDER.get(s, 0), default="pass")


# ------------------------------------------------------------ metrics
def _metrics(cfg: PitConfig, alias: str, spec: dict, vals: np.ndarray, t0: float, complete: bool) -> list[Metric]:
    m = cfg.warn_margin
    out: list[Metric] = []
    if vals.size == 0:
        return [Metric(alias, "data", None, "", "pending" if not complete else "fail", "no data recorded")]
    peak = float(np.max(np.abs(vals)))
    n = vals.size
    if "peak_max" in spec:
        st = _upper(peak, spec["peak_max"], m)
        out.append(Metric(alias, "peak", peak, f"<= {spec['peak_max']:g}", st))
    if "peak_min" in spec:
        st = _lower(peak, spec["peak_min"], m)
        if st != "pass" and not complete:
            st = "pending"
        out.append(Metric(alias, "peak", peak, f">= {spec['peak_min']:g}", st))
    if "min" in spec:
        lo = float(np.min(vals))
        out.append(Metric(alias, "min", lo, f">= {spec['min']:g}", _lower(lo, spec["min"], m)))
    if "max" in spec:
        hi = float(np.max(vals))
        out.append(Metric(alias, "max", hi, f"<= {spec['max']:g}", _upper(hi, spec["max"], m)))
    if "steady" in spec:
        lo, hi = spec["steady"]
        if complete:
            k = max(1, int(n * cfg.steady_fraction))
            mean = float(np.mean(vals[-k:]))
            margin = m * max(hi - lo, abs(hi), 1e-9)
            if lo <= mean <= hi:
                st = "pass"
            elif lo - margin <= mean <= hi + margin:
                st = "warn"
            else:
                st = "fail"
            out.append(Metric(alias, "steady", mean, f"{lo:g} .. {hi:g}", st))
        else:
            out.append(Metric(alias, "steady", None, f"{lo:g} .. {hi:g}", "pending"))
    tol = spec.get("tolerance")
    if "final" in spec and tol is not None:
        if complete:
            k = max(3, int(n * 0.05))
            fin = float(np.mean(vals[-k:]))
            out.append(Metric(alias, "final", fin, f"{spec['final']:g} ± {tol:g}", _band(fin, spec["final"], tol, m)))
        else:
            out.append(Metric(alias, "final", None, f"{spec['final']:g} ± {tol:g}", "pending"))
    if "settle_to" in spec and tol is not None and "within" in spec:
        target = spec["settle_to"]
        inside = np.abs(vals - target) <= tol
        # last index that is outside the band; settled after it
        outside = np.nonzero(~inside)[0]
        if outside.size == 0:
            ts = 0.0
        elif outside[-1] == n - 1:
            ts = None
        else:
            ts = (outside[-1] + 1) * DT
        lim = f"<= {spec['within']:g} s to {target:g} ± {tol:g}"
        if ts is None:
            st = "pending" if not complete else "fail"
            out.append(Metric(alias, "settle", None, lim, st, "never settled" if complete else ""))
        else:
            st = _upper(ts, spec["within"], m)
            if not complete and st != "pass":
                st = "pending"
            out.append(Metric(alias, "settle", ts, lim, st))
    if "overshoot_max" in spec:
        target = spec.get("settle_to", spec.get("final"))
        if target is not None:
            move = target - vals[0]
            if abs(move) > 1e-9:
                over = float(np.max((vals - target) * np.sign(move)) / abs(move) * 100)
                over = max(over, 0.0)
                out.append(
                    Metric(alias, "overshoot %", over, f"<= {spec['overshoot_max']:g}", _upper(over, spec["overshoot_max"], m))
                )
    if not out:
        out.append(Metric(alias, "peak", peak, "", "info"))
    return out


# ------------------------------------------------------------ analyze
def analyze(
    cfg: PitConfig, rec: Recording, data_end: float | None = None, final: bool = False, grace: float = 0.0
) -> list[CheckResult]:
    """grace (live mode): seconds to wait past a window's end before grading it, because the
    robot sends NT updates in batches (~100 ms), so the newest samples arrive late."""
    if data_end is None:
        data_end = max((s[-1][0] for s in rec.values() if s), default=0.0)
    results: list[CheckResult] = []
    after = -math.inf
    for chk in cfg.checks:
        ser = rec.get(cfg.topic(chk.start_when.signal), [])
        t0 = find_start(ser, chk.start_when, after)
        if t0 is None:
            results.append(CheckResult(chk, "missing" if final else "waiting", note=f"never saw {chk.start_when}"))
            continue
        t1 = t0 + chk.window
        complete = data_end >= t1 + (0.0 if final else grace)
        tend = min(t1, data_end)
        metrics: list[Metric] = []
        for alias, spec in chk.expect.items():
            vals = resample(rec.get(cfg.topic(alias), []), t0, tend)
            metrics.extend(_metrics(cfg, alias, spec, vals, t0, complete or final))
        if complete or final:
            status = worst(mt.status for mt in metrics)
            status = "fail" if status == "pending" else status
        else:
            status = "fail" if any(mt.status == "fail" for mt in metrics) else "running"
        note = "" if complete else ("recording stopped before the window ended" if final else "")
        results.append(CheckResult(chk, status, t0, t1, metrics, note))
        if cfg.in_order:
            after = t0
    return results


def summary(results: list[CheckResult]) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))


def report_text(results: list[CheckResult]) -> str:
    lines = []
    for r in results:
        lines.append(f"[{r.status.upper():7}] {r.check.name}" + (f"  ({r.note})" if r.note else ""))
        for mt in r.metrics:
            lines.append(f"    {mt.status:7} {mt.signal:20} {mt.kind:12} {_fmt(mt.value):>8}   {mt.limit} {mt.detail}")
    return "\n".join(lines)


# ------------------------------------------------------------ learn
@dataclass
class Suggestion:
    check_index: int
    check_name: str
    signal: str
    key: str
    old: object
    new: object
    observed: str


def _nice(x: float) -> float:
    if x == 0 or not math.isfinite(x):
        return 0.0
    x = float(x)  # numpy floats can't be written to YAML
    digits = 2 - int(math.floor(math.log10(abs(x))))
    return float(round(x, max(digits, 0)))


def learn(cfg: PitConfig, rec: Recording) -> list[Suggestion]:
    """Suggest limits from a known-good run. Targets (final/settle_to) are kept:
    they're setpoints, not something to learn. Only keys already in the file
    are changed, except an empty expectation gets peak_max + steady."""
    sugg: list[Suggestion] = []
    results = analyze(cfg, rec, final=True)
    for i, r in enumerate(results):
        if r.start is None:
            continue
        tend = r.end
        for alias, spec in r.check.expect.items():
            vals = resample(rec.get(cfg.topic(alias), []), r.start, tend)
            if vals.size == 0:
                continue
            peak = float(np.max(np.abs(vals)))
            k = max(1, int(vals.size * cfg.steady_fraction))
            mean = float(np.mean(vals[-k:]))
            lo_v, hi_v = float(np.min(vals)), float(np.max(vals))
            keys = set(spec) or {"peak_max", "steady"}

            def add(key, new, observed):
                sugg.append(Suggestion(i, r.check.name, alias, key, spec.get(key), new, observed))

            if "peak_max" in keys:
                # start-up current spikes vary a lot from run to run (seen 9 A one run, 47 A the next)
                add("peak_max", _nice(peak * 1.5), f"peak {peak:.3g}")
            if "peak_min" in keys:
                add("peak_min", _nice(peak * 0.5), f"peak {peak:.3g}")
            if "steady" in keys:
                half = max(0.4 * abs(mean), 1.0)
                lo = mean - half
                if lo_v >= 0:
                    lo = max(lo, 0.0)
                add("steady", [_nice(lo), _nice(mean + half)], f"steady mean {mean:.3g}")
            if "min" in keys:
                add("min", _nice(lo_v - 0.2 * max(hi_v - lo_v, 1e-6)), f"min {lo_v:.3g}")
            if "max" in keys:
                add("max", _nice(hi_v + 0.2 * max(hi_v - lo_v, 1e-6)), f"max {hi_v:.3g}")
            if "within" in keys and "settle_to" in spec:
                met = next((mt for mt in r.metrics if mt.signal == alias and mt.kind == "settle"), None)
                if met and met.value is not None:
                    add("within", max(_nice(met.value * 1.5), 0.2), f"settled in {met.value:.3g} s")
    return sugg


def apply_suggestions(yaml_text: str, suggestions: list[Suggestion]) -> str:
    """Write accepted suggestions into the YAML, keeping comments and layout."""
    y = yaml_rt()
    doc = y.load(yaml_text)
    for s in suggestions:
        exp = doc["checks"][s.check_index].setdefault("expect", {})
        spec = exp.get(s.signal)
        if spec is None:
            from ruamel.yaml.comments import CommentedMap

            spec = CommentedMap()
            spec.fa.set_flow_style()
            exp[s.signal] = spec
        if isinstance(s.new, list):
            from ruamel.yaml.comments import CommentedSeq

            seq = CommentedSeq(s.new)
            seq.fa.set_flow_style()
            spec[s.key] = seq
        else:
            spec[s.key] = s.new
    buf = io.StringIO()
    y.dump(doc, buf)
    return buf.getvalue()
