"""`days` + `events` multi-day shorthand.

Almost every real edit is "these weekdays all alike", which previously meant
repeating an identical event list five times. `_collect_updates` folds the
shorthand and the per-weekday fields into the same map the merge consumes.
"""
import pytest
from eb300_ble.const import DOMAIN, WEEKDAYS
from eb300_ble.services import SET_HOME_PROGRAM_SCHEMA, _collect_updates
from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

WEEK = [
    {"time": "06:00", "temperature": 22.0},
    {"time": "08:00", "temperature": 17.0, "active": False},
    {"time": "23:00", "temperature": 17.0},
]


def _call(hass, data):
    """Validate through the real schema first, as a live call would be."""
    return ServiceCall(hass, DOMAIN, "set_home_program",
                       SET_HOME_PROGRAM_SCHEMA({"entity_id": ["climate.x"], **data}))


async def test_shorthand_applies_to_every_named_day(hass):
    updates = _collect_updates(_call(hass, {
        "days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
        "events": WEEK,
    }))
    assert set(updates) == {"monday", "tuesday", "wednesday", "thursday", "friday"}
    assert all(updates[d] == updates["monday"] for d in updates)
    assert updates["monday"][1]["active"] is False


async def test_shorthand_and_per_day_combine(hass):
    """Weekdays via the shorthand, weekend spelled out - one call."""
    weekend = [{"time": "08:00", "temperature": 21.0}]
    updates = _collect_updates(_call(hass, {
        "days": ["monday", "tuesday"], "events": WEEK,
        "saturday": weekend, "sunday": weekend,
    }))
    assert set(updates) == {"monday", "tuesday", "saturday", "sunday"}
    assert updates["saturday"] == updates["sunday"]
    assert updates["monday"] != updates["saturday"]


async def test_per_day_alone_still_works(hass):
    """The pre-shorthand call shape must keep working unchanged."""
    updates = _collect_updates(_call(hass, {"monday": WEEK}))
    assert set(updates) == {"monday"}


async def test_day_named_twice_is_rejected(hass):
    """Ambiguous: the two forms would write different schedules to one day."""
    with pytest.raises(ServiceValidationError, match="monday, friday"):
        _collect_updates(_call(hass, {
            "days": ["monday", "friday"], "events": WEEK,
            "monday": [{"time": "07:00", "temperature": 20.0}],
            "friday": [{"time": "07:00", "temperature": 20.0}],
        }))


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"days": ["monday"]}, "without 'events'"),
        ({"events": WEEK}, "without 'days'"),
    ],
)
async def test_half_the_shorthand_is_rejected(hass, data, match):
    with pytest.raises(ServiceValidationError, match=match):
        _collect_updates(_call(hass, data))


async def test_no_day_at_all_is_rejected(hass):
    with pytest.raises(ServiceValidationError, match="No weekday given"):
        _collect_updates(_call(hass, {}))


async def test_duplicate_days_are_harmless(hass):
    updates = _collect_updates(_call(hass, {"days": ["monday", "monday"], "events": WEEK}))
    assert set(updates) == {"monday"}


def test_unknown_weekday_rejected_by_schema():
    import voluptuous as vol
    with pytest.raises(vol.Invalid):
        SET_HOME_PROGRAM_SCHEMA(
            {"entity_id": ["climate.x"], "days": ["someday"], "events": WEEK}
        )


def test_all_seven_days_via_shorthand():
    out = SET_HOME_PROGRAM_SCHEMA(
        {"entity_id": ["climate.x"], "days": list(WEEKDAYS), "events": WEEK}
    )
    assert out["days"] == list(WEEKDAYS)
