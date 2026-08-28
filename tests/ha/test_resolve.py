"""Does _resolve_coordinator actually accept a device target on real HA 2026.8.1?"""
import pytest
from eb300_ble.const import DOMAIN
from eb300_ble.services import _resolve_coordinator
from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from pytest_homeassistant_custom_component.common import MockConfigEntry

SENTINEL = object()

def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="AA:BB")
    entry.add_to_hass(hass)
    entry.runtime_data = SENTINEL
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:FF")},
        name="EB300",
    )
    ent = er.async_get(hass)
    climate = ent.async_get_or_create(
        "climate", DOMAIN, "AA:BB-climate", config_entry=entry, device_id=device.id,
    )
    # a config entity on the same device: device targets pull these in too
    ent.async_get_or_create(
        "button", DOMAIN, "AA:BB-sync", config_entry=entry, device_id=device.id,
        entity_category=EntityCategory.CONFIG,
    )
    return entry, device, climate

def _call(hass, data):
    return ServiceCall(hass, DOMAIN, "set_home_program", data)

async def test_entity_target(hass):
    _, _, climate = _setup(hass)
    assert _resolve_coordinator(hass, _call(hass, {"entity_id": [climate.entity_id]})) is SENTINEL

async def test_device_target(hass):
    """The bug the user hit: this used to be rejected outright."""
    _, device, _ = _setup(hass)
    assert _resolve_coordinator(hass, _call(hass, {"device_id": [device.id]})) is SENTINEL

async def test_device_and_entity_together(hass):
    _, device, climate = _setup(hass)
    call = _call(hass, {"device_id": [device.id], "entity_id": [climate.entity_id]})
    assert _resolve_coordinator(hass, call) is SENTINEL

async def test_area_target(hass):
    from homeassistant.helpers import area_registry as ar
    _, device, _ = _setup(hass)
    area = ar.async_get(hass).async_get_or_create("Hall")
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    assert _resolve_coordinator(hass, _call(hass, {"area_id": [area.id]})) is SENTINEL

async def test_foreign_entity_rejected(hass):
    _setup(hass)
    with pytest.raises(ServiceValidationError, match="not an eb300_ble entity"):
        _resolve_coordinator(hass, _call(hass, {"entity_id": ["light.kitchen"]}))

async def test_two_thermostats_rejected(hass):
    _, d1, _ = _setup(hass)
    e2 = MockConfigEntry(domain=DOMAIN, data={}, unique_id="CC:DD")
    e2.add_to_hass(hass)
    e2.runtime_data = object()
    d2 = dr.async_get(hass).async_get_or_create(
        config_entry_id=e2.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, "11:22:33:44:55:66")}, name="EB300 #2")
    er.async_get(hass).async_get_or_create(
        "climate", DOMAIN, "CC:DD-climate", config_entry=e2, device_id=d2.id)
    with pytest.raises(ServiceValidationError, match="exactly one"):
        _resolve_coordinator(hass, _call(hass, {"device_id": [d1.id, d2.id]}))

async def test_unknown_device_rejected(hass):
    _setup(hass)
    with pytest.raises(ServiceValidationError, match="exactly one"):
        _resolve_coordinator(hass, _call(hass, {"device_id": ["nope"]}))
