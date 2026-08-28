#!/usr/bin/env python3
"""Write tests, run in one attended session against the real device.

Always run tools/session.py --snapshot first and --restore after (this script does
not restore anything itself). Writes stay within +/-1.0 C of the setpoint measured
at the start of this script. That bound is not negotiable: this actuates real
floor heating.

Usage:
    uv run --project tests/lib python tools/w_tests.py --mac AA:BB:CC:DD:EE:FF --psk-file psk.txt
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import re
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "eb300_ble"))

from eb300_ble.client import BleakTransport, EB300Client
from eb300_ble.const import PID, KeyLock, Program


def _load_psk(psk_file: str | None, psk_b64: str | None) -> bytes:
    if psk_b64:
        raw = psk_b64
    elif psk_file:
        text = Path(psk_file).read_text()
        match = re.search(r"base64:\s*(\S+)", text)
        raw = match.group(1) if match else text.strip()
    else:
        raise SystemExit("Provide --psk-file or --psk-b64")
    psk = base64.b64decode(raw)
    if len(psk) != 32:
        raise SystemExit(f"Decoded PSK is {len(psk)} bytes, expected exactly 32")
    return psk


def _header(test_id: str, title: str) -> None:
    print()
    print(f"=== {test_id}: {title} ===")


async def _pause(seconds: float, note: str) -> None:
    print(f"  ...waiting {seconds:.0f}s ({note})...")
    await asyncio.sleep(seconds)


async def run(mac: str, psk: bytes, timeout: float) -> None:
    transport = BleakTransport(mac, timeout=timeout)
    client = EB300Client(transport, psk, request_timeout=timeout)
    await client.connect()
    try:
        status = await client.read_status()
        base_setpoint_c = status.current_set_temperature_c
        print(f"Baseline: setpoint={base_setpoint_c:.1f}C program={status.program.name} "
              f"power_off={status.power_off} relay_on={status.relay_on}")

        # W-1: Override = baseline + 1.0C
        _header("W-1", "Override 0x10D0 = baseline +1.0C")
        target_decideg = round((base_setpoint_c + 1.0) * 10)
        t0 = time.monotonic()
        await client.set_override_temp(target_decideg)
        print(f"  SET override -> {target_decideg / 10:.1f}C  (error 0 = no exception raised)")
        await _pause(3, "let device apply + display refresh")
        status = await client.read_status()
        elapsed = time.monotonic() - t0
        print(f"  GET 0x1004 after {elapsed:.1f}s: set temp={status.current_set_temperature_c:.1f}C "
              f"(expect {target_decideg / 10:.1f}C)")
        print("  >>> Check physical display now: does it show the new setpoint? <<<")
        await _pause(3, "time to glance at display")

        # W-2: Manual temp 0x1082 in Manual mode, sticks across reconnect
        _header("W-2", "Manual temp 0x1082 in Manual mode, check it sticks across reconnect")
        if status.program != Program.MANUAL:
            print("  Not in MANUAL, switching first")
            await client.set_program(Program.MANUAL)
            await _pause(2, "program switch to take")
        manual_target_decideg = round((base_setpoint_c + 1.0) * 10)
        await client.set_manual_temp(manual_target_decideg)
        print(f"  SET manual_control_temp -> {manual_target_decideg / 10:.1f}C")
        await _pause(2, "let it apply")
    finally:
        await client.disconnect()
    print("  Disconnected. Reconnecting to check the value stuck...")
    await asyncio.sleep(1)

    await client.connect()
    try:
        manual_readback = (await client.get(PID.MANUAL_CONTROL_TEMP)).data
        manual_after_reconnect = struct.unpack("<H", manual_readback)[0]
        print(f"  GET manual_control_temp after reconnect: {manual_after_reconnect / 10:.1f}C "
              f"(expect {manual_target_decideg / 10:.1f}C) -> "
              f"{'STUCK' if manual_after_reconnect == manual_target_decideg else 'DID NOT STICK'}")

        # W-3: 0x1082 while Home program active
        _header("W-3", "Manual temp 0x1082 while Home program is active")
        await client.set_program(Program.HOME)
        print("  SET program -> HOME")
        await _pause(2, "program switch to take")
        home_status = await client.read_status()
        print(f"  Status now: setpoint={home_status.current_set_temperature_c:.1f}C program={home_status.program.name}")
        probe_decideg = round((base_setpoint_c + 1.0) * 10)
        await client.set_manual_temp(probe_decideg)
        print(f"  SET manual_control_temp -> {probe_decideg / 10:.1f}C while HOME is active")
        await _pause(3, "let it (maybe) apply")
        after_probe = await client.read_status()
        print(f"  Status after: setpoint={after_probe.current_set_temperature_c:.1f}C "
              f"(if unchanged from the pre-probe HOME value, 0x1082 does nothing while HOME is active)")
        print("  >>> Check physical display now: did the setpoint change? <<<")
        await _pause(3, "time to glance at display")

        # W-5: Program toggle back to MANUAL (also exercises the 0<->1 toggle pass criteria)
        _header("W-5", "Program toggle HOME -> MANUAL")
        await client.set_program(Program.MANUAL)
        print("  SET program -> MANUAL")
        await _pause(2, "let it apply")
        toggled = await client.read_status()
        print(f"  Status: program={toggled.program.name} setpoint={toggled.current_set_temperature_c:.1f}C")
        print("  >>> Check physical display now: does it show Manual mode? <<<")
        await _pause(3, "time to glance at display")

        # W-4: Power 0 -> 1 (off, then back on)
        _header("W-4", "Power off, then on")
        await client.set_power(False)
        print("  SET power -> OFF")
        await _pause(3, "let relay drop + display refresh")
        off_status = await client.read_status()
        print(f"  Status: power_off={off_status.power_off} relay_on={off_status.relay_on} "
              f"error_flags={off_status.active_error_flags or 'none'}")
        print("  >>> Check physical display now: does it show OFF? <<<")
        await _pause(3, "time to glance at display")
        await client.set_power(True)
        print("  SET power -> ON")
        await _pause(3, "let it recover")
        on_status = await client.read_status()
        print(f"  Status: power_off={on_status.power_off} relay_on={on_status.relay_on}")
        print("  >>> Check physical display now: is it back ON? <<<")
        await _pause(2, "time to glance at display")

        # W-6: Key lock
        _header("W-6", "Key lock: lock, then unlock")
        await client.set_key_lock(True)
        print("  SET key_lock -> LOCKED")
        print("  >>> Try the physical buttons now: they should NOT respond. <<<")
        await _pause(5, "time to test physical buttons while locked")
        lock_readback = (await client.get(PID.KEY_LOCK)).data[0]
        print(f"  GET key_lock: {KeyLock(lock_readback).name}")
        await client.set_key_lock(False)
        print("  SET key_lock -> UNLOCKED")
        print("  >>> Try the physical buttons now: they should respond again. <<<")
        await _pause(5, "time to test physical buttons while unlocked")
        lock_readback = (await client.get(PID.KEY_LOCK)).data[0]
        print(f"  GET key_lock: {KeyLock(lock_readback).name}")

        # W-7: Clock sync
        _header("W-7", "Sync clock 0x0230")
        before = time.time()
        await client.sync_clock()
        after_get = await client.get(PID.NTP_TIME)
        device_time = struct.unpack("<I", after_get.data)[0]
        drift = device_time - before
        print(f"  SET+GET NTP time: device={device_time} local~={before:.0f} drift={drift:+.0f}s "
              f"(pass if within 2s, allowing for round trip)")

        # W-8: Calibration
        _header("W-8", "Calibration 0x10B2: room +0.5C, floor -0.5C")
        await client.set_calibration(room_decideg=5, floor_decideg=-5)
        print("  SET calibration -> room=+0.5C floor=-0.5C")
        await _pause(2, "let it apply")
        cal_readback = (await client.get(PID.CALIBRATION_USER)).data
        room, floor, relay = struct.unpack("<hhh", cal_readback)
        print(f"  GET calibration: room={room / 10:.1f}C floor={floor / 10:.1f}C relay={relay} "
              f"(pass if room=0.5 floor=-0.5 relay=0)")

    finally:
        await client.disconnect()

    print()
    print("All W-1..W-8 probes complete. Now run tools/session.py --restore --file <snapshot> immediately.")


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mac", type=str, required=True)
    parser.add_argument("--psk-file", type=str, default=None)
    parser.add_argument("--psk-b64", type=str, default=None)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    psk = _load_psk(args.psk_file, args.psk_b64)
    asyncio.run(run(args.mac, psk, args.timeout))


if __name__ == "__main__":
    main()
