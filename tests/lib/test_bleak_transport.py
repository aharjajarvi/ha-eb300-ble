"""BleakTransport normalizes raw bleak/bleak_retry_connector failures to EB300ConnectionError.

Regression test for a real bug found on hardware (docs/HARDWARE_NOTES.md):
bleak.exc.BleakError (and every bleak_retry_connector subclass of it) is not
an EB300Error or a TimeoutError, so leaving it unwrapped let it slip past
every caller that only catches those two — including the HA coordinator's
retry logic and the climate entity's failed-write state revert.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from bleak.exc import BleakError
from eb300_ble.client import BleakTransport
from eb300_ble.exceptions import EB300ConnectionError


@pytest.mark.asyncio
async def test_connect_wraps_bleak_error_as_eb300_connection_error() -> None:
    transport = BleakTransport("AA:BB:CC:DD:EE:FF")

    fake_device = AsyncMock(name="fake_device")
    fake_device.name = "EB300"
    fake_device.address = "AA:BB:CC:DD:EE:FF"

    with (
        patch("bleak.BleakScanner.find_device_by_address", new=AsyncMock(return_value=fake_device)),
        patch("bleak_retry_connector.establish_connection", new=AsyncMock(side_effect=BleakError("no route"))),
        pytest.raises(EB300ConnectionError),
    ):
        await transport.connect()


@pytest.mark.asyncio
async def test_write_wraps_bleak_error_as_eb300_connection_error() -> None:
    transport = BleakTransport("AA:BB:CC:DD:EE:FF")
    transport._client = AsyncMock()  # type: ignore[attr-defined]
    transport._client.write_gatt_char = AsyncMock(side_effect=BleakError("disconnected"))  # type: ignore[attr-defined]

    with pytest.raises(EB300ConnectionError):
        await transport.write(b"\x00")


@pytest.mark.asyncio
async def test_connect_wraps_start_notify_bleak_error() -> None:
    """A link dropped between connect and notify-subscribe must not escape raw."""
    transport = BleakTransport("AA:BB:CC:DD:EE:FF")

    fake_device = AsyncMock(name="fake_device")
    fake_device.name = "EB300"
    fake_device.address = "AA:BB:CC:DD:EE:FF"

    connected = AsyncMock()
    connected.start_notify = AsyncMock(side_effect=BleakError("le-connection-abort-by-local"))

    with (
        patch("bleak.BleakScanner.find_device_by_address", new=AsyncMock(return_value=fake_device)),
        patch("bleak_retry_connector.establish_connection", new=AsyncMock(return_value=connected)),
        pytest.raises(EB300ConnectionError),
    ):
        await transport.connect()


@pytest.mark.asyncio
async def test_disconnect_swallows_bleak_error() -> None:
    """disconnect() runs from callers' `finally` — raising there would mask the real error."""
    transport = BleakTransport("AA:BB:CC:DD:EE:FF")
    client = AsyncMock()
    client.is_connected = True
    client.disconnect = AsyncMock(side_effect=BleakError("already gone"))
    transport._client = client  # type: ignore[attr-defined]

    await transport.disconnect()  # must not raise

    assert transport._client is None  # type: ignore[attr-defined]
