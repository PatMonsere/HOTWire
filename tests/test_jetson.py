"""Jetson health parsing: USB error counting and the heartbeat view."""

from hotwire.jetson import HeartbeatWatch, count_usb_errors, parse_health

BOOT = [
    "[    2.104512] tegra-xusb 3610000.usb: Firmware timestamp: 2023-02-10",
    "[    3.218004] fusb301 1-0025: fusb301: probe of 1-0025 failed with error -22",
    "[    3.900120] usb 1-2: new high-speed USB device number 3 using tegra-xusb",
    "[    5.402311] usb 2-1: reset SuperSpeed USB device number 2 using tegra-xusb",
]


def test_usb_boot_messages_are_not_errors():
    assert count_usb_errors(BOOT) == (0, "")


def test_usb_real_errors_are_counted():
    lines = BOOT + [
        "[  812.330001] usb 1-2: USB disconnect, device number 3",
        "[  815.004000] usb 1-2: device descriptor read/64, error -71",
        "[  901.100000] usb 2-1: reset SuperSpeed USB device number 2 using tegra-xusb",
        "[  950.000000] usb usb1-port2: over-current condition",
    ]
    n, last = count_usb_errors(lines)
    assert n == 4
    assert "over-current" in last


def test_parse_health_usb():
    text = "usb_ok=1\n" + "\n".join("usbk=" + l for l in BOOT + ["[ 700.0] usb 1-2: USB disconnect, device number 3"])
    h = parse_health(text)
    assert h["usb_errors"] == "1"
    assert "USB disconnect" in h["usb_last"]
    assert parse_health("")["usb_errors"] == "?"


def test_heartbeat_alive_and_stale():
    w = HeartbeatWatch()
    r = w.update(seq_ts_us=10_000_000, now_us=10_400_000, jetson_uptime=100, app_uptime=50, vin=12.0, vin_min=11.9)
    assert (r.alive_text, r.alive_status) == ("alive", "pass")
    r = w.update(seq_ts_us=10_000_000, now_us=15_000_000, jetson_uptime=101, app_uptime=51, vin=12.0, vin_min=11.9)
    assert r.alive_status == "fail" and "5 s" in r.alive_text


def test_heartbeat_dip_and_reboots():
    w = HeartbeatWatch()
    w.update(1, 1, jetson_uptime=500, app_uptime=400, vin=12.2, vin_min=12.1, clock="10:00:00")
    r = w.update(1, 1, jetson_uptime=501, app_uptime=401, vin=12.2, vin_min=9.5, clock="10:00:01")
    assert r.vin_min_status == "warn" and "9.50 V at 10:00:01" in r.vin_min_text
    r = w.update(1, 1, jetson_uptime=3, app_uptime=2, vin=12.2, vin_min=12.1)  # power loss -> reboot
    assert (r.reboots, r.restarts) == (1, 1)
    w.update(1, 1, jetson_uptime=300, app_uptime=290, vin=12.2, vin_min=12.1)
    r = w.update(1, 1, jetson_uptime=310, app_uptime=1, vin=12.2, vin_min=12.1)  # app restart only
    assert (r.reboots, r.restarts) == (1, 2)
    assert r.vin_min_text.startswith("9.50 V")  # lowest is remembered


def test_heartbeat_without_voltage():
    r = HeartbeatWatch().update(1, 1, None, None, None, None)
    assert r.vin_status == "info" and r.vin_min_text == "?"
