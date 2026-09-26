"""Rotating the PSK on an existing entry, by both routes.

Ebeco issues a fresh key every time local API is toggled in the Connect app, so
a stored key can go dead on a device that is otherwise perfectly reachable.
Before reauth/reconfigure existed the only fix was deleting the entry and
adding it back, which drops every registry customisation (renamed entities,
areas, hidden flags) that HA keys to the entry rather than to the MAC.

Two halves here: the coordinator recognising a refusal as an auth failure
rather than a connectivity one, and the flows that swap the key in place.
"""
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eb300_ble.config_flow import CONF_ADDRESS, CannotConnect, InvalidAuth
from eb300_ble.const import CONF_PSK, DOMAIN
from eb300_ble.coordinator import EB300Coordinator
from eb300_ble.eb300_ble.exceptions import EB300ConnectionError, HandshakeError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

ADDRESS = "AA:BB:CC:DD:EE:FF"
OLD_PSK = base64.b64encode(bytes(range(32))).decode()
NEW_PSK = base64.b64encode(bytes(range(1, 33))).decode()

REJECTED = HandshakeError("wrong PSK?", step=3, error_code=4)
UNREACHABLE = HandshakeError("Timed out waiting for handshake response", step=0)


@pytest.fixture(autouse=True)
def _loadable(registered_integration):
    """Every flow here is started by domain, so HA has to be able to find us."""


def _entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=format_mac(ADDRESS),
        title="EB-Therm 300 (123456)",
        data={CONF_ADDRESS: ADDRESS, CONF_PSK: OLD_PSK},
    )
    entry.add_to_hass(hass)
    return entry


def _coordinator(hass):
    return EB300Coordinator(hass, _entry(hass), ADDRESS, b"\x00" * 32, 60)


# --- the coordinator's half: which failures mean "the key is wrong" ------


async def test_rejected_key_raises_config_entry_auth_failed(hass):
    """ConfigEntryAuthFailed is what makes HA offer the reauth prompt."""
    coordinator = _coordinator(hass)
    with patch.object(EB300Coordinator, "_with_client", side_effect=REJECTED), pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


@pytest.mark.parametrize("error", [UNREACHABLE, EB300ConnectionError("out of range"), TimeoutError()])
async def test_connectivity_failures_stay_update_failed(hass, error):
    """A device that cannot answer must not be reported as a bad key."""
    coordinator = _coordinator(hass)
    with patch.object(EB300Coordinator, "_with_client", side_effect=error), pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_rejected_key_is_not_retried(hass):
    """Retrying a refused key only burns shared BLE connection slots."""
    coordinator = _coordinator(hass)
    with patch.object(EB300Coordinator, "_run_once", side_effect=REJECTED) as run_once, pytest.raises(HandshakeError):
        await coordinator._with_client(AsyncMock())
    assert run_once.call_count == 1


async def test_connectivity_failure_still_retries(hass):
    """The fast-fail must be scoped to rejections, not to every handshake error."""
    coordinator = _coordinator(hass)
    with patch.object(EB300Coordinator, "_run_once", side_effect=UNREACHABLE) as run_once, pytest.raises(HandshakeError):
        await coordinator._with_client(AsyncMock())
    assert run_once.call_count > 1


# --- the flows' half ----------------------------------------------------


@pytest.fixture
def validated():
    """Stand in for the live handshake the flow does before accepting a key."""
    with patch(
        "eb300_ble.config_flow._validate_and_fetch_device_info",
        AsyncMock(return_value=MagicMock(serial="123456")),
    ) as mock:
        yield mock


async def _start(hass, entry, reconfigure):
    return await (entry.start_reconfigure_flow(hass) if reconfigure else entry.start_reauth_flow(hass))


@pytest.mark.parametrize(
    ("reconfigure", "step_id", "abort_reason"),
    [(False, "reauth_confirm", "reauth_successful"), (True, "reconfigure", "reconfigure_successful")],
)
async def test_new_key_replaces_the_old_one_in_place(hass, validated, reconfigure, step_id, abort_reason):
    entry = _entry(hass)
    entry_id = entry.entry_id

    result = await _start(hass, entry, reconfigure)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step_id

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PSK: NEW_PSK})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == abort_reason

    # Same entry, same unique ID, new key: that is the whole point — anything
    # keyed to the entry (entity IDs, history, areas) survives.
    assert entry.entry_id == entry_id
    assert entry.data == {CONF_ADDRESS: ADDRESS, CONF_PSK: NEW_PSK}
    assert entry.unique_id == format_mac(ADDRESS)

    # The key was validated against the entry's own device, and the address was
    # never re-asked.
    assert validated.call_args.args[1] == ADDRESS


async def test_key_is_validated_before_it_is_stored(hass):
    """A key the device refuses must not overwrite one that might still work."""
    entry = _entry(hass)
    result = await entry.start_reauth_flow(hass)

    with patch("eb300_ble.config_flow._validate_and_fetch_device_info", AsyncMock(side_effect=InvalidAuth)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PSK: NEW_PSK})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_PSK] == OLD_PSK


async def test_unreachable_device_offers_a_retry_not_a_key_error(hass):
    entry = _entry(hass)
    result = await entry.start_reauth_flow(hass)

    with patch("eb300_ble.config_flow._validate_and_fetch_device_info", AsyncMock(side_effect=CannotConnect)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PSK: NEW_PSK})

    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_PSK] == OLD_PSK


@pytest.mark.parametrize(
    ("typed", "error"),
    [("not base64!", "psk_not_base64"), (base64.b64encode(b"short").decode(), "psk_wrong_length")],
)
async def test_malformed_keys_are_rejected_without_touching_the_device(hass, validated, typed, error):
    entry = _entry(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PSK: typed})

    assert result["errors"] == {CONF_PSK: error}
    assert validated.call_count == 0
    assert entry.data[CONF_PSK] == OLD_PSK


async def test_surrounding_whitespace_is_stripped(hass, validated):
    """Keys arrive by email and get pasted with a stray newline more often than not."""
    entry = _entry(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PSK: f"  {NEW_PSK}\n"})

    assert result["type"] is FlowResultType.ABORT
    assert entry.data[CONF_PSK] == NEW_PSK


async def test_re_entering_the_stored_key_still_reloads(hass, validated):
    """Nothing changes in the entry, so nothing else would clear the failed state."""
    entry = _entry(hass)
    result = await entry.start_reauth_flow(hass)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PSK: OLD_PSK})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    reload.assert_called_once_with(entry.entry_id)


async def test_reload_puts_the_new_key_on_the_wire(hass, validated):
    """The end the user cares about: the running coordinator polls with the new key.

    Set up for real (minus the BLE poll and the platforms) so the update
    listener registered by `async_setup_entry` is the one doing the reload —
    that listener is why the flow deliberately does not call
    `async_update_reload_and_abort`.
    """
    entry = _entry(hass)
    with (
        patch("eb300_ble.PLATFORMS", []),
        patch.object(EB300Coordinator, "_async_update_data", AsyncMock(return_value=MagicMock())),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data._psk == base64.b64decode(OLD_PSK)

        result = await entry.start_reauth_flow(hass)
        await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PSK: NEW_PSK})
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data._psk == base64.b64decode(NEW_PSK)
