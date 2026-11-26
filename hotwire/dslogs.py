"""Copy the Driver Station's own logs next to the robot logs.

The 2027 Driver Station writes FIRST_DS_<UTC date>_<time>.wpilog files on this laptop
(C:\\Users\\Public\\Documents\\FIRSTDriverStation\\Logs). They hold what the robot log doesn't:
the robot's console output and errors, alerts and connection status. A DS log starts at the
same moment (within a few seconds) as the robot log of that session, when the DS connects to
new robot code, so the times in the names match.

Where a copy goes:
  - next to the robot log whose name time is within MATCH_WINDOW_S: <that folder>/ds/
    (also FMS match folders <save>/<device>/<EVENT>/<MATCH>/ds/)
  - otherwise <save>/<device>/<date from the name>/ds/, the same date folder as practice logs.
A copy that was put in a date folder is moved next to its robot log once that is downloaded.
Only copies: the originals stay (the Driver Station manages them). A file modified in the last
`in_use_seconds` is still being written and is left for later.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DIR = r"C:\Users\Public\Documents\FIRSTDriverStation\Logs"
DS_RE = re.compile(r"^FIRST_DS_(\d{8})_(\d{6})\.wpilog$")
ROBOT_RE = re.compile(r"^(?:FRC|WPILIB)_(\d{8})_(\d{6})(?:_[A-Za-z0-9]+_[A-Za-z]+\d+)?\.wpilog$")
MATCH_WINDOW_S = 30


@dataclass
class DsCopy:
    src: Path
    dest: Path
    action: str  # "copied" | "moved"


def name_time(name: str) -> datetime | None:
    """The (UTC) time in a DS or robot log name."""
    m = DS_RE.match(name) or ROBOT_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def robot_logs(save_folder: str | Path, device: str) -> list[tuple[datetime, Path]]:
    """(time, folder) of every robot .wpilog already saved for this device."""
    base = Path(save_folder) / device
    out = []
    if base.is_dir():
        for p in base.rglob("*.wpilog"):
            t = name_time(p.name)
            if t is not None and ROBOT_RE.match(p.name):
                out.append((t, p.parent))
    return out


def destination(save_folder: str | Path, device: str, name: str, robot: list[tuple[datetime, Path]]) -> Path:
    t = name_time(name)
    if t is not None:
        near = [(abs((rt - t).total_seconds()), folder) for rt, folder in robot]
        near = [x for x in near if x[0] <= MATCH_WINDOW_S]
        if near:
            return min(near, key=lambda x: x[0])[1] / "ds" / name
    day = t.strftime("%Y-%m-%d") if t else datetime.now().strftime("%Y-%m-%d")
    return Path(save_folder) / device / day / "ds" / name


def copy_new(
    ds_dir: str | Path,
    save_folder: str | Path,
    device: str,
    in_use_seconds: int = 60,
    days: int = 14,
    now: float | None = None,
) -> list[DsCopy]:
    """Copies DS logs from the last `days` days that aren't saved yet. Returns what it did."""
    src_dir = Path(ds_dir)
    if not src_dir.is_dir() or not str(save_folder).strip():
        return []
    now = time.time() if now is None else now
    oldest = datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    robot = robot_logs(save_folder, device)
    done: list[DsCopy] = []
    for src in sorted(src_dir.glob("FIRST_DS_*.wpilog")):
        t = name_time(src.name)
        if t is None or t < oldest:
            continue
        try:
            st = src.stat()
        except OSError:
            continue
        if now - st.st_mtime < in_use_seconds:
            continue  # the DS is still writing it
        dest = destination(save_folder, device, src.name, robot)
        if dest.exists() and dest.stat().st_size == st.st_size:
            continue
        fallback = destination(save_folder, device, src.name, [])
        dest.parent.mkdir(parents=True, exist_ok=True)
        if fallback != dest and fallback.exists() and fallback.stat().st_size == st.st_size:
            os.replace(fallback, dest)  # its robot log arrived: move the copy next to it
            done.append(DsCopy(src, dest, "moved"))
            continue
        part = dest.with_name(dest.name + ".part")
        shutil.copy2(src, part)
        os.replace(part, dest)
        done.append(DsCopy(src, dest, "copied"))
    return done
