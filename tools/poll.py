#!/usr/bin/env python3
"""Repeated polling, as the integration will do it. Also the S-1/S-3 soak harness.

Each cycle: connect -> handshake -> GET 0x1004 -> disconnect. Read-only.

- S-1 (50-cycle reconnect loop): `--cycles 50 --interval 2`
- S-3 (12h poll soak at the real interval): `--interval 300` (no --cycles, runs until Ctrl+C)
- I-8 (poll behaviour): same shape as what the HA coordinator will do per cycle

Usage:
    uv run --project tests/lib python tools/poll.py --mac AA:BB:CC:DD:EE:FF --interval 300 --log poll.jsonl
    uv run --project tests/lib python tools/poll.py --mac AA:BB:CC:DD:EE:FF --interval 2 --cycles 50 --log soak-s1.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import resource
import statistics
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "eb300_ble"))

from eb300_ble.client import BleakTransport, EB300Client
from eb300_ble.exceptions import EB300Error


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


def _rss_mb() -> float:
    # ru_maxrss is KB on Linux
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


async def one_cycle(mac: str, psk: bytes, timeout: float) -> dict:
    transport = BleakTransport(mac, timeout=timeout)
    client = EB300Client(transport, psk, request_timeout=timeout)

    t0 = time.monotonic()
    result: dict = {"ts": time.time()}
    try:
        await client.connect()
        t_connected = time.monotonic()
        status = await client.read_status()
        t_read = time.monotonic()
        result.update(
            ok=True,
            connect_s=round(t_connected - t0, 2),
            read_s=round(t_read - t_connected, 2),
            set_temp_c=status.current_set_temperature_c,
            room_temp_c=status.room_temperature_c,
            floor_temp_c=status.floor_temperature_c,
            relay_on=status.relay_on,
            program=status.program.name,
            error_flags=status.active_error_flags,
        )
    except EB300Error as exc:
        result.update(ok=False, error_type=type(exc).__name__, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - soak harness: record and keep going
        result.update(ok=False, error_type=type(exc).__name__, error=str(exc))
    finally:
        try:
            await client.disconnect()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup, never let this abort the cycle
            print(f"  (cleanup disconnect also failed: {type(exc).__name__}: {exc})")
        result["total_s"] = round(time.monotonic() - t0, 2)
        result["rss_mb"] = round(_rss_mb(), 1)

    return result


async def run(
    mac: str,
    psk: bytes,
    timeout: float,
    interval: float,
    cycles: int | None,
    log_file: TextIO | None,
) -> None:
    results: list[dict] = []
    cycle = 0

    print(
        f"Polling {mac} every {interval:.0f}s"
        + (f" for {cycles} cycles" if cycles else " until Ctrl+C")
    )

    try:
        while cycles is None or cycle < cycles:
            cycle += 1
            result = await one_cycle(mac, psk, timeout)
            result["cycle"] = cycle
            results.append(result)

            if log_file:
                log_file.write(json.dumps(result) + "\n")
                log_file.flush()

            if result["ok"]:
                print(
                    f"[{cycle:4d}] OK   total={result['total_s']:5.2f}s "
                    f"(connect={result['connect_s']:5.2f}s read={result['read_s']:.2f}s)  "
                    f"set={result['set_temp_c']:.1f}C room={result['room_temp_c']:.1f}C "
                    f"relay={'ON' if result['relay_on'] else 'off'}  rss={result['rss_mb']:.0f}MB"
                )
            else:
                print(
                    f"[{cycle:4d}] FAIL total={result['total_s']:5.2f}s  "
                    f"{result['error_type']}: {result['error']}  rss={result['rss_mb']:.0f}MB"
                )

            if cycles is None or cycle < cycles:
                await asyncio.sleep(interval)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass

    _summarize(results)


def _summarize(results: list[dict]) -> None:
    if not results:
        print("No cycles completed.")
        return

    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    print()
    print(f"RESULT: {len(ok)}/{len(results)} cycles succeeded ({100 * len(ok) / len(results):.1f}%)")

    if ok:
        totals = [r["total_s"] for r in ok]
        connects = [r["connect_s"] for r in ok]
        print(
            f"  total cycle time:  min={min(totals):.2f}s  avg={statistics.mean(totals):.2f}s  max={max(totals):.2f}s"
        )
        print(
            f"  connect time:      min={min(connects):.2f}s  avg={statistics.mean(connects):.2f}s  max={max(connects):.2f}s"
        )

    if failed:
        by_type: dict[str, int] = {}
        for r in failed:
            by_type[r["error_type"]] = by_type.get(r["error_type"], 0) + 1
        print(f"  failures by type:  {by_type}")

    rss_values = [r["rss_mb"] for r in results]
    print(f"  RSS:               first={rss_values[0]:.0f}MB  last={rss_values[-1]:.0f}MB  max={max(rss_values):.0f}MB")


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mac", type=str, required=True)
    parser.add_argument("--psk-file", type=str, default=None)
    parser.add_argument("--psk-b64", type=str, default=None)
    parser.add_argument("--timeout", type=float, default=20.0, help="Per-connect BLE timeout (default 20)")
    parser.add_argument("--interval", type=float, default=300.0, help="Seconds between cycle starts (default 300)")
    parser.add_argument("--cycles", type=int, default=None, help="Stop after N cycles (default: run until Ctrl+C)")
    parser.add_argument("--log", type=str, default=None, help="Append each cycle as a JSON line to this file")
    args = parser.parse_args()

    psk = _load_psk(args.psk_file, args.psk_b64)
    if args.log:
        print(f"Logging each cycle to {args.log}")
    with open(args.log, "a") if args.log else nullcontext(None) as log_file:
        asyncio.run(run(args.mac, psk, args.timeout, args.interval, args.cycles, log_file))


if __name__ == "__main__":
    main()
