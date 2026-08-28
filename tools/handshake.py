#!/usr/bin/env python3
"""H-3 / H-4: perform the real handshake against the EB300, with the real or a wrong PSK.

Read-only — the handshake itself writes no thermostat state, only establishes
a session. With --get-status it also does one GET 0x1004 read as a bonus
sanity check (still read-only).

Usage:
    uv run --project tests/lib python tools/handshake.py --mac AA:BB:CC:DD:EE:FF --psk-file psk.txt
    uv run --project tests/lib python tools/handshake.py --mac AA:BB:CC:DD:EE:FF --psk-file psk.txt --wrong-psk
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "eb300_ble"))

from eb300_ble.client import BleakTransport, EB300Client
from eb300_ble.exceptions import HandshakeError


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


async def run(mac: str, psk: bytes, timeout: float, get_status: bool) -> None:
    transport = BleakTransport(mac, timeout=timeout)
    client = EB300Client(transport, psk, request_timeout=timeout)

    print(f"Connecting to {mac}...")
    t0 = time.monotonic()
    try:
        await client.connect()
    except HandshakeError as exc:
        elapsed = time.monotonic() - t0
        print(f"FAIL: handshake failed at step {exc.step} after {elapsed:.2f}s: {exc}")
        if exc.error_code is not None:
            print(f"  device error code: {exc.error_code}")
        raise SystemExit(1) from exc

    elapsed = time.monotonic() - t0
    print(f"OK: session secured in {elapsed:.2f}s")

    if get_status:
        status = await client.read_status()
        print()
        print("Bonus GET 0x1004 (Thermostat Status):")
        print(f"  set temp:    {status.current_set_temperature_c:.1f} C")
        print(f"  room temp:   {status.room_temperature_c:.1f} C")
        print(f"  floor temp:  {status.floor_temperature_c:.1f} C")
        print(f"  relay temp:  {status.relay_temperature_c:.1f} C")
        print(f"  relay on:    {status.relay_on}")
        print(f"  power off:   {status.power_off}")
        print(f"  program:     {status.program.name}")
        print(f"  error flags: {status.active_error_flags or 'none'}")

    await client.disconnect()
    print()
    print("Disconnected cleanly.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mac", type=str, required=True)
    parser.add_argument("--psk-file", type=str, default=None, help="Path to a file containing a 'base64: ...' PSK line")
    parser.add_argument("--psk-b64", type=str, default=None, help="PSK as a base64 string directly")
    parser.add_argument("--wrong-psk", action="store_true", help="Flip a byte in the decoded PSK (H-4: expect a clean failure)")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--get-status", action="store_true", help="After handshake, also GET 0x1004 as a bonus sanity check")
    args = parser.parse_args()

    psk = bytearray(_load_psk(args.psk_file, args.psk_b64))
    if args.wrong_psk:
        psk[0] ^= 0xFF
        print("(--wrong-psk: flipped the first PSK byte — expecting a clean handshake failure)")

    asyncio.run(run(args.mac, bytes(psk), args.timeout, args.get_status))


if __name__ == "__main__":
    main()
