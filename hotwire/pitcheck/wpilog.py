"""Pure-Python reader for WPILib .wpilog files (DataLog format v1.0).

Used to run the pit checks on a robot log after the fact. DataLogManager
stores NetworkTables topics as entries named "NT:/HOT/...".

Format: "WPILOG" + u16 version + u32 extra-header length + extra header,
then records. Each record starts with a bitfield byte giving the byte
lengths of entry id (1-4), payload size (1-4) and timestamp (1-8).
Entry 0 holds control records (0 = start, 1 = finish, 2 = set metadata).
"""

from __future__ import annotations

import struct
from pathlib import Path

from hotwire.nt4 import norm

NUMERIC = {"double", "float", "int64", "boolean"}


def read_wpilog(path: str | Path, wanted_topics: list[str] | None = None):
    """Return ({topic: [(t_seconds, value)]}, {topic: type}) for numeric entries.

    wanted_topics: NT topic names (with or without leading '/'). None = all numeric.
    """
    data = Path(path).read_bytes()
    if data[:6] != b"WPILOG":
        raise ValueError("not a .wpilog file")
    (version,) = struct.unpack_from("<H", data, 6)
    if version >> 8 != 1:
        raise ValueError(f"unsupported wpilog version {version:#x}")
    (extra,) = struct.unpack_from("<I", data, 8)
    pos = 12 + extra
    wanted = {norm(t) for t in wanted_topics} if wanted_topics else None

    entries: dict[int, tuple[str, str]] = {}  # id -> (topic, type)
    out: dict[str, list[tuple[float, float]]] = {}
    types: dict[str, str] = {}
    n = len(data)
    while pos < n:
        bits = data[pos]
        id_len = (bits & 0x3) + 1
        size_len = ((bits >> 2) & 0x3) + 1
        ts_len = ((bits >> 4) & 0x7) + 1
        hdr = 1 + id_len + size_len + ts_len
        if pos + hdr > n:
            break
        p = pos + 1
        eid = int.from_bytes(data[p : p + id_len], "little")
        p += id_len
        size = int.from_bytes(data[p : p + size_len], "little")
        p += size_len
        ts = int.from_bytes(data[p : p + ts_len], "little")
        p += ts_len
        payload = data[p : p + size]
        pos = p + size
        if len(payload) < size:
            break  # truncated (file still being written)
        if eid == 0:
            if payload and payload[0] == 0 and len(payload) >= 9:
                (new_id,) = struct.unpack_from("<I", payload, 1)
                q = 5
                (ln,) = struct.unpack_from("<I", payload, q)
                name = payload[q + 4 : q + 4 + ln].decode("utf-8", "replace")
                q += 4 + ln
                (ln,) = struct.unpack_from("<I", payload, q)
                typ = payload[q + 4 : q + 4 + ln].decode("utf-8", "replace")
                topic = norm(name[3:]) if name.startswith("NT:") else norm(name)
                if typ in NUMERIC and (wanted is None or topic in wanted):
                    entries[new_id] = (topic, typ)
                    types[topic] = typ
            elif payload and payload[0] == 1 and len(payload) >= 5:
                (fin,) = struct.unpack_from("<I", payload, 1)
                entries.pop(fin, None)
            continue
        e = entries.get(eid)
        if e is None:
            continue
        topic, typ = e
        try:
            if typ == "double":
                v = struct.unpack("<d", payload)[0]
            elif typ == "float":
                v = struct.unpack("<f", payload)[0]
            elif typ == "int64":
                v = float(struct.unpack("<q", payload)[0])
            else:
                v = 1.0 if payload[0] else 0.0
        except (struct.error, IndexError):
            continue
        out.setdefault(topic, []).append((ts / 1e6, v))
    for s in out.values():
        s.sort(key=lambda p: p[0])
    return out, types
