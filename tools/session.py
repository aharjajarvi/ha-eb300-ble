#!/usr/bin/env python3
"""Safety rule for write testing: --snapshot before any write test, --restore after.

Snapshot dumps every writable non-momentary PID (power, manual control temp,
selected program, home program, calibration, key lock, language, screensaver)
to a timestamped JSON file. Restore reads a snapshot back and SETs every value
onto the device, then GETs each one back to confirm it stuck.

Deliberately does NOT touch 0x10D0 (Override Temperature): it's write-only and
momentary, there is nothing to read back or restore — restoring 0x1082/0x1083
puts the device back in the state it would have settled into anyway.

Usage:
    uv run --project tests/lib python tools/session.py --mac AA:BB:CC:DD:EE:FF --psk-file psk.txt --snapshot
    uv run --project tests/lib python tools/session.py --mac AA:BB:CC:DD:EE:FF --psk-file psk.txt --restore --file snapshot-20260819-213000.json
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "eb300_ble"))

from eb300_ble.client import BleakTransport, EB300Client
from eb300_ble.const import PID

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"


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


async def _snapshot(client: EB300Client) -> dict[str, object]:
    power_on = (await client.get(PID.POWER_ON)).data
    manual_temp = (await client.get(PID.MANUAL_CONTROL_TEMP)).data
    program = (await client.get(PID.SELECTED_PROGRAM)).data
    home_program = (await client.get(PID.HOME_PROGRAM)).data
    calibration = (await client.get(PID.CALIBRATION_USER)).data
    key_lock = (await client.get(PID.KEY_LOCK)).data
    language = (await client.get(PID.LANGUAGE)).data
    screensaver = (await client.get(PID.SCREENSAVER_TYPE)).data

    return {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "power_on": power_on[0],
        "manual_control_temp_decideg": struct.unpack("<H", manual_temp)[0],
        "selected_program": program[0],
        "home_program_hex": home_program.hex(),
        "calibration_decideg": list(struct.unpack("<hhh", calibration)),
        "key_lock": key_lock[0],
        "language": language[0],
        "screensaver_type": screensaver[0],
    }


async def _restore(client: EB300Client, snap: dict[str, object]) -> None:
    calibration = snap["calibration_decideg"]
    assert isinstance(calibration, list)
    room, floor, _relay = (int(v) for v in calibration)

    print(f"  power_on -> {snap['power_on']}")
    await client.set_power(bool(snap["power_on"]))

    print(f"  manual_control_temp -> {snap['manual_control_temp_decideg']}")
    await client.set_manual_temp(int(snap["manual_control_temp_decideg"]))  # type: ignore[arg-type]

    print(f"  selected_program -> {snap['selected_program']}")
    await client.set(PID.SELECTED_PROGRAM, bytes([int(snap["selected_program"])]))  # type: ignore[arg-type]

    print("  home_program -> (112 bytes)")
    await client.set(PID.HOME_PROGRAM, bytes.fromhex(str(snap["home_program_hex"])))

    print(f"  calibration -> room={room} floor={floor}")
    await client.set_calibration(room_decideg=room, floor_decideg=floor)

    print(f"  key_lock -> {snap['key_lock']}")
    await client.set_key_lock(bool(snap["key_lock"]))

    print(f"  language -> {snap['language']}")
    await client.set(PID.LANGUAGE, bytes([int(snap["language"])]))  # type: ignore[arg-type]

    print(f"  screensaver_type -> {snap['screensaver_type']}")
    await client.set(PID.SCREENSAVER_TYPE, bytes([int(snap["screensaver_type"])]))  # type: ignore[arg-type]


async def _verify(client: EB300Client, snap: dict[str, object]) -> list[str]:
    """Re-read every restored PID and report anything that didn't stick."""
    after = await _snapshot(client)
    mismatches = [key for key in snap if key != "captured_at" and after[key] != snap[key]]
    return mismatches


async def run(mac: str, psk: bytes, timeout: float, mode: str, file_path: str | None) -> None:
    transport = BleakTransport(mac, timeout=timeout)
    client = EB300Client(transport, psk, request_timeout=timeout)
    await client.connect()
    try:
        if mode == "snapshot":
            snap = await _snapshot(client)
            SNAPSHOT_DIR.mkdir(exist_ok=True)
            out_path = SNAPSHOT_DIR / f"snapshot-{time.strftime('%Y%m%d-%H%M%S')}.json"
            out_path.write_text(json.dumps(snap, indent=2))
            print(f"Snapshot written: {out_path}")
            print(json.dumps(snap, indent=2))
        else:
            assert file_path is not None
            snap = json.loads(Path(file_path).read_text())
            print(f"Restoring from {file_path}:")
            await _restore(client, snap)
            mismatches = await _verify(client, snap)
            if mismatches:
                print(f"MISMATCH after restore: {mismatches} did not read back as written")
                raise SystemExit(1)
            print("Restore verified: every field reads back as snapshotted.")
    finally:
        await client.disconnect()


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mac", type=str, required=True)
    parser.add_argument("--psk-file", type=str, default=None)
    parser.add_argument("--psk-b64", type=str, default=None)
    parser.add_argument("--timeout", type=float, default=20.0)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--snapshot", action="store_true")
    mode.add_argument("--restore", action="store_true")
    parser.add_argument("--file", type=str, default=None, help="Snapshot JSON to restore (required with --restore)")
    args = parser.parse_args()

    if args.restore and not args.file:
        parser.error("--restore requires --file")

    psk = _load_psk(args.psk_file, args.psk_b64)
    asyncio.run(run(args.mac, psk, args.timeout, "snapshot" if args.snapshot else "restore", args.file))


if __name__ == "__main__":
    main()
