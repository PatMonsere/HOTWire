"""List the NetworkTables topics a server publishes (read-only), then disconnect.

    .venv\\Scripts\\python tools\\nt_topics.py                     robot code, 10.0.67.2:5810
    .venv\\Scripts\\python tools\\nt_topics.py --port 6810         SystemCore OS "system" server
    .venv\\Scripts\\python tools\\nt_topics.py --prefix /DriverStation --seconds 5

Prints each topic's name, type and current value. It only subscribes (like
HOTWire), never publishes, and closes the connection when done. Only run it in
the pits, not with the robot on the field.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hotwire.nt4 import NT4Client  # noqa: E402  (built-in client: works with Smart App Control on)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="10.0.67.2")
    ap.add_argument("--port", type=int, default=5810, help="5810 = robot code, 6810 = SystemCore OS system server")
    ap.add_argument("--prefix", default="/", help="only topics under this prefix")
    ap.add_argument("--seconds", type=float, default=3.0, help="how long to collect values after connecting")
    args = ap.parse_args()

    nt = NT4Client(port=args.port)
    nt.start([args.host])
    nt.subscribe([args.prefix], prefix=True, periodic=0.5)
    try:
        t0 = time.time()
        while not nt.connected and time.time() - t0 < 8:
            time.sleep(0.2)
        if not nt.connected:
            print(f"Can't connect to {args.host}:{args.port} (robot on? laptop on the robot network?)")
            return 1
        time.sleep(args.seconds)
        topics = sorted((t for t in nt.topics() if t.name.startswith(args.prefix)), key=lambda t: t.name)
        print(f"{len(topics)} topic(s) on {args.host}:{args.port} under {args.prefix}")
        for t in topics:
            v = repr(nt.get(t.name))
            print(f"  {t.name:60} {t.type:24} {v[:70]}")
        return 0
    finally:
        nt.stop()


if __name__ == "__main__":
    sys.exit(main())
