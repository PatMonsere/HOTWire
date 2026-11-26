"""Load and validate pit_checks.yaml.

Format (see examples/pit_checks.yaml for a full, commented example):

    defaults:
      warn_margin: 0.10      # 10 % past a limit = warn, more = fail
      steady_fraction: 0.3   # "steady" = last 30 % of the window
      in_order: true         # checks happen in the listed order

    signals:                 # short alias -> NetworkTables topic
      shooter_vel: HOT/Shooter/Left Top/ShooterLeft TopVelocity

    step_signal: HOT/Pit/Step  # optional: the robot's pit step name (a string topic)

    checks:
      - name: Shooter Batter preset
        start_when: shooter_vel > 5     # alias, comparison, number; abs(alias) allowed
        # or, if the robot publishes its step name:
        # start_step: "2/6"             # starts when the step name begins with this
        window: 3.0                     # seconds analyzed after the start
        expect:
          shooter_vel:  { settle_to: 55, tolerance: 2, within: 1.5 }
          shooter_amps: { peak_max: 80, steady: [5, 25] }

Expectation keys (per signal):
    peak_max     largest |value| must be <= this
    peak_min     largest |value| must be >= this (proves it actually moved/drew current)
    steady       [low, high] for the mean over the steady part of the window
    final        value at the end of the window, with `tolerance`
    settle_to    target; must get within `tolerance` and stay there, within `within` seconds
    overshoot_max  % past settle_to/final target (relative to the size of the move)
    min / max    every value in the window must be >= min / <= max
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml import YAML

from hotwire.nt4 import norm

EXPECT_KEYS = {
    "peak_max",
    "peak_min",
    "steady",
    "final",
    "tolerance",
    "settle_to",
    "within",
    "overshoot_max",
    "min",
    "max",
}
COND_RE = re.compile(r"^\s*(abs\(\s*)?([A-Za-z_][\w.]*)\s*(\))?\s*(>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")


@dataclass
class Condition:
    signal: str  # alias
    op: str
    value: float
    use_abs: bool = False

    def test(self, v: float) -> bool:
        x = abs(v) if self.use_abs else v
        return {">": x > self.value, "<": x < self.value, ">=": x >= self.value, "<=": x <= self.value}[self.op]

    def __str__(self) -> str:
        s = f"abs({self.signal})" if self.use_abs else self.signal
        return f"{s} {self.op} {self.value:g}"


@dataclass
class Check:
    name: str
    start_when: Condition | None  # a signal crossing a value, or ...
    window: float
    expect: dict[str, dict]  # alias -> expectation dict
    start_step: str | None = None  # ... the robot's pit step name starting with this text

    def start_text(self) -> str:
        return f"step '{self.start_step}'" if self.start_step else str(self.start_when)


DEFAULT_STEP_TOPIC = "/HOT/Pit/Step"


@dataclass
class PitConfig:
    signals: dict[str, str]  # alias -> normalized NT topic
    checks: list[Check]
    warn_margin: float = 0.10
    steady_fraction: float = 0.3
    in_order: bool = True
    errors: list[str] = field(default_factory=list)
    path: Path | None = None
    step_topic: str = DEFAULT_STEP_TOPIC  # string topic with the robot's pit step name

    def topic(self, alias: str) -> str:
        return self.signals.get(alias, norm(alias))

    def uses_steps(self) -> bool:
        return any(c.start_step for c in self.checks)

    def all_topics(self) -> list[str]:
        """Every topic to record (the step topic only if a check starts on a step)."""
        return sorted(set(self.signals.values()) | ({self.step_topic} if self.uses_steps() else set()))


def yaml_rt() -> YAML:
    y = YAML()  # round-trip: keeps comments when we write suggestions back
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 120
    return y


def _line(node, key=None) -> str:
    try:
        if key is not None:
            return f"line {node.lc.key(key)[0] + 1}"
        return f"line {node.lc.line + 1}"
    except Exception:
        return "?"


def parse_condition(text: str) -> Condition | None:
    m = COND_RE.match(str(text))
    if not m:
        return None
    use_abs = bool(m.group(1))
    if use_abs != bool(m.group(3)):
        return None
    return Condition(m.group(2), m.group(4), float(m.group(5)), use_abs)


def load_text(text: str, path: Path | None = None) -> PitConfig:
    errors: list[str] = []
    try:
        doc = yaml_rt().load(text) or {}
    except Exception as e:
        return PitConfig({}, [], errors=[f"YAML syntax error: {e}"], path=path)
    if not isinstance(doc, dict):
        return PitConfig({}, [], errors=["The file must be a mapping with 'signals' and 'checks'"], path=path)

    defaults = doc.get("defaults") or {}
    signals_raw = doc.get("signals") or {}
    signals: dict[str, str] = {}
    if not isinstance(signals_raw, dict):
        errors.append("'signals' must be a mapping of alias: NT topic")
        signals_raw = {}
    for alias, topic in signals_raw.items():
        if not re.fullmatch(r"[A-Za-z_][\w.]*", str(alias)):
            errors.append(f"{_line(signals_raw, alias)}: signal alias '{alias}' must be letters, digits, _ or .")
            continue
        signals[str(alias)] = norm(str(topic))

    checks: list[Check] = []
    raw_checks = doc.get("checks") or []
    if not isinstance(raw_checks, list):
        errors.append("'checks' must be a list")
        raw_checks = []
    for i, c in enumerate(raw_checks):
        where = _line(c) if hasattr(c, "lc") else f"check #{i + 1}"
        if not isinstance(c, dict):
            errors.append(f"{where}: each check must be a mapping")
            continue
        name = str(c.get("name") or f"check {i + 1}")
        step = c.get("start_step")
        step = str(step).strip() if step is not None and str(step).strip() else None
        cond = None
        if step is not None and c.get("start_when") is not None:
            errors.append(f"{where} ({name}): use start_when or start_step, not both")
            continue
        if step is None:
            cond = parse_condition(c.get("start_when", ""))
            if cond is None:
                errors.append(
                    f"{where} ({name}): start_when must look like 'alias > 5' or 'abs(alias) > 5'"
                    ' (or use start_step: "2/6")'
                )
            elif cond.signal not in signals:
                errors.append(f"{where} ({name}): start_when uses unknown signal '{cond.signal}'")
        try:
            window = float(c.get("window", 2.0))
            if window <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{where} ({name}): window must be a positive number of seconds")
            continue
        expect_raw = c.get("expect") or {}
        expect: dict[str, dict] = {}
        for alias, spec in expect_raw.items():
            alias = str(alias)
            if alias not in signals:
                errors.append(f"{where} ({name}): expect uses unknown signal '{alias}'")
                continue
            if not isinstance(spec, dict):
                errors.append(f"{where} ({name}): expectations for '{alias}' must be a mapping")
                continue
            bad = set(map(str, spec)) - EXPECT_KEYS
            if bad:
                errors.append(f"{where} ({name}): unknown key(s) {sorted(bad)} for '{alias}'")
            e = {}
            for k, v in spec.items():
                k = str(k)
                if k == "steady":
                    try:
                        lo, hi = (float(x) for x in v)
                        e[k] = [min(lo, hi), max(lo, hi)]
                    except (TypeError, ValueError):
                        errors.append(f"{where} ({name}): steady must be [low, high]")
                elif k in EXPECT_KEYS:
                    try:
                        e[k] = float(v)
                    except (TypeError, ValueError):
                        errors.append(f"{where} ({name}): {k} for '{alias}' must be a number")
            if ("final" in e or "settle_to" in e) and "tolerance" not in e:
                errors.append(f"{where} ({name}): '{alias}' has final/settle_to but no tolerance")
            if "settle_to" in e and "within" not in e:
                errors.append(f"{where} ({name}): '{alias}' has settle_to but no 'within' (seconds)")
            expect[alias] = e
        if cond is not None or step is not None:
            checks.append(Check(name, cond, window, expect, step))

    return PitConfig(
        signals,
        checks,
        warn_margin=float(defaults.get("warn_margin", 0.10)),
        steady_fraction=float(defaults.get("steady_fraction", 0.3)),
        in_order=bool(defaults.get("in_order", True)),
        errors=errors,
        path=path,
        step_topic=_step_topic(doc.get("step_signal"), signals),
    )


def _step_topic(raw, signals: dict[str, str]) -> str:
    if raw is None or not str(raw).strip():
        return DEFAULT_STEP_TOPIC
    raw = str(raw).strip()
    return signals.get(raw, norm(raw))  # an alias or a topic


def load(path: Path) -> PitConfig:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        return PitConfig({}, [], errors=[f"Can't read {path}: {e}"], path=Path(path))
    return load_text(text, Path(path))
