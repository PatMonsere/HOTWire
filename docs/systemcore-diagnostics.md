# Systemcore diagnostics server (NetworkTables port 6810)

The Systemcore OS runs its own NetworkTables 4 server on **port 6810**, separate from robot code's server (5810). It runs whether robot code is running or not, so it shows the robot's state and health even when robot code is stopped, crashed or being deployed. AdvantageScope calls it "Systemcore Diagnostics". WPILib's HAL connects to it as the client "RobotProgram".

Read-only tools can subscribe to it like any NT4 server. HOTWire reads the robot state, battery, disk and health from it (`hotwire/systemserver.py`).

This list was captured from the 2026 comp bot on **2026-11-25**: Systemcore OS `limelightosr-2027.0.0-alpha17`, WPILib 2027 alpha-8, robot code (2027_Biocore) running, disabled, in Utility mode. It came from HOTWire's `tools/nt_topics.py --port 6810` (470 topics). Topic names and layouts can change with OS updates, so list them again after updating:

```
cd C:\HOT67\HOTWire
.venv\Scripts\python tools\nt_topics.py --port 6810
```

## The useful topics

### `/sys/`: Systemcore health

| Topic | Type | Value seen | Meaning |
|---|---|---|---|
| `/sys/battery` | double | 11.545 | Battery voltage at the Systemcore's power input (same as `RobotController.getBatteryVoltage()`) |
| `/sys/brownout` | int | 0 | Brownout |
| `/sys/cpu` | double | 15.5 | CPU use, % |
| `/sys/temp` | double | 38.05 | Systemcore temperature, °C |
| `/sys/ram`, `ramAvail`, `ramtotal` | int | 670 MB, 3.57 GB, 4.24 GB | Memory, bytes |
| `/sys/ramutil` | double | 15.8 | Memory use, % |
| `/sys/storage`, `storageavail`, `storagetotal` | int | 2.28 GB, 4.65 GB, 7.33 GB | Disk used / free / total, bytes (logs live here) |
| `/sys/storageutil` | double | 31.2 | Disk use, % |
| `/sys/emmc/status` | string | "good" | eMMC (built-in storage) health |
| `/sys/emmc/lifetime_a`, `lifetime_b`, `pre_eol` | int | 1, 1, 1 | eMMC wear estimates |
| `/sys/faults/canbus_down`, `canbus_unavail` | int | 0 | CAN bus faults now |
| `/sys/faultcounts/canbus_down`, `canbus_unavail` | int | 0 | CAN bus fault counts |
| `/sys/faults/data` | struct:IOFaults | | brownout, io, rsl, usb, display, imu, ... |
| `/sys/faults/emmcstate` | int | 0 | |
| `/sys/iofreq`, `iorxfreq` | int | 995, 497 | IO loop rates (Hz) |
| `/sys/osver`, `oshash`, `osslot` | string | "limelightosr-2027.0.0-alpha17", ..., "A" | OS version, build hash, A/B slot |
| `/sys/team`, `teamnum` | string, int | "67", 67 | Team number |
| `/sys/dsip` | int | 3232253393 | Driver Station IP as a number (192.168.68.209) |
| `/sys/current3v3`, `dsub`, `hsub`, `wifi/display` | | 0, 0, 0, "" | |

### `/Netcomm/`: robot state and the Driver Station link

| Topic | Type | Meaning |
|---|---|---|
| `/Netcomm/Control/ControlData` | proto:mrc.proto.ProtobufControlData | Robot state from the DS: `ControlWord` (field 5, uint32), match time, joysticks, current opmode hash, game data |
| `/Netcomm/Control/MatchInfo` | proto:mrc.proto.ProtobufMatchInfo | Event name, match number and type (empty without the FMS) |
| `/Netcomm/Control/JoystickDescriptors` | proto | Connected controllers ("Xbox One Controller") |
| `/Netcomm/Control/HasSetWallClock` | boolean | The DS has set the Systemcore's clock (it has no clock battery) |
| `/Netcomm/Control/ServerReady`, `WatchdogActive` | boolean | |
| `/Netcomm/Control/RobotPidAllowed`, `RobotPidRequested` | int | |
| `/Netcomm/Status/HasUserCode`, `HasUserCodeReady` | boolean | Robot code is running / ready |
| `/Netcomm/Status/CurrentOpModeTrace` | int | |
| `/Netcomm/OpModeOptions` | proto:ProtobufAvailableOpModes | The opmodes robot code offers (what the DS lists) |
| `/Netcomm/Console/ConsoleLine` | string | The last console line |
| `/Netcomm/Console/ErrorInfo` | proto:ProtobufErrorInfo | The last error or warning (for example, the loop overrun breakdown `robotPeriodic(): 0.287927s`) |
| `/Netcomm/Console/ProgramCrashInfo` | proto | Crash details |
| `/Netcomm/Display/AnsiText` | string | The Systemcore's display text |
| `/Netcomm/Debug/PacketTimeOffset` | int | |
| `/Netcomm/Outputs/JoystickOutput/0`–`5` | proto | Controller rumble and LEDs |
| `/Netcomm/Reporting/Usage/...` | string | What robot code uses: framework (OpModeRobot), language, WPILib version, opmodes, Epilogue, CTRE Swerve, ... |
| `/Netcomm/Reporting/Versions/<bus>/<id>/<device>` | string | Every CTRE device found, by CAN bus and ID |

