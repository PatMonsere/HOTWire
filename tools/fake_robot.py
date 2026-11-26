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
    "/HOT/Indexer/RightKick/IndexerRightKickVelocity": "double",
    "/HOT/Indexer/RightKick/IndexerRightKickSupply Current": "double",
    "/HOT/Shooter/Left Top/ShooterLeft TopVelocity": "double",
    "/HOT/Shooter/Left Top/ShooterLeft TopSupply Current": "double",
    "/HOT/Shooter/Left Bottom/ShooterLeft BottomVelocity": "double",
    "/HOT/Shooter/Left Bottom/ShooterLeft BottomSupply Current": "double",
    "/HOT/Shooter/Right Top/ShooterRight TopVelocity": "double",
    "/HOT/Shooter/Right Top/ShooterRight TopSupply Current": "double",
    "/HOT/Shooter/Hood/ShooterHoodPosition": "double",
    "/HOT/Shooter/Hood/ShooterHoodSupply Current": "double",
    "/HOT/Drive/TotalCurrent": "double",
    "/HOT/Robot/BatteryVoltage": "double",
    "/HOT/Pit/Step": "string",  # the 2027 robot code's pit step name
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


def track(c: float, steps: list[tuple[float, float]], tau: float, v0: float = 0.0) -> float:
    """First-order response to a list of (time, setpoint) changes, starting at v0."""
    v, t_prev, target = v0, None, v0
    for ts, new in steps:
        if ts > c:
            break
        if t_prev is not None:
            v = target + (v - target) * math.exp(-(ts - t_prev) / tau)
        t_prev, target = ts, new
    if t_prev is None:
        return v0
    return target + (v - target) * math.exp(-(c - t_prev) / tau)


def level(c: float, steps: list[tuple[float, float]], v0: float = 0.0) -> float:
    """The latest (time, value) at or before c."""
    out = v0
    for ts, val in steps:
        if ts <= c:
            out = val
    return out


def spikes(c: float, events: list[tuple[float, float]], tau: float = 0.15) -> float:
    """Start-up current spikes that decay."""
    return sum(peak * math.exp(-(c - ts) / tau) for ts, peak in events if c >= ts)


# One cycle of the robot's pit test routine (Rebuilt_Systemcore getPitTestRoutine), with the
# values measured on the real robot on 2026-11-24. Times are when the driver pressed 'back'.
EXTEND, AGITATE, RETRACT = 2.0, 4.5, 7.0
INDEX_ON, INDEX_OFF = 9.0, 12.0
BATTER, CLIMB_POLE, PASSING, HOOD_DOWN = 13.0, 15.5, 17.5, 19.5
LOAD, FIRE, FIRE_END = 24.0, 27.0, 29.3
DRIVE, DRIVE_END = 31.0, 34.0
CYCLE = 36.0


