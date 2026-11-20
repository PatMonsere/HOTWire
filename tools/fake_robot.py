"""Fake robot NetworkTables 4 server for testing HOTWire without hardware.

    python tools/fake_robot.py [--port 5810] [--fms] [--enabled] [--loop]

Publishes the DS control word, a few HOTVision camera topics and simulated
pit test signals (intake, indexer, shooter, drive) that follow the order of
the robot's pit test routine. Point HOTWire's NT device at 127.0.0.1.

This is a test tool: a tiny WebSocket + NT4 server, not a full implementation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import socket
import struct
import threading
import time

import msgpack

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 magic value
T0 = time.monotonic()


def now_us() -> int:
    return int((time.monotonic() - T0) * 1e6) + 1_000_000


TOPICS = {
    "/DriverStation/ControlWord": "struct:ControlWord",  # 2027 SystemCore format
    "/DriverStation/EventName": "string",
    "/DriverStation/MatchNumber": "int",
    "/DriverStation/MatchType": "int",
    "/HOT/Intake/Extend/IntakeExtendPosition": "double",
    "/HOT/Intake/Extend/IntakeExtendSupply Current": "double",
    "/HOT/Intake/Roller/IntakeRollerVelocity": "double",
    "/HOT/Intake/Roller/IntakeRollerSupply Current": "double",
    "/HOT/Indexer/Floor/IndexerFloorVelocity": "double",
    "/HOT/Indexer/Floor/IndexerFloorSupply Current": "double",
    "/HOT/Indexer/LeftKick/IndexerLeftKickVelocity": "double",
    "/HOT/Indexer/LeftKick/IndexerLeftKickSupply Current": "double",
    "/HOT/Shooter/Left Top/ShooterLeft TopVelocity": "double",
    "/HOT/Shooter/Left Top/ShooterLeft TopSupply Current": "double",
    "/HOT/Shooter/Hood/ShooterHoodPosition": "double",
    "/HOT/Shooter/Hood/ShooterHoodSupply Current": "double",
    "/HOT/Drive/TotalCurrent": "double",
    "/HOT/Robot/BatteryVoltage": "double",
}
for cam in ("LEFT", "FRONT", "BACK_LEFT", "BACK_RIGHT"):
    TOPICS[f"/HOTVision/{cam}/connected"] = "boolean"
    TOPICS[f"/HOTVision/{cam}/capture_fps"] = "double"
    TOPICS[f"/HOTVision/{cam}/apriltag/latency"] = "double"
    TOPICS[f"/HOTVision/{cam}/apriltag/num_tags"] = "int"
    TOPICS[f"/HOTVision/{cam}/status/fps"] = "double"  # heartbeat camera health (not a pipeline)
# HOTVision heartbeat and events (jetson-heartbeat branch)
for key in ("seq", "jetson_uptime_s", "app_uptime_s", "cpu_temp_c", "gpu_temp_c", "vin_v", "vin_min_v"):
    TOPICS[f"/HOTVision/heartbeat/{key}"] = "double"
TOPICS["/HOTVision/heartbeat/throttled"] = "boolean"
TOPICS["/HOTVision/events/last_event"] = "string"
TOPICS["/HOTVision/events/event_count"] = "double"
TOPICS["/HOTVision/events/usb_disconnects"] = "double"
# Topic properties, like a WPILib unit topic publishes ("unit" = the unit's name)
PROPERTIES = {"/HOT/Robot/BatteryVoltage": {"unit": "volt"}}
TYPE_IDS = {"boolean": 0, "double": 1, "int": 2, "float": 3, "string": 4, "struct:ControlWord": 5}


def control_word(enabled: bool, fms: bool, ds: bool = True, mode: int = 3) -> bytes:
    """2027 struct:ControlWord: opModeHash:56, robotMode:2, enabled, eStop, fmsAttached, dsAttached."""
    w = (mode << 56) | (int(enabled) << 58) | (int(fms) << 60) | (int(ds) << 61)
    return w.to_bytes(8, "little")
IDS = {name: i + 1 for i, name in enumerate(TOPICS)}


def first_order(t, start, target, tau=0.25):
    if t < start:
        return 0.0
    return target * (1 - math.exp(-(t - start) / tau))


def pit_signals(t: float) -> dict:
    """A 30 s pit test cycle: intake 2-6, rollers 7-10, indexer 11-14, shooter 15-20, drive 22-25."""
    c = t % 30
    v = {}
    ext_on = 2 <= c < 6
    v["/HOT/Intake/Extend/IntakeExtendPosition"] = first_order(c, 2, 12.3) if ext_on else 0.0
    v["/HOT/Intake/Extend/IntakeExtendSupply Current"] = (
        (25 * math.exp(-(c - 2) / 0.2) + 1.5) if ext_on else 0.0
    )
    roll = 7 <= c < 10
    v["/HOT/Intake/Roller/IntakeRollerVelocity"] = first_order(c, 7, 40) if roll else 0.0
    v["/HOT/Intake/Roller/IntakeRollerSupply Current"] = (30 * math.exp(-(c - 7) / 0.15) + 6) if roll else 0.0
    idx = 11 <= c < 14
    for k in ("Floor", "LeftKick"):
        v[f"/HOT/Indexer/{k}/Indexer{k}Velocity"] = first_order(c, 11, 35) if idx else 0.0
        v[f"/HOT/Indexer/{k}/Indexer{k}Supply Current"] = (25 * math.exp(-(c - 11) / 0.15) + 5) if idx else 0.0
    sh = 15 <= c < 20
    v["/HOT/Shooter/Left Top/ShooterLeft TopVelocity"] = first_order(c, 15, 55, 0.4) if sh else 0.0
    v["/HOT/Shooter/Left Top/ShooterLeft TopSupply Current"] = (60 * math.exp(-(c - 15) / 0.3) + 12) if sh else 0.0
    v["/HOT/Shooter/Hood/ShooterHoodPosition"] = first_order(c, 15, 3.0, 0.2) if sh else 0.0
    v["/HOT/Shooter/Hood/ShooterHoodSupply Current"] = (10 * math.exp(-(c - 15) / 0.2) + 1) if sh else 0.0
    dr = 22 <= c < 25
    v["/HOT/Drive/TotalCurrent"] = (90 * math.exp(-(c - 22) / 0.3) + 35 + 5 * math.sin(c * 7)) if dr else 2.0
    v["/HOT/Robot/BatteryVoltage"] = 12.6 - 0.012 * v["/HOT/Drive/TotalCurrent"]  # sag under load
    return v


class Client(threading.Thread):
    def __init__(self, sock, state):
        super().__init__(daemon=True)
        self.sock, self.state = sock, state
        self.subs: dict[int, tuple[list[str], dict]] = {}
        self.announced: set[str] = set()
        self.lock = threading.Lock()
        self.alive = True

    # --- websocket framing (server side: unmasked out, masked in)
    def send_frame(self, opcode: int, payload: bytes):
        hdr = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            hdr += bytes([n])
        elif n < 65536:
            hdr += bytes([126]) + struct.pack(">H", n)
        else:
            hdr += bytes([127]) + struct.pack(">Q", n)
        with self.lock:
            self.sock.sendall(hdr + payload)

    def recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def recv_frame(self):
        b1, b2 = self.recv_exact(2)
        opcode, masked, n = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self.recv_exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.recv_exact(8))[0]
        mask = self.recv_exact(4) if masked else b"\0\0\0\0"
        data = bytearray(self.recv_exact(n))
        for i in range(n):
            data[i] ^= mask[i % 4]
        return opcode, bytes(data)

    def handshake(self):
        req = b""
        while b"\r\n\r\n" not in req:
            req += self.sock.recv(4096)
        headers = {}
        for line in req.decode().split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        key = headers["sec-websocket-key"]
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        proto = headers.get("sec-websocket-protocol", "").split(",")[0].strip()
        self.sock.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: {proto}\r\n\r\n"
            ).encode()
        )

    def matches(self, name):
        for topics, opts in self.subs.values():
            for t in topics:
                if (opts.get("prefix") and name.startswith(t)) or name == t:
                    return True
        return False

    def announce_matching(self):
        msgs = []
        for name, typ in TOPICS.items():
            if name not in self.announced and self.matches(name):
                self.announced.add(name)
                props = PROPERTIES.get(name, {})
                msgs.append({"method": "announce", "params": {"name": name, "id": IDS[name], "type": typ, "properties": props}})
        if msgs:
            self.send_frame(1, json.dumps(msgs).encode())

    def send_values(self, values: dict):
        out = b""
        for name, val in values.items():
            if name in self.announced:
                out += msgpack.packb([IDS[name], now_us(), TYPE_IDS[TOPICS[name]], val])
        if out:
            self.send_frame(2, out)

    def run(self):
        try:
            self.handshake()
            self.state["clients"].append(self)
            while True:
                op, data = self.recv_frame()
                if op == 8:
                    break
                if op == 9:
                    self.send_frame(10, data)
                elif op == 1:
                    for m in json.loads(data):
                        p = m.get("params", {})
                        if m.get("method") == "subscribe":
                            self.subs[p["subuid"]] = (p.get("topics", []), p.get("options", {}))
                            self.announce_matching()
                            self.send_values(self.state["values"])
                        elif m.get("method") == "unsubscribe":
                            self.subs.pop(p.get("subuid"), None)
                elif op == 2:
                    up = msgpack.Unpacker(raw=False)
                    up.feed(data)
                    for msg in up:
                        if msg[0] == -1:
                            self.send_frame(2, msgpack.packb([-1, now_us(), 2, msg[3]]))
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            self.alive = False
            if self in self.state["clients"]:
                self.state["clients"].remove(self)
            self.sock.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5810)
    ap.add_argument("--fms", action="store_true", help="pretend the FMS is attached")
    ap.add_argument("--enabled", action="store_true", help="pretend the robot is enabled")
    args = ap.parse_args()
    word = control_word(args.enabled, args.fms, mode=2 if args.fms else 3)
    state = {"clients": [], "values": {}}
    state["values"].update(
        {
            "/DriverStation/ControlWord": word,
            "/DriverStation/EventName": "TEST" if args.fms else "",
            "/DriverStation/MatchNumber": 12 if args.fms else 0,
            "/DriverStation/MatchType": 2 if args.fms else 0,
        }
    )
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen()
    print(f"fake robot NT4 server on 127.0.0.1:{args.port}  control word={word}")

    def accept_loop():
        while True:
            s, _ = srv.accept()
            Client(s, state).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    t_start = time.monotonic()
    last_ctrl = 0.0
    last_seq, n_events = -1, 0
    while True:
        t = time.monotonic() - t_start
        vals = pit_signals(t)
        for cam in ("LEFT", "FRONT", "BACK_LEFT", "BACK_RIGHT"):
            vals[f"/HOTVision/{cam}/connected"] = True
            vals[f"/HOTVision/{cam}/capture_fps"] = 119.5 if cam != "FRONT" else 60.0
            vals[f"/HOTVision/{cam}/apriltag/latency"] = 18.0
            vals[f"/HOTVision/{cam}/apriltag/num_tags"] = 2
            vals[f"/HOTVision/{cam}/status/fps"] = 119.5
        seq = int(t / 0.25)
        if seq != last_seq:
            last_seq = seq
            hb = "/HOTVision/heartbeat/"
            vals.update({hb + "seq": float(seq), hb + "jetson_uptime_s": 3600 + t, hb + "app_uptime_s": 600 + t,
                         hb + "cpu_temp_c": 48.0, hb + "gpu_temp_c": 46.0, hb + "throttled": False,
                         hb + "vin_v": 12.2, hb + "vin_min_v": 12.1})
            if seq % 8 == 0:  # an event every 2 s
                n_events += 1
                vals["/HOTVision/events/last_event"] = f"fake: test event #{n_events}"
                vals["/HOTVision/events/event_count"] = float(n_events)
                vals["/HOTVision/events/usb_disconnects"] = 0.0
        if t - last_ctrl > 0.5:  # control word / FMS info twice a second
            last_ctrl = t
            vals.update({k: v for k, v in state["values"].items() if k.startswith("/DriverStation")})
        state["values"].update(vals)
        for c in list(state["clients"]):
            try:
                c.send_values(vals)
            except OSError:
                pass
        time.sleep(0.02)


if __name__ == "__main__":
    main()