**`ControlWord` bits** (field 5 of `ControlData`). These were checked against the Driver Station's own logs from 2026-11-04 to 11-25, except bit 4:

| Bit | Meaning |
|---|---|
| 0 | enabled |
| 1–2 | mode: 1 autonomous, 2 teleop, 3 utility |
| 3 | e-stop (`0x802c` was seen after e-stops) |
| 4 | FMS attached: **not seen with a real FMS yet** (same order as robot code's `ControlWord` struct) |
| 5 | Driver Station attached |
| 15 | always set (`0x8000`) |

Seen values: `0x8024` teleop disabled, `0x8025` teleop enabled, `0x8022` / `0x8023` auto, `0x8026` / `0x8027` utility. The protobuf schema is published at `/.schema/proto:MrcComm.proto`, and every Driver Station log has a copy.

### `/Netcomm/Alerts/`: every alert, on or off (about 200 topics)

Each alert has two topics: `/Netcomm/Alerts/<group>/<level>/<name>/active` (int: a timestamp while the alert is on, **0 when off**) and `.../text` (string). Level 0 = error, 1 = warning. Groups seen:

- **Robot code's `Alert`s**: `Alerts/0/Intake/Pivot config`, `Alerts/0/Intake/Roller config` (off), and `Alerts/1/opmode-loop-overrun` (on: "Loop time of "0.02"s overrun").
- **DataLogManager**: `lowSpace` (off), `internalStorage` (on: "Logging to SystemCore internal storage. Attach a USB...").
- **Phoenix 6**, for every device (Talon FX, CANcoder, Pigeon 2): `Apply Configs Error`, `Refresh Configs Error`, `FirmVersCheck`, `Apply Configs Warning`, `Refresh Configs Warning` and `Freq Configs` ("Do not apply or refresh configs periodically"); `IsNetworkUp` for each CANivore and `can_s0`; `Signal Logger Stopped`, `Failed Start` and `Low Storage`. None were on.
- **Telemetry**: `warning0_0`, `warning0_1` (on: the `Robot/scheduler` protobuf bug, see the 2027_Biocore notes).

### `/diagnostics/`: network and CAN interfaces

| Topic | Type | Meaning |
|---|---|---|
| `/diagnostics/networkInterfaces` | string[] | lo, eth0, can0, can1, can_s0–s4, can_d0–d19, usb0, usb1, wlan0, docker0 |
| `/diagnostics/<interface>` | double[] | Counters per interface (bytes / packets / errors ...; layout not worked out yet) |
| `/diagnostics/canbusutil` | double[] | CAN bus use, % per bus (10.4 % on bus 0) |
| `/diagnostics/canbusfps` | double[] | CAN frames per second per bus (736) |
| `/diagnostics/canbuserr`, `canbushealth` | double[] | CAN errors and health per bus |
| `/diagnostics/canbusinfo` | struct:CanBusInfoEntry[] | nominal and data bit rates, sample points per bus |

### Other

- `/imu/data` (struct:IMUData: accel[3], gyro[3], quat[4], pitchFlat, ...), `/imu/reset_ack`, `/imu/yaw_zero_ack`: the Systemcore's built-in IMU.
- `/io/0`–`5/dataget` (struct:IOGet: val, rate, period): the Systemcore's IO ports.
- `/mc/...` (motor controller board: banks, encoders, odometry, ports, firmware, OTA) and `/pd/0`–`1/...` (power distribution frames): no values, since none were attached.
- `/.schema/...`: `proto:MrcComm.proto` and the structs `CanBusInfoEntry`, `IMUData`, `IOFaults`, `IOGet`.

## Full listing

Exactly as `tools/nt_topics.py --port 6810` printed it. Long values are cut off.

```
C:\HOT67\HOTWire>.venv\Scripts\python tools\nt_topics.py --port 6810
470 topic(s) on 10.0.67.2:6810 under /
  /.schema/proto:MrcComm.proto                                 proto:FileDescriptorProto b'\n\rMrcComm.proto\x12\tmrc.proto"D\n\x12ProtobufFingerData\x12\x0c\n
  /.schema/struct:CanBusInfoEntry                              structschema             b'double nominalMbps;double dataMbps;double samplePoint;double dataSam
  /.schema/struct:IMUData                                      structschema             b'double accel[3];double gyro[3];double quat[4];double pitchFlat;doubl
  /.schema/struct:IOFaults                                     structschema             b'bool brownout;bool io;bool rsl;bool usb;bool display;bool imu;int32
  /.schema/struct:IOGet                                        structschema             b'int32 val;int32 rate;int32 period'
  /Netcomm/Alerts/Alerts/0/Intake/Pivot config/active          int                      0
  /Netcomm/Alerts/Alerts/0/Intake/Pivot config/text            string                   'Intake/Pivot: configuration failed'
  /Netcomm/Alerts/Alerts/0/Intake/Roller config/active         int                      0
  /Netcomm/Alerts/Alerts/0/Intake/Roller config/text           string                   'Intake/Roller: configuration failed'
  /Netcomm/Alerts/Alerts/1/opmode-loop-overrun/active          int                      209358451
  /Netcomm/Alerts/Alerts/1/opmode-loop-overrun/text            string                   'Loop time of "0.02"s overrun'
  /Netcomm/Alerts/DataLogManager/1/lowSpace/active             int                      0
  /Netcomm/Alerts/DataLogManager/1/lowSpace/text               string                   ''
  /Netcomm/Alerts/DataLogManager/2/internalStorage/active      int                      207981838
  /Netcomm/Alerts/DataLogManager/2/internalStorage/text        string                   'DataLogManager: Logging to SystemCore internal storage. Attach a USB
  /Netcomm/Alerts/Phoenix 6/0/999A095946324B532020204A101312FF IsNetworkUp/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/999A095946324B532020204A101312FF IsNetworkUp/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/C8BD62D0463847532020204B120C0DFF IsNetworkUp/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/C8BD62D0463847532020204B120C0DFF IsNetworkUp/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 40 ("DT") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 40 ("DT") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 40 ("DT") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 40 ("DT") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 40 ("DT") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 40 ("DT") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 41 ("DT") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 41 ("DT") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 41 ("DT") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 41 ("DT") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 41 ("DT") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 41 ("DT") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 42 ("DT") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 42 ("DT") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 42 ("DT") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 42 ("DT") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 42 ("DT") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 42 ("DT") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 43 ("DT") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 43 ("DT") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 43 ("DT") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 43 ("DT") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 43 ("DT") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 43 ("DT") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 47 ("RF") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 47 ("RF") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 47 ("RF") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 47 ("RF") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 47 ("RF") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/CANcoder 47 ("RF") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Pigeon 2 50 ("DT") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Pigeon 2 50 ("DT") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Pigeon 2 50 ("DT") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Pigeon 2 50 ("DT") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Pigeon 2 50 ("DT") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Pigeon 2 50 ("DT") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Signal Logger Stopped/active     int                      0
  /Netcomm/Alerts/Phoenix 6/0/Signal Logger Stopped/text       string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 1 ("DT") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 1 ("DT") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 1 ("DT") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 1 ("DT") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 1 ("DT") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 1 ("DT") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 16 ("RF") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 16 ("RF") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 16 ("RF") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 16 ("RF") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 16 ("RF") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 16 ("RF") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 17 ("can_s0") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 17 ("can_s0") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 17 ("can_s0") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 17 ("can_s0") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 17 ("can_s0") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 17 ("can_s0") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 2 ("DT") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 2 ("DT") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 2 ("DT") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 2 ("DT") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 2 ("DT") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 2 ("DT") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 3 ("DT") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 3 ("DT") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 3 ("DT") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 3 ("DT") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 3 ("DT") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 3 ("DT") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 4 ("DT") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 4 ("DT") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 4 ("DT") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 4 ("DT") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 4 ("DT") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 4 ("DT") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 5 ("DT") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 5 ("DT") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 5 ("DT") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 5 ("DT") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 5 ("DT") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 5 ("DT") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 6 ("DT") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 6 ("DT") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 6 ("DT") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 6 ("DT") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 6 ("DT") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 6 ("DT") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 7 ("DT") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 7 ("DT") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 7 ("DT") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 7 ("DT") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 7 ("DT") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 7 ("DT") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 8 ("DT") Apply Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 8 ("DT") Apply Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 8 ("DT") FirmVersCheck/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 8 ("DT") FirmVersCheck/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 8 ("DT") Refresh Configs Error/active int                      0
  /Netcomm/Alerts/Phoenix 6/0/Talon FX 8 ("DT") Refresh Configs Error/text string                   ''
  /Netcomm/Alerts/Phoenix 6/0/can_s0 IsNetworkUp/active        int                      0
  /Netcomm/Alerts/Phoenix 6/0/can_s0 IsNetworkUp/text          string                   ''
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 40 ("DT") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 40 ("DT") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 40 ("DT") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 40 ("DT") Freq Configs/text string                   'CANcoder 40 ("DT") Configs: Do not apply or refresh configs periodica
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 40 ("DT") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 40 ("DT") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 41 ("DT") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 41 ("DT") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 41 ("DT") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 41 ("DT") Freq Configs/text string                   'CANcoder 41 ("DT") Configs: Do not apply or refresh configs periodica
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 41 ("DT") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 41 ("DT") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 42 ("DT") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 42 ("DT") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 42 ("DT") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 42 ("DT") Freq Configs/text string                   'CANcoder 42 ("DT") Configs: Do not apply or refresh configs periodica
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 42 ("DT") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 42 ("DT") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 43 ("DT") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 43 ("DT") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 43 ("DT") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 43 ("DT") Freq Configs/text string                   'CANcoder 43 ("DT") Configs: Do not apply or refresh configs periodica
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 43 ("DT") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 43 ("DT") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 47 ("RF") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 47 ("RF") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 47 ("RF") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 47 ("RF") Freq Configs/text string                   'CANcoder 47 ("RF") Configs: Do not apply or refresh configs periodica
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 47 ("RF") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/CANcoder 47 ("RF") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Pigeon 2 50 ("DT") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Pigeon 2 50 ("DT") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Pigeon 2 50 ("DT") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Pigeon 2 50 ("DT") Freq Configs/text string                   'Pigeon 2 50 ("DT") Configs: Do not apply or refresh configs periodica
  /Netcomm/Alerts/Phoenix 6/1/Pigeon 2 50 ("DT") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Pigeon 2 50 ("DT") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Signal Logger Failed Start/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Signal Logger Failed Start/text  string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Signal Logger Low Storage/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Signal Logger Low Storage/text   string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 1 ("DT") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 1 ("DT") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 1 ("DT") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 1 ("DT") Freq Configs/text string                   'Talon FX 1 ("DT") Configs: Do not apply or refresh configs periodical
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 1 ("DT") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 1 ("DT") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 16 ("RF") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 16 ("RF") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 16 ("RF") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 16 ("RF") Freq Configs/text string                   'Talon FX 16 ("RF") Configs: Do not apply or refresh configs periodica
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 16 ("RF") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 16 ("RF") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 17 ("can_s0") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 17 ("can_s0") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 17 ("can_s0") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 17 ("can_s0") Freq Configs/text string                   'Talon FX 17 ("can_s0") Configs: Do not apply or refresh configs perio
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 17 ("can_s0") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 17 ("can_s0") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 2 ("DT") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 2 ("DT") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 2 ("DT") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 2 ("DT") Freq Configs/text string                   'Talon FX 2 ("DT") Configs: Do not apply or refresh configs periodical
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 2 ("DT") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 2 ("DT") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 3 ("DT") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 3 ("DT") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 3 ("DT") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 3 ("DT") Freq Configs/text string                   'Talon FX 3 ("DT") Configs: Do not apply or refresh configs periodical
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 3 ("DT") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 3 ("DT") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 4 ("DT") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 4 ("DT") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 4 ("DT") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 4 ("DT") Freq Configs/text string                   'Talon FX 4 ("DT") Configs: Do not apply or refresh configs periodical
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 4 ("DT") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 4 ("DT") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 5 ("DT") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 5 ("DT") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 5 ("DT") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 5 ("DT") Freq Configs/text string                   'Talon FX 5 ("DT") Configs: Do not apply or refresh configs periodical
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 5 ("DT") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 5 ("DT") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 6 ("DT") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 6 ("DT") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 6 ("DT") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 6 ("DT") Freq Configs/text string                   'Talon FX 6 ("DT") Configs: Do not apply or refresh configs periodical
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 6 ("DT") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 6 ("DT") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 7 ("DT") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 7 ("DT") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 7 ("DT") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 7 ("DT") Freq Configs/text string                   'Talon FX 7 ("DT") Configs: Do not apply or refresh configs periodical
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 7 ("DT") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 7 ("DT") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 8 ("DT") Apply Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 8 ("DT") Apply Configs Warning/text string                   ''
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 8 ("DT") Freq Configs/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 8 ("DT") Freq Configs/text string                   'Talon FX 8 ("DT") Configs: Do not apply or refresh configs periodical
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 8 ("DT") Refresh Configs Warning/active int                      0
  /Netcomm/Alerts/Phoenix 6/1/Talon FX 8 ("DT") Refresh Configs Warning/text string                   ''
  /Netcomm/Alerts/Telemetry/1/warning0_0/active                int                      627538932
  /Netcomm/Alerts/Telemetry/1/warning0_0/text                  string                   "Telemetry '/Robot/scheduler': warning: failed to publish protobuf val
  /Netcomm/Alerts/Telemetry/1/warning0_1/active                int                      669915276
  /Netcomm/Alerts/Telemetry/1/warning0_1/text                  string                   "Telemetry '/Robot/scheduler': warning: failed to publish protobuf val
  /Netcomm/Console/ConsoleLine                                 string                   None
  /Netcomm/Console/ErrorInfo                                   proto:mrc.proto.ProtobufErrorInfo b'\x10\x02\x1aw\trobotPeriodic(): 0.287927s\n\tdisabledExit(): 0.00007
  /Netcomm/Console/ProgramCrashInfo                            proto:mrc.proto.ProtobufProgramCrashInfo None
  /Netcomm/Control/ControlData                                 proto:mrc.proto.ProtobufControlData b'\x1a\x0e\x08\xff\xff\x01\x18?"\x06\x00\x01\x00\x01\x00\x00!\x00\x00\
  /Netcomm/Control/HasSetWallClock                             boolean                  True
  /Netcomm/Control/JoystickDescriptors                         proto:mrc.proto.ProtobufJoystickDescriptors b'\n\x1b\n\x13Xbox One Controller\x10\x01\x18\x03 \x08'
  /Netcomm/Control/MatchInfo                                   proto:mrc.proto.ProtobufMatchInfo None
  /Netcomm/Control/RobotPidAllowed                             int                      1833
  /Netcomm/Control/RobotPidRequested                           int                      1833
  /Netcomm/Control/ServerReady                                 boolean                  True
  /Netcomm/Control/WatchdogActive                              boolean                  False
  /Netcomm/Debug/PacketTimeOffset                              int                      1175
  /Netcomm/Display/AnsiText                                    string                   None
  /Netcomm/OpModeOptions                                       proto:mrc.proto.ProtobufAvailableOpModes b'\n+\t+\xfb\x18\x95\xff\xff\xff\x02\x12\x06Teleop\x1a\x00"\x00(\xff\x
  /Netcomm/Outputs/JoystickOutput/0                            proto:mrc.proto.ProtobufJoystickOutput None
  /Netcomm/Outputs/JoystickOutput/1                            proto:mrc.proto.ProtobufJoystickOutput None
  /Netcomm/Outputs/JoystickOutput/2                            proto:mrc.proto.ProtobufJoystickOutput None
  /Netcomm/Outputs/JoystickOutput/3                            proto:mrc.proto.ProtobufJoystickOutput None
  /Netcomm/Outputs/JoystickOutput/4                            proto:mrc.proto.ProtobufJoystickOutput None
  /Netcomm/Outputs/JoystickOutput/5                            proto:mrc.proto.ProtobufJoystickOutput None
  /Netcomm/Reporting/Usage/CTRE Swerve                         string                   ''
  /Netcomm/Reporting/Usage/DataLogManager                      string                   'Onboard'
  /Netcomm/Reporting/Usage/Epilogue                            string                   'update'
  /Netcomm/Reporting/Usage/Framework                           string                   'OpModeRobot'
  /Netcomm/Reporting/Usage/HID[0]                              string                   'Gamepad'
  /Netcomm/Reporting/Usage/HID[1]                              string                   'Gamepad'
  /Netcomm/Reporting/Usage/Language                            string                   'Java'
  /Netcomm/Reporting/Usage/NT/HOTVision@2                      string                   ''
  /Netcomm/Reporting/Usage/OpMode/AUTONOMOUS                   string                   '1'
  /Netcomm/Reporting/Usage/OpMode/TELEOPERATED                 string                   '1'
  /Netcomm/Reporting/Usage/OpMode/UNKNOWN                      string                   '0'
  /Netcomm/Reporting/Usage/OpMode/UTILITY                      string                   '1'
  /Netcomm/Reporting/Usage/SwerveDriveKinematics               string                   ''
  /Netcomm/Reporting/Usage/WPILibVersion                       string                   '2027.0.0-alpha-8'
  /Netcomm/Reporting/Versions/0/18/Talon FX                    string                   ''
  /Netcomm/Reporting/Versions/25/0/CANivore                    string                   ''
  /Netcomm/Reporting/Versions/25/2/Talon FX                    string                   ''
  /Netcomm/Reporting/Versions/25/3/Talon FX                    string                   ''
  /Netcomm/Reporting/Versions/25/4/Talon FX                    string                   ''
  /Netcomm/Reporting/Versions/25/41/CANcoder                   string                   ''
  /Netcomm/Reporting/Versions/25/42/CANcoder                   string                   ''
  /Netcomm/Reporting/Versions/25/43/CANcoder                   string                   ''
  /Netcomm/Reporting/Versions/25/44/CANcoder                   string                   ''
  /Netcomm/Reporting/Versions/25/5/Talon FX                    string                   ''
  /Netcomm/Reporting/Versions/25/51/Pigeon 2                   string                   ''
  /Netcomm/Reporting/Versions/25/6/Talon FX                    string                   ''
  /Netcomm/Reporting/Versions/25/7/Talon FX                    string                   ''
  /Netcomm/Reporting/Versions/25/8/Talon FX                    string                   ''
  /Netcomm/Reporting/Versions/25/9/Talon FX                    string                   ''
  /Netcomm/Reporting/Versions/26/0/CANivore                    string                   ''
  /Netcomm/Reporting/Versions/26/17/Talon FX                   string                   ''
  /Netcomm/Reporting/Versions/26/48/CANcoder                   string                   ''
  /Netcomm/Reporting/Versions/31/0/WPILib                      string                   '2027.0.0-alpha-8 (Java)'
  /Netcomm/Status/CurrentOpModeTrace                           int                      216172782113783808
  /Netcomm/Status/HasUserCode                                  boolean                  True
  /Netcomm/Status/HasUserCodeReady                             boolean                  True
  /diagnostics/can0                                            double[]                 [49841884.0, 362716.0, 957405.0, 45597.0, 0.0, 0.0, 0.0, 0.0, 422.336,
  /diagnostics/can1                                            double[]                 [150775134.0, 5605292.0, 2729615.0, 701002.0, 0.0, 0.0, 0.0, 0.0, 1273
  /diagnostics/can_d0                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d1                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d10                                         double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d11                                         double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d12                                         double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d13                                         double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d14                                         double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d15                                         double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d16                                         double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d17                                         double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d18                                         double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d19                                         double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d2                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d3                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d4                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d5                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d6                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d7                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d8                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_d9                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/can_s0                                          double[]                 [4991140.0, 286448.0, 626915.0, 73706.0, 0.0, 0.0, 0.0, 0.0, 41.28, 2.
  /diagnostics/can_s1                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, -1.0, 0.0, 1.0
  /diagnostics/can_s2                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, -1.0, 0.0, 1.0
  /diagnostics/can_s3                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, -1.0, 0.0, 1.0
  /diagnostics/can_s4                                          double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, -1.0, 0.0, 1.0
  /diagnostics/canbuserr                                       double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 40.05293178838012, 0.0,
  /diagnostics/canbusfps                                       double[]                 [736.3408269418394, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/canbushealth                                    double[]                 [0.0, 0.0, 0.0, 0.0, 0.0]
  /diagnostics/canbusinfo                                      struct:CanBusInfoEntry[] b'\x00\x00\x00\x00\x00\x00\xf0?\x00\x00\x00\x00\x00\x00\xf0\xbf\x00\x0
  /diagnostics/canbusutil                                      double[]                 [10.398742243116265, 0.10398742243116266, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
  /diagnostics/docker0                                         double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 74.0, 0.0, 0.0]
  /diagnostics/eth0                                            double[]                 [45552425.0, 318426032.0, 396288.0, 333479.0, 0.0, 0.0, 0.0, 0.0, 257.
  /diagnostics/lo                                              double[]                 [351598473.0, 351598473.0, 2026137.0, 2026137.0, 0.0, 0.0, 0.0, 0.0, 3
  /diagnostics/networkInterfaces                               string[]                 ['lo', 'eth0', 'can0', 'can1', 'can_s4', 'can_s3', 'can_s2', 'can_s1',
  /diagnostics/usb0                                            double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 82.0, 0.0, 0.0]
  /diagnostics/usb1                                            double[]                 [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 82.0, 0.0, 0.0]
  /diagnostics/wlan0                                           double[]                 [0.0, 8977.0, 0.0, 77.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  /imu/data                                                    struct:IMUData           b'\x00\x00\x00\xe0a\xbd\x91?\x00\x00\x00\xe0a\xbd\xa1?\x00\x00\x00\xe0
  /imu/reset_ack                                               int                      None
  /imu/yaw_zero_ack                                            int                      None
  /io/0/dataget                                                struct:IOGet             b'\x00\x00\x00\x00\x00\x00\x00\x00 N\x00\x00'
  /io/1/dataget                                                struct:IOGet             b'\x00\x00\x00\x00\x00\x00\x00\x00 N\x00\x00'
  /io/2/dataget                                                struct:IOGet             b'\x00\x00\x00\x00\x00\x00\x00\x00 N\x00\x00'
  /io/3/dataget                                                struct:IOGet             b'\x00\x00\x00\x00\x00\x00\x00\x00 N\x00\x00'
  /io/4/dataget                                                struct:IOGet             b'\x00\x00\x00\x00\x00\x00\x00\x00 N\x00\x00'
  /io/5/dataget                                                struct:IOGet             b'\x00\x00\x00\x00\x00\x00\x00\x00 N\x00\x00'
  /mc/banks/0/current_ma                                       int                      None
  /mc/banks/0/enabled                                          boolean                  None
  /mc/banks/0/overcurrent_fast_fault                           boolean                  None
  /mc/banks/0/overcurrent_slow_fault                           boolean                  None
  /mc/banks/0/temp                                             double                   None
  /mc/banks/1/current_ma                                       int                      None
  /mc/banks/1/enabled                                          boolean                  None
  /mc/banks/1/overcurrent_fast_fault                           boolean                  None
  /mc/banks/1/overcurrent_slow_fault                           boolean                  None
  /mc/banks/1/temp                                             double                   None
  /mc/banks/2/current_ma                                       int                      None
  /mc/banks/2/enabled                                          boolean                  None
  /mc/banks/2/overcurrent_fast_fault                           boolean                  None
  /mc/banks/2/overcurrent_slow_fault                           boolean                  None
  /mc/banks/2/temp                                             double                   None
  /mc/banks/3/current_ma                                       int                      None
  /mc/banks/3/enabled                                          boolean                  None
  /mc/banks/3/overcurrent_fast_fault                           boolean                  None
  /mc/banks/3/overcurrent_slow_fault                           boolean                  None
  /mc/banks/3/temp                                             double                   None
  /mc/banks/4/current_ma                                       int                      None
  /mc/banks/4/enabled                                          boolean                  None
  /mc/banks/4/overcurrent_fast_fault                           boolean                  None
  /mc/banks/4/overcurrent_slow_fault                           boolean                  None
  /mc/banks/4/temp                                             double                   None
  /mc/board_temp                                               double                   None
  /mc/boot/count                                               int                      None
  /mc/boot/flags                                               int                      None
  /mc/boot/last_stage_c0                                       int                      None
  /mc/boot/last_stage_c1                                       int                      None
  /mc/canfd_active                                             boolean                  False
  /mc/current_limit/amps                                       int                      None
  /mc/current_limit/secs                                       int                      None
  /mc/enc0/a                                                   boolean                  None
  /mc/enc0/b                                                   boolean                  None
  /mc/enc0/pos                                                 int                      None
  /mc/enc0/vel                                                 int                      None
  /mc/enc0/window                                              int                      None
  /mc/enc1/a                                                   boolean                  None
  /mc/enc1/b                                                   boolean                  None
  /mc/enc1/pos                                                 int                      None
  /mc/enc1/vel                                                 int                      None
  /mc/enc1/window                                              int                      None
  /mc/enc2/a                                                   boolean                  None
  /mc/enc2/b                                                   boolean                  None
  /mc/enc2/pos                                                 int                      None
  /mc/enc2/vel                                                 int                      None
  /mc/enc2/window                                              int                      None
  /mc/faults/encoder_power                                     boolean                  None
  /mc/faults/overtemp                                          boolean                  None
  /mc/fd/status                                                struct:MCPortStatus      None
  /mc/firmware/major                                           int                      None
  /mc/firmware/minor                                           int                      None
  /mc/firmware/patch                                           int                      None
  /mc/host_bridge_rx_rate                                      int                      0
  /mc/host_bridge_tx_rate                                      int                      0
  /mc/host_rx_dropped                                          int                      0
  /mc/host_rx_errors                                           int                      1
  /mc/host_rx_packed                                           int                      0
  /mc/host_tx_dropped                                          int                      0
  /mc/host_tx_errors                                           int                      0
  /mc/host_tx_packed                                           int                      0
  /mc/odom/freq                                                int                      0
  /mc/odom/theta                                               double                   0.0
  /mc/odom/vtheta                                              double                   0.0
  /mc/odom/vx                                                  double                   0.0
  /mc/odom/vy                                                  double                   0.0
  /mc/odom/x                                                   double                   0.0
  /mc/odom/y                                                   double                   0.0
  /mc/ota/attempted_flash                                      boolean                  False
  /mc/ota/progress                                             double                   -1.0
  /mc/ota/successful_flash                                     boolean                  False
  /mc/perf/status                                              struct:MCPerf            None
  /mc/perf/timing                                              struct:MCPerfTiming      None
  /mc/port0/status                                             struct:MCPortStatus      None
  /mc/port1/status                                             struct:MCPortStatus      None
  /mc/port10/status                                            struct:MCPortStatus      None
  /mc/port11/status                                            struct:MCPortStatus      None
  /mc/port12/status                                            struct:MCPortStatus      None
  /mc/port13/status                                            struct:MCPortStatus      None
  /mc/port14/status                                            struct:MCPortStatus      None
  /mc/port15/status                                            struct:MCPortStatus      None
  /mc/port16/status                                            struct:MCPortStatus      None
  /mc/port17/status                                            struct:MCPortStatus      None
  /mc/port18/status                                            struct:MCPortStatus      None
  /mc/port19/status                                            struct:MCPortStatus      None
  /mc/port2/status                                             struct:MCPortStatus      None
  /mc/port3/status                                             struct:MCPortStatus      None
  /mc/port4/status                                             struct:MCPortStatus      None
  /mc/port5/status                                             struct:MCPortStatus      None
  /mc/port6/status                                             struct:MCPortStatus      None
  /mc/port7/status                                             struct:MCPortStatus      None
  /mc/port8/status                                             struct:MCPortStatus      None
  /mc/port9/status                                             struct:MCPortStatus      None
  /mc/rx_frames                                                int                      0
  /mc/temp_limit/celsius                                       int                      None
  /mc/tx_frames                                                int                      0
  /mc/uptime_s                                                 int                      953
  /mc/vin_mv                                                   int                      None
  /pd/0/deviceid                                               int                      None
  /pd/0/frame0                                                 pd                       None
  /pd/0/frame1                                                 pd                       None
  /pd/0/frame2                                                 pd                       None
  /pd/0/frame3                                                 pd                       None
  /pd/1/deviceid                                               int                      None
  /pd/1/frame0                                                 pd                       None
  /pd/1/frame1                                                 pd                       None
  /pd/1/frame2                                                 pd                       None
  /pd/1/frame3                                                 pd                       None
  /sys/battery                                                 double                   11.545000076293945
  /sys/brownout                                                int                      0
  /sys/cpu                                                     double                   15.461349487304688
  /sys/current3v3                                              double                   0.0
  /sys/dsip                                                    int                      3232253393
  /sys/dsub                                                    int                      0
  /sys/emmc/lifetime_a                                         int                      1
  /sys/emmc/lifetime_b                                         int                      1
  /sys/emmc/pre_eol                                            int                      1
  /sys/emmc/status                                             string                   'good'
  /sys/faultcounts/canbus_down                                 int                      0
  /sys/faultcounts/canbus_unavail                              int                      0
  /sys/faults/canbus_down                                      int                      0
  /sys/faults/canbus_unavail                                   int                      0
  /sys/faults/data                                             struct:IOFaults          b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00
  /sys/faults/emmcstate                                        int                      0
  /sys/hsub                                                    int                      0
  /sys/iofreq                                                  int                      995
  /sys/iorxfreq                                                int                      497
  /sys/oshash                                                  string                   '6f06cbf963735355dacdd300b536b610d30f5a7f'
  /sys/osslot                                                  string                   'A'
  /sys/osver                                                   string                   'limelightosr-2027.0.0-alpha17'
  /sys/ram                                                     int                      670138368
  /sys/ramAvail                                                int                      3574136832
  /sys/ramtotal                                                int                      4244275200
  /sys/ramutil                                                 double                   15.789228439331055
  /sys/storage                                                 int                      2284244992
  /sys/storageavail                                            int                      4649603072
  /sys/storagetotal                                            int                      7326429184
  /sys/storageutil                                             double                   31.178148790252468
  /sys/team                                                    string                   '67'
  /sys/teamnum                                                 int                      67
  /sys/temp                                                    double                   38.04999923706055
  /sys/wifi/display                                            string                   ''```
