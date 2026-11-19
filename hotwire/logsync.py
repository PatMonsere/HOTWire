"""Robot log listing, download, verification and deletion.

Safety rules (keep them!):
1. A robot file is only deleted after the local copy's SHA-256 matches the
   SHA-256 computed *on the robot*. If the robot can't compute a hash, the
   file is never auto-deleted.
2. Files modified within ``in_use_seconds`` of the robot's own clock are
   "in use" (the log being written) and are never downloaded or deleted.
3. Downloads go to ``<file>.part`` and are renamed only after verification.

The ``RemoteFS`` protocol lets tests run the same logic on a local folder.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from hotwire.settings import data_dir

CHUNK = 256 * 1024


@dataclass
class RemoteFile:
    path: str  # absolute posix path on the robot
    rel: str  # path relative to the log dir
    size: int
    mtime: float  # robot clock
    in_use: bool = False

    @property
    def name(self) -> str:
        return posixpath.basename(self.path)


class RemoteFS(Protocol):
    def walk(self, root: str) -> list[tuple[str, int, float]]: ...  # (path, size, mtime)
    def now(self) -> float: ...
    def sha256(self, path: str) -> str | None: ...
    def open_read(self, path: str): ...
    def remove(self, path: str) -> None: ...
    def rmdir_if_empty(self, path: str) -> None: ...
    def disk(self, path: str) -> tuple[int, int] | None: ...  # (used, total) bytes


class SSHRemoteFS:
    """RemoteFS over an sshconn.Connection (SFTP + a few shell commands)."""

    def __init__(self, conn):
        self.conn = conn
        self.sftp = conn.sftp()

    def walk(self, root):
        import stat as st

        out = []
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                entries = self.sftp.listdir_attr(d)
            except IOError:
                continue
            for e in entries:
                p = posixpath.join(d, e.filename)
                if st.S_ISDIR(e.st_mode or 0):
                    stack.append(p)
                elif st.S_ISREG(e.st_mode or 0):
                    out.append((p, int(e.st_size or 0), float(e.st_mtime or 0)))
        return out

    def now(self):
        rc, out, _ = self.conn.run("date +%s")
        try:
            return float(out.strip())
        except ValueError:
            return time.time()

    def sha256(self, path):
        rc, out, _ = self.conn.run(f"sha256sum -- {shlex.quote(path)}", timeout=120)
        if rc != 0:
            return None
        h = out.strip().split(" ", 1)[0].lower()
        return h if re.fullmatch(r"[0-9a-f]{64}", h) else None

    def open_read(self, path):
        f = self.sftp.open(path, "rb")
        f.prefetch()
        return f

    def remove(self, path):
        self.sftp.remove(path)

    def rmdir_if_empty(self, path):
        try:
            if not self.sftp.listdir(path):
                self.sftp.rmdir(path)
        except IOError:
            pass

    def disk(self, path):
        rc, out, _ = self.conn.run(f"df -k {shlex.quote(path)} | tail -1")
        parts = out.split()
        try:
            # Filesystem 1K-blocks Used Available Use% Mounted
            nums = [int(x) for x in parts if x.isdigit()]
            total, used = nums[0], nums[1]
            return used * 1024, total * 1024
        except (IndexError, ValueError):
            return None


# --------------------------------------------------------------- listing
def list_logs(fs: RemoteFS, log_dir: str, exts: list[str], in_use_seconds: int) -> list[RemoteFile]:
    now = fs.now()
    files = []
    exts_l = tuple(e.lower() for e in exts)
    for p, size, mtime in fs.walk(log_dir):
        if exts_l and not p.lower().endswith(exts_l):
            continue
        rel = posixpath.relpath(p, log_dir)
        files.append(RemoteFile(p, rel, size, mtime, in_use=(now - mtime) < in_use_seconds))
    files.sort(key=lambda f: f.mtime)
    return files


# --------------------------------------------------------------- organizing
# 2027 (SystemCore) names logs WPILIB_<date>_<time>[_EVENT_MATCH].wpilog, or
# WPILIB_TBD_<hash>.wpilog before the DS has sent the time; older WPILib used FRC_.
WPILOG_RE = re.compile(r"^(?:FRC|WPILIB)_(\d{8})_(\d{6})(?:_([A-Za-z0-9]+)_([A-Za-z]+\d+))?\.wpilog$")
DATE_RE = re.compile(r"(20\d\d)[-_]?(\d\d)[-_]?(\d\d)")


def destination(save_folder: str, device_name: str, rf: RemoteFile, today: str | None = None) -> Path:
    """Where a robot file is saved locally.

    WPILIB_<date>_<time>_<EVENT>_<Q12>.wpilog -> <save>/<device>/<EVENT>/<Q12>/
    anything else                          -> <save>/<device>/<date>/<relative path>
    The date comes from the file name if it has one, else today's (laptop) date,
    because the robot clock is often wrong.
    """
    base = Path(save_folder) / device_name
    m = WPILOG_RE.match(rf.name)
    if m and m.group(3):
        return base / m.group(3).upper() / m.group(4).upper() / rf.name
    dm = DATE_RE.search(rf.rel)
    if dm:
        day = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
    else:
        day = today or datetime.now().strftime("%Y-%m-%d")
    return base / day / Path(*rf.rel.split("/"))


# --------------------------------------------------------------- index
class Index:
    """Remembers what was downloaded: key -> {local, sha256, time}."""

    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "download_index.json")
        self._lock = threading.Lock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    @staticmethod
    def key(device: str, rf: RemoteFile) -> str:
        return f"{device}|{rf.rel}|{rf.size}"

    def get(self, device: str, rf: RemoteFile) -> dict | None:
        e = self.data.get(self.key(device, rf))
        if e and Path(e["local"]).exists():
            return e
        return None

    def put(self, device: str, rf: RemoteFile, local: Path, sha: str) -> None:
        with self._lock:
            self.data[self.key(device, rf)] = {
                "local": str(local),
                "sha256": sha,
                "time": datetime.now().isoformat(timespec="seconds"),
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            tmp.replace(self.path)


# --------------------------------------------------------------- transfer
class Cancelled(Exception):
    pass


ProgressCb = Callable[[int, int], None]  # (bytes_done, bytes_total)


def download(
    fs: RemoteFS,
    rf: RemoteFile,
    dest: Path,
    speed_limit_kbps: int = 0,
    progress: ProgressCb | None = None,
    cancel: threading.Event | None = None,
) -> str:
    """Copy rf to dest (via dest.part). Returns the local SHA-256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    done = 0
    t0 = time.monotonic()
    rate = speed_limit_kbps * 1024 if speed_limit_kbps > 0 else 0
    with fs.open_read(rf.path) as src, open(part, "wb") as out:
        while True:
            if cancel is not None and cancel.is_set():
                break
            chunk = src.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, rf.size)
            if rate:
                ahead = done / rate - (time.monotonic() - t0)
                if ahead > 0:
                    time.sleep(ahead)
    if cancel is not None and cancel.is_set():
        part.unlink(missing_ok=True)
        raise Cancelled()
    return h.hexdigest()


