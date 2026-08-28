#!/usr/bin/env python3
"""H-5 / H-6 / H-8: read-only sweep — device info, ping, NTP drift, status, energy.

Entirely read-only. Zero risk to device state.

Usage:
    uv run --project tests/lib python tools/read_all.py --mac AA:BB:CC:DD:EE:FF --psk-file psk.txt --status --verbose
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
from eb300_ble.const import PID
from eb300_ble.exceptions import DeviceError


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


async def run(mac: str, psk: bytes, timeout: float, show_status: bool, verbose: bool) -> None:
    transport = BleakTransport(mac, timeout=timeout)
    client = EB300Client(transport, psk, request_timeout=timeout)

    t0 = time.monotonic()
    await client.connect()
    connect_elapsed = time.monotonic() - t0

    try:
        print(f"Connected and handshaked in {connect_elapsed:.2f}s")
        print()

        info = await client.read_device_info()
        print("Device info:")
        print(f"  model:            {info.model!r}")
        print(f"  batch:            {info.batch!r}")
        print(f"  serial:           {info.serial!r}")
        print(f"  firmware_version: {info.firmware_version!r}")
        print()

        t_ping = time.monotonic()
        await client.ping()
        ping_ms = (time.monotonic() - t_ping) * 1000
        print(f"Ping: OK ({ping_ms:.0f}ms round trip)")
        print()

        try:
            ntp_msg = await client.get(PID.NTP_TIME)
            device_time = struct.unpack("<I", ntp_msg.data)[0]
            local_time = int(time.time())
            drift = device_time - local_time
            print(f"NTP time: device={device_time} local={local_time} drift={drift:+d}s")
        except DeviceError as exc:
            print(f"NTP time: unsupported on this firmware ({exc})")
        print()

        try:
            energy = await client.read_energy_meter()
            print(f"Energy meter: {energy} minutes ({energy / 60:.1f} hours) cumulative relay-on time")
        except DeviceError as exc:
            print(f"Energy meter: unsupported on this firmware ({exc})")
        print()

        if show_status or verbose:
            status = await client.read_status()
            print("Thermostat status (PID 0x1004):")
            print(f"  set temp:    {status.current_set_temperature_c:.1f} C")
            print(f"  limiting temp: {status.limiting_temperature_c:.1f} C")
            print(f"  room temp:   {status.room_temperature_c:.1f} C")
            print(f"  floor temp:  {status.floor_temperature_c:.1f} C")
            print(f"  relay temp:  {status.relay_temperature_c:.1f} C")
            print(f"  time to target: {status.time_to_target} min")
            print(f"  relay on:    {status.relay_on}")
            print(f"  power off:   {status.power_off}")
            print(f"  in error state: {status.in_error_state}")
            print(f"  limited by limiting sensor: {status.limited_by_limiting_sensor}")
            print(f"  program:     {status.program.name}")
            print(f"  room sensor fault:  {status.room_sensor_fault.name}")
            print(f"  floor sensor fault: {status.floor_sensor_fault.name}")
            print(f"  error flags: {status.active_error_flags or 'none'}")
            if verbose:
                raw = status.to_bytes()
                print(f"  raw ({len(raw)} bytes): {raw.hex()}")
    finally:
        await client.disconnect()

    print()
    print("Disconnected cleanly. Read-only sweep complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mac", type=str, required=True)
    parser.add_argument("--psk-file", type=str, default=None)
    parser.add_argument("--psk-b64", type=str, default=None)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--status", action="store_true", help="Also read and print full thermostat status (PID 0x1004)")
    parser.add_argument("--verbose", action="store_true", help="Include raw hex dump of the status struct")
    args = parser.parse_args()

    psk = _load_psk(args.psk_file, args.psk_b64)
    asyncio.run(run(args.mac, psk, args.timeout, args.status, args.verbose))


if __name__ == "__main__":
    main()
