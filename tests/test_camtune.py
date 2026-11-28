"""Camera image tuning: search, scoring, picture limits, pending file, v4l2 ranges."""

from __future__ import annotations

import sys
from pathlib import Path

from hotwire import camtune as ct

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
from fake_robot import FAKE_BEST, FAKE_RANGES, fake_capture_fps, fake_tags  # noqa: E402

V4L2 = """
User Controls

                     brightness 0x00980900 (int)    : min=-64 max=64 step=1 default=0 value=0
                       contrast 0x00980901 (int)    : min=0 max=95 step=1 default=32 value=32
                          gamma 0x00980910 (int)    : min=64 max=300 step=1 default=100 value=170
      white_balance_automatic 0x0098090c (bool)   : default=1 value=0

Camera Controls

                  auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=1 (Manual Mode)
         exposure_time_absolute 0x009a0902 (int)    : min=1 max=5000 step=1 default=157 value=60 flags=inactive
"""


def test_parse_v4l2_controls():
    r = ct.parse_v4l2_controls(V4L2)
    assert r["brightness"] == (-64.0, 64.0, 1.0)
    assert r["gamma"] == (64.0, 300.0, 1.0)
    assert r["exposure_time_absolute"] == (1.0, 5000.0, 1.0)
    assert "auto_exposure" not in r and "white_balance_automatic" not in r  # menus / bools aren't tuned


def test_exposure_cap_from_framerate():
    assert ct.exposure_cap(120) == 10000 / 120  # one frame at 120 fps, in 100 us units
    run = ct.TuneRun({"exposure_time_absolute": 150, "gamma": 120, "contrast": 10, "brightness": 20},
                     {"exposure_time_absolute": (1, 5000, 1)}, framerate=120)
    assert run.ranges["exposure_time_absolute"][1] <= 84


def picture(s: dict) -> tuple[float, float, float, float]:
    """Same model as the fake HOTVision stream: brightness from exposure + brightness, contrast from contrast."""
    level = max(0, min(255, 30 + 1.4 * s["exposure_time_absolute"] + s["brightness"]))
    con = max(0, min(120, 8 + 1.5 * s["contrast"]))
    blown = 0.5 if level + con >= 250 else 0.0
    crushed = 0.5 if level - con <= 5 else 0.0
    return level, blown, crushed, 2 * con


def simulate(cam="BACK_RIGHT", start=None):
    start = start or {"exposure_time_absolute": 150.0, "gamma": 120.0, "contrast": 10.0, "brightness": 20.0}
    ranges = {k: (lo, hi, 1.0) for k, (lo, hi) in FAKE_RANGES.items()}
    run = ct.TuneRun(start, ranges, framerate=120)
    trials = 0
    while (t := run.next_trial()) is not None:
        trials += 1
        c = ct.Collector()
        for _ in range(8):
            c.add_tags(fake_tags(t))
            c.add_fps(fake_capture_fps(cam, t))
        c.add_image(*picture(t))
        m = c.result()
        run.report(ct.score(m, 120), m)
    return run, trials


def test_search_finds_the_shop_settings():
    run, trials = simulate()
    assert run.finished and trials <= run.total, (trials, run.total)  # repeats are skipped
    assert trials <= 20  # about 25 s at ~1.3 s per step
    best = run.best
    assert abs(best["exposure_time_absolute"] - FAKE_BEST["exposure_time_absolute"]) <= 20, best
    assert abs(best["gamma"] - FAKE_BEST["gamma"]) <= 50, best
    assert abs(best["contrast"] - FAKE_BEST["contrast"]) <= 15, best
    assert run.after_measure.tags > run.before_measure.tags + 1.5
    assert run.after_score > run.before_score
    assert run.before == {"exposure_time_absolute": 150.0, "gamma": 120.0, "contrast": 10.0, "brightness": 20.0}


def test_already_good_settings_stay_good():
    run, _ = simulate(start=dict(FAKE_BEST))
    assert run.after_measure.tags >= run.before_measure.tags - 0.3
    assert ct.picture_problem(run.after_measure) == ""


def test_picture_limits_beat_tag_count():
    good = ct.Measure(tags=2.0, fps=119, mean=110, blown=0.01, crushed=0.05, contrast=150)
    washed = ct.Measure(tags=2.5, fps=119, mean=230, blown=0.4, crushed=0.0, contrast=40)
    dark = ct.Measure(tags=2.5, fps=119, mean=15, blown=0.0, crushed=0.7, contrast=20)
    assert ct.picture_problem(washed) == "washed out" and ct.picture_problem(dark) == "too dark"
    assert ct.score(good, 120) > ct.score(washed, 120)
    assert ct.score(good, 120) > ct.score(dark, 120)
    slow = ct.Measure(tags=2.2, fps=60, mean=110, blown=0, crushed=0, contrast=150)  # exposure too long
    assert ct.score(good, 120) > ct.score(slow, 120)


def test_pending_store(tmp_path):
    p = ct.PendingStore(tmp_path / "pending.json")
    assert p.cameras("Jetson") == {}
    p.add("Jetson", "FRONT", {"gamma": 100, "contrast": 10})
    p.add("Jetson", "FRONT", {"gamma": 999})  # a second change keeps the first "before"
    assert p.get("Jetson", "FRONT") == {"gamma": 100.0, "contrast": 10.0}
    p.add("Jetson", "LEFT", {"gamma": 5})
    assert set(p.cameras("Jetson")) == {"FRONT", "LEFT"}
    p.remove("Jetson", "FRONT")
    p.remove("Jetson", "LEFT")
    assert not (tmp_path / "pending.json").exists()  # nothing pending: no file


def test_parse_cameras():
    info = ct.parse_cameras(
        {"FRONT": {"index": "/dev/x", "framerate": 60, "settings": {"gamma": 170.0, "gain": 49.0, "contrast": 32}}}
    )
    assert info["FRONT"].framerate == 60.0 and info["FRONT"].index == "/dev/x"
    assert info["FRONT"].settings == {"gamma": 170.0, "contrast": 32.0}  # only tuned keys
