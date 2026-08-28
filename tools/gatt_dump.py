#!/usr/bin/env python3
"""H-2: connect to the EB300, discover GATT services, report the negotiated MTU.

Read-only — this only discovers services/characteristics, it never writes to
RX or reads a characteristic value. Zero risk to device state.

Usage:
    uv run --project tests/lib python tools/gatt_dump.py --mac AA:BB:CC:DD:EE:FF
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "eb300_ble"))

from bleak import BleakClient
from eb300_ble.const import (
    CHAR_DATA_STREAM_RX,
    CHAR_DATA_STREAM_TX,
    SERVICE_DATA_ACCESS,
)


async def run(mac: str, timeout: float) -> None:
    print(f"Connecting to {mac} (timeout {timeout:.0f}s)...")
    async with BleakClient(mac, timeout=timeout) as client:
        print(f"Connected. Negotiated MTU: {client.mtu_size} bytes"
              f" (want >= 185 for the largest 118-byte-payload frame; usable inner budget"
              f" shrinks accordingly if lower)")
        print()

        found_service = False
        found_rx = False
        found_tx = False

        for service in client.services:
            print(f"Service {service.uuid}")
            if service.uuid.lower() == SERVICE_DATA_ACCESS.lower():
                found_service = True
            for char in service.characteristics:
                props = ",".join(char.properties)
                marker = ""
                if char.uuid.lower() == CHAR_DATA_STREAM_RX.lower():
                    found_rx = True
                    marker = "  <-- Data Stream RX (expect: write)"
                elif char.uuid.lower() == CHAR_DATA_STREAM_TX.lower():
                    found_tx = True
                    marker = "  <-- Data Stream TX (expect: notify)"
                print(f"  Characteristic {char.uuid}  [{props}]{marker}")
        print()

        print("H-2 checklist:")
        print(f"  [{'x' if found_service else ' '}] Data Access service {SERVICE_DATA_ACCESS} present")
        print(f"  [{'x' if found_rx else ' '}] Data Stream RX {CHAR_DATA_STREAM_RX} present")
        print(f"  [{'x' if found_tx else ' '}] Data Stream TX {CHAR_DATA_STREAM_TX} present")
        print(f"  [{'x' if client.mtu_size >= 185 else ' '}] MTU >= 185 (got {client.mtu_size})")

        if not (found_service and found_rx and found_tx):
            print()
            print("FAIL: expected service/characteristics not found.")
            sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mac", type=str, required=True, help="Device MAC address, e.g. AA:BB:CC:DD:EE:FF")
    parser.add_argument("--timeout", type=float, default=15.0, help="BLE connect timeout in seconds (default 15)")
    args = parser.parse_args()
    asyncio.run(run(args.mac, args.timeout))


if __name__ == "__main__":
    main()
