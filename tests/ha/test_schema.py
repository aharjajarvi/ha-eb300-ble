"""The SET schema must accept exactly what the UI submits.

HA's `time` selector submits `HH:MM:SS`. The original `_TIME_RE` demanded
`HH:MM` and nothing else, so every event the UI produced would have been
rejected even once the selector rendered correctly.
"""
import datetime

import pytest
import voluptuous as vol
from eb300_ble.services import SET_HOME_PROGRAM_SCHEMA, _valid_time


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("06:00", "06:00"),
        ("06:00:00", "06:00"),          # what the UI sends
        ("22:30:45", "22:30"),          # seconds dropped: device resolution is 1 min
        ("00:00:00", "00:00"),
        ("23:59:59", "23:59"),
        (datetime.time(6, 5), "06:05"),  # unquoted YAML sexagesimal
    ],
)
def test_valid_times_normalise_to_hhmm(value, expected):
    assert _valid_time(value) == expected


@pytest.mark.parametrize(
    "value", ["24:00", "06:60", "6:00", "0600", "", "06:00:60", None, 600, "06:00:00.5"]
)
def test_invalid_times_rejected(value):
    with pytest.raises(vol.Invalid):
        _valid_time(value)


def test_ui_shaped_payload_is_accepted():
    """A full frontend-shaped call: device target, HH:MM:SS times, metadata blob."""
    out = SET_HOME_PROGRAM_SCHEMA(
        {
            "device_id": ["abc123"],
            "metadata": {},
            "monday": [
                {"time": "06:00:00", "temperature": 22.0},
                {"time": "08:00:00", "temperature": 17.0},
            ],
            "sunday": [{"time": "07:00:00", "temperature": 21.5}],
        }
    )
    assert out["monday"] == [
        {"time": "06:00", "temperature": 22.0, "active": True},
        {"time": "08:00", "temperature": 17.0, "active": True},
    ]
    assert out["sunday"] == [{"time": "07:00", "temperature": 21.5, "active": True}]
    assert "metadata" not in out


def test_more_than_four_events_rejected():
    with pytest.raises(vol.Invalid):
        SET_HOME_PROGRAM_SCHEMA(
            {
                "entity_id": ["climate.x"],
                "monday": [{"time": f"0{i}:00:00", "temperature": 20.0} for i in range(5)],
            }
        )


def test_target_alone_is_accepted_and_rejected_later():
    """No weekday given is schema-valid; the handler is what rejects it."""
    assert SET_HOME_PROGRAM_SCHEMA({"entity_id": ["climate.x"]}) == {
        "entity_id": ["climate.x"]
    }