@dataclass
class SyncResult:
    rf: RemoteFile
    local: Path | None
    ok: bool
    deleted: bool
    message: str


def sync_file(
    fs: RemoteFS,
    device_name: str,
    rf: RemoteFile,
    save_folder: str,
    index: Index,
    delete_after: bool,
    speed_limit_kbps: int = 0,
    progress: ProgressCb | None = None,
    cancel: threading.Event | None = None,
) -> SyncResult:
    """Download, verify against the robot's own hash, record, optionally delete."""
    if rf.in_use:
        return SyncResult(rf, None, False, False, "skipped: in use (being written)")
    dest = destination(save_folder, device_name, rf)
    existing = index.get(device_name, rf)
    if existing:
        local_sha = existing["sha256"]
        dest = Path(existing["local"])
    else:
        if dest.exists():  # same name, different file: don't overwrite
            stem, n = dest.stem, 1
            while dest.exists():
                dest = dest.with_name(f"{stem}__{n}{dest.suffix}")
                n += 1
        local_sha = download(fs, rf, dest, speed_limit_kbps, progress, cancel)
    remote_sha = fs.sha256(rf.path)
    part = dest.with_name(dest.name + ".part")
    if remote_sha is None:
        if part.exists():
            part.replace(dest)
            index.put(device_name, rf, dest, local_sha)
        return SyncResult(rf, dest, True, False, "downloaded, but the robot could not hash it: NOT deleted")
    if remote_sha != local_sha:
        part.unlink(missing_ok=True)
        return SyncResult(rf, None, False, False, "checksum MISMATCH: local copy discarded, robot file kept")
    if part.exists():
        part.replace(dest)
    index.put(device_name, rf, dest, local_sha)
    deleted = False
    if delete_after:
        fs.remove(rf.path)
        parent = posixpath.dirname(rf.path)
        if posixpath.dirname(rf.rel):  # only clean sub-folders, never the log dir itself
            fs.rmdir_if_empty(parent)
        deleted = True
    return SyncResult(rf, dest, True, deleted, "verified" + (", deleted from robot" if deleted else ""))
