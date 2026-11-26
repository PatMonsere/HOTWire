"""Driver Station log copies: where they go, what's skipped, moves once the robot log arrives."""

from __future__ import annotations

import os
import time
from pathlib import Path

from hotwire import dslogs


def _ds(d: Path, name: str, age_s: float = 600, data: bytes = b"WPILOG ds") -> Path:
    p = d / name
    p.write_bytes(data)
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def test_copy_rules(tmp_path):
    ds, save = tmp_path / "ds", tmp_path / "save"
    ds.mkdir()
    now = time.time()
    day = time.strftime("%Y%m%d", time.gmtime(now))
    iso = time.strftime("%Y-%m-%d", time.gmtime(now))
    # a practice robot log already saved, and a match log
    (save / "SystemCore" / iso).mkdir(parents=True)
    (save / "SystemCore" / iso / f"WPILIB_{day}_120000.wpilog").write_bytes(b"robot")
    (save / "SystemCore" / "MIGA" / "Q12").mkdir(parents=True)
    (save / "SystemCore" / "MIGA" / "Q12" / f"WPILIB_{day}_130010_MIGA_Q12.wpilog").write_bytes(b"robot")
    _ds(ds, f"FIRST_DS_{day}_120002.wpilog")  # 2 s after the practice log
    _ds(ds, f"FIRST_DS_{day}_130000.wpilog")  # 10 s before the match log
    _ds(ds, f"FIRST_DS_{day}_140000.wpilog")  # no robot log near it
    _ds(ds, f"FIRST_DS_{day}_150000.wpilog", age_s=5)  # still being written
    _ds(ds, "FIRST_DS_20200101_000000.wpilog")  # too old
    (ds / "notes.txt").write_text("x")

    done = dslogs.copy_new(ds, save, "SystemCore", in_use_seconds=60, days=14, now=now)
    dests = sorted(str(c.dest.relative_to(save)).replace("\\", "/") for c in done)
    assert dests == [
        f"SystemCore/{iso}/ds/FIRST_DS_{day}_120002.wpilog",
        f"SystemCore/{iso}/ds/FIRST_DS_{day}_140000.wpilog",
        f"SystemCore/MIGA/Q12/ds/FIRST_DS_{day}_130000.wpilog",
    ]
    assert (ds / f"FIRST_DS_{day}_120002.wpilog").exists()  # originals stay
    assert dslogs.copy_new(ds, save, "SystemCore", now=now) == []  # nothing new

    # the robot log for 14:00 arrives later as a match log: the copy moves next to it
    (save / "SystemCore" / "MIGA" / "Q13").mkdir(parents=True)
    (save / "SystemCore" / "MIGA" / "Q13" / f"WPILIB_{day}_140005_MIGA_Q13.wpilog").write_bytes(b"robot")
    done = dslogs.copy_new(ds, save, "SystemCore", now=now)
    assert [c.action for c in done] == ["moved"]
    assert (save / "SystemCore" / "MIGA" / "Q13" / "ds" / f"FIRST_DS_{day}_140000.wpilog").exists()
    assert not (save / "SystemCore" / iso / "ds" / f"FIRST_DS_{day}_140000.wpilog").exists()


def test_missing_folders(tmp_path):
    assert dslogs.copy_new(tmp_path / "nope", tmp_path / "save", "SystemCore") == []
    assert dslogs.name_time("FIRST_DS_20261125_234634.wpilog").hour == 23
    assert dslogs.name_time("random.wpilog") is None