def pit_signals(t: float) -> dict:
    """A 36 s pit test cycle: intake 2-9, indexer 9-12, shooter presets 13-21, load 24, fire 27, drive 31-34."""
    c = t % CYCLE
    v = {}
    # intake: retracted 0.61, extended 0.065; extend() also spins the rollers
    pos_steps = [(EXTEND, 0.065), (AGITATE, 0.44), (RETRACT, 0.61), (LOAD, 0.065), (FIRE_END, 0.61)]
    v["/HOT/Intake/Extend/IntakeExtendPosition"] = track(c, pos_steps, 0.15, 0.61)
    v["/HOT/Intake/Extend/IntakeExtendSupply Current"] = 0.2 + spikes(
        c, [(EXTEND, 50), (AGITATE, 70), (RETRACT, 35), (LOAD, 50), (FIRE_END, 35)]
    )
    roller_steps = [(EXTEND, 115), (RETRACT, 0), (LOAD, 115), (FIRE, 105), (FIRE_END, 0)]
    v["/HOT/Intake/Roller/IntakeRollerVelocity"] = track(c, roller_steps, 0.2)
    rolling = level(c, roller_steps) > 0
    v["/HOT/Intake/Roller/IntakeRollerSupply Current"] = (8.0 if rolling else 0.0) + spikes(c, [(EXTEND, 40), (LOAD, 40)])
    # indexer: kickers start 0.1 s before the floor; runFloorSlow runs both slowly backward
    floor_steps = [(INDEX_ON + 0.1, 70), (INDEX_OFF, 0), (LOAD, -7), (FIRE + 0.1, 70), (FIRE_END, 0)]
    kick_steps = [(INDEX_ON, 85), (INDEX_OFF, 0), (LOAD, -9), (FIRE, 85), (FIRE_END, 0)]
    v["/HOT/Indexer/Floor/IndexerFloorVelocity"] = track(c, floor_steps, 0.1)
    on = level(c, floor_steps) > 0
    v["/HOT/Indexer/Floor/IndexerFloorSupply Current"] = (13.0 if on else 0.0) + spikes(
        c, [(INDEX_ON + 0.1, 65), (FIRE + 0.1, 55)]
    )
    for k in ("LeftKick", "RightKick"):
        v[f"/HOT/Indexer/{k}/Indexer{k}Velocity"] = track(c, kick_steps, 0.1)
        v[f"/HOT/Indexer/{k}/Indexer{k}Supply Current"] = (9.0 if level(c, kick_steps) > 0 else 0.0) + spikes(
            c, [(INDEX_ON, 80), (FIRE, 70)]
        )
    # shooter: Left Top publishes only 0 (like the real robot on 2026-11-24); stop() lets the wheels coast
    shot_steps = [(BATTER, 39), (CLIMB_POLE, 41.2), (PASSING, 71), (FIRE, 39)]
    if HOOD_DOWN <= c < FIRE:
        sv = track(HOOD_DOWN, shot_steps, 0.25) * math.exp(-(c - HOOD_DOWN) / 1.5)
    elif c >= FIRE_END:
        sv = track(FIRE_END, shot_steps, 0.25) * math.exp(-(c - FIRE_END) / 1.5)
    else:
        sv = track(c, shot_steps, 0.25) if c >= BATTER else 0.0
    if FIRE <= c < FIRE_END:
        sv = track(c, [(FIRE, 39)], 0.12, 0.0)
    driving = BATTER <= c < HOOD_DOWN or FIRE <= c < FIRE_END
    amps = (3.3 if sv < 50 else 7.0) if driving else 0.0
    amps += spikes(c, [(BATTER, 28), (PASSING, 50), (FIRE, 28)], 0.2)
    v["/HOT/Shooter/Left Top/ShooterLeft TopVelocity"] = 0.0
    v["/HOT/Shooter/Left Top/ShooterLeft TopSupply Current"] = 0.0
    for k in ("Left Bottom", "Right Top"):
        v[f"/HOT/Shooter/{k}/Shooter{k}Velocity"] = sv
        v[f"/HOT/Shooter/{k}/Shooter{k}Supply Current"] = amps
    hood_steps = [(BATTER, 3.61), (CLIMB_POLE, 4.91), (PASSING, 9.55), (HOOD_DOWN, -0.08), (FIRE, 3.61), (DRIVE_END, -0.08)]
    v["/HOT/Shooter/Hood/ShooterHoodPosition"] = track(c, hood_steps, 0.05)
    v["/HOT/Shooter/Hood/ShooterHoodSupply Current"] = 0.04 + spikes(
        c, [(ts, 20) for ts, _ in hood_steps], 0.05
    )
    dr = DRIVE <= c < DRIVE_END
    v["/HOT/Drive/TotalCurrent"] = (47 * math.exp(-(c - DRIVE) / 0.3) + 11 + 2 * math.sin(c * 7)) if dr else 0.5
    v["/HOT/Robot/BatteryVoltage"] = 12.6 - 0.012 * v["/HOT/Drive/TotalCurrent"]  # sag under load
    v["/HOT/Pit/Step"] = pit_step(c)
    return v


PIT_STEPS = [(EXTEND, "Intake"), (INDEX_ON, "Indexer"), (BATTER, "Shooter presets"), (LOAD, "Load"),
             (FIRE, "Fire"), (DRIVE, "Drive")]  # fmt: skip


def pit_step(c: float) -> str:
    """Like the 2027 robot's HOT/Pit/Step: "2/6 Indexer (running)"."""
    n = sum(1 for ts, _ in PIT_STEPS if c >= ts)
    if n == 0:
        return "press d-pad right to start"
    return f"{n}/{len(PIT_STEPS)} {PIT_STEPS[n - 1][1]} (running)"


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
