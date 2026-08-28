#!/usr/bin/env python3
"""Scan for the EB300's BLE advertisements and decode them live.

This is the definitive test for whether the thermostat is reachable at all.

A powered EB300 advertises continuously — it does not sleep its radio and does
not need the Ebeco Connect app open (see docs/HARDWARE_NOTES.md). So if nothing
shows up here, it is a range or adapter problem, not the device idling.

Usage:
    uv run --project tests/lib python tools/scan.py --timeout 30
    uv run --project tests/lib python tools/scan.py --mac AA:BB:CC:DD:EE:FF --watch
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "eb300_ble"))

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from eb300_ble.advertisement import (
    is_burst_chunk,
    is_normal_advertisement,
    parse_burst_chunk,
    parse_normal_advertisement,
)
from eb300_ble.const import MANUFACTURER_ID


def _format_mac(mac: bytes) -> str:
    return ":".join(f"{b:02X}" for b in mac)


class _Sighting:
    __slots__ = ("count", "first_seen", "last_seen")

    def __init__(self) -> None:
        self.count = 0
        self.first_seen = 0.0
        self.last_seen: float | None = None


def _make_callback(target_mac: str | None, seen: dict[str, _Sighting], start: float):
    def on_detect(device: BLEDevice, adv: AdvertisementData) -> None:
        payload = adv.manufacturer_data.get(MANUFACTURER_ID)
        if payload is None:
            return
        elapsed = time.monotonic() - start

        if is_normal_advertisement(payload):
            try:
                normal = parse_normal_advertisement(payload)
            except Exception as exc:  # noqa: BLE001 - bring-up tool, report and continue
                print(f"[{elapsed:7.2f}s] malformed normal advert from {device.address}: {exc}")
                return
            mac_str = _format_mac(normal.mac)
            if target_mac is not None and mac_str != target_mac:
                return

            sighting = seen.setdefault(mac_str, _Sighting())
            gap = None if sighting.last_seen is None else elapsed - sighting.last_seen
            sighting.count += 1
            if sighting.last_seen is None:
                sighting.first_seen = elapsed
            sighting.last_seen = elapsed

            tag = "NEW " if sighting.count == 1 else f"#{sighting.count:<4d}"
            gap_str = "" if gap is None else f"  gap={gap:6.2f}s"
            print(
                f"[{elapsed:7.2f}s] {tag} {mac_str}  RSSI={adv.rssi:4d}dBm{gap_str}  "
                f"open_api_psk_provisioned={normal.open_api_psk_provisioned}  "
                f"advert_channel_is_open_api={normal.advert_channel_is_open_api}  "
                f"status_advert_active={normal.status_advert_active}  "
                f"enc_flags=0x{normal.encryption_flags:02X}  "
                f"event_flags=0x{normal.event_flags:04X}  "
                f"regulator_error_flags=0x{normal.regulator_error_flags:04X}"
            )
        elif is_burst_chunk(payload):
            try:
                chunk = parse_burst_chunk(payload)
            except Exception as exc:  # noqa: BLE001
                print(f"[{elapsed:7.2f}s] malformed burst chunk from {device.address}: {exc}")
                return
            print(
                f"[{elapsed:7.2f}s] BURST from {device.address}  counter={chunk.counter}  "
                f"chunk={chunk.chunk_index + 1}/{chunk.total_chunks}  RSSI={adv.rssi}dBm"
            )
        else:
            print(f"[{elapsed:7.2f}s] unknown package type 0x{payload[0]:02X} from {device.address}")

    return on_detect


async def run(timeout: float, target_mac: str | None, watch: bool) -> None:
    seen: dict[str, _Sighting] = {}
    start = time.monotonic()
    callback = _make_callback(target_mac, seen, start)

    print(f"Scanning for Ebeco manufacturer ID 0x{MANUFACTURER_ID:04X}"
          + (f", filtering to MAC {target_mac}" if target_mac else "")
          + (" (Ctrl+C to stop)..." if watch else f" for {timeout:.0f}s..."))

    async with BleakScanner(detection_callback=callback):
        if watch:
            try:
                while True:
                    await asyncio.sleep(3600)
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
        else:
            await asyncio.sleep(timeout)

    print()
    if not seen:
        print("RESULT: no EB300 advertisements seen at all.")
        print("  -> Out of range, or BLE is off on the device. A powered EB300")
        print("     advertises continuously, so idling is not the explanation.")
        return

    print("RESULT: summary by advertised MAC")
    for mac_str, sighting in seen.items():
        span = (sighting.last_seen or 0) - sighting.first_seen
        avg_gap = span / (sighting.count - 1) if sighting.count > 1 else float("nan")
        print(
            f"  {mac_str}: {sighting.count} sightings over {span:.1f}s "
            f"(first at {sighting.first_seen:.1f}s, last at {sighting.last_seen:.1f}s, "
            f"avg gap {avg_gap:.2f}s)"
        )


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)  # stream live even when redirected to a file/pipe

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timeout", type=float, default=30.0, help="Scan duration in seconds (default 30, ignored with --watch)")
    parser.add_argument("--mac", type=str, default=None, help="Only report this advertised MAC, e.g. AA:BB:CC:DD:EE:FF")
    parser.add_argument("--watch", action="store_true", help="Scan until Ctrl+C instead of stopping after --timeout")
    args = parser.parse_args()

    target_mac = args.mac.upper().replace("-", ":") if args.mac else None
    asyncio.run(run(args.timeout, target_mac, args.watch))


if __name__ == "__main__":
    main()
