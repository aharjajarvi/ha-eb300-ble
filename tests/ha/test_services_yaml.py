"""Validate services.yaml against HA's own selector machinery.

The 2026-08-27 deploy shipped `object: {}` for every weekday. An object
selector defaults to `multiple: false`, so a *list* of events — which is what
every day field is — could not be entered in the UI at all. Nothing caught it
because services.yaml was never validated anywhere but by the frontend.
"""
import pathlib

import pytest
import voluptuous as vol
import yaml
from eb300_ble.const import WEEKDAYS
from homeassistant.helpers.selector import selector as make_selector

COMPONENT_DIR = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "eb300_ble"
SERVICES_YAML = COMPONENT_DIR / "services.yaml"

SERVICES = yaml.safe_load(SERVICES_YAML.read_text())
SET_FIELDS = SERVICES["set_home_program"]["fields"]
# A section is UI grouping only; flatten it the way the frontend does.
DAY_FIELDS = SET_FIELDS["per_day"]["fields"]

# What the frontend actually submits: the `time` selector yields HH:MM:SS.
UI_EVENTS = [
    {"time": "06:00:00", "temperature": 22.0},
    {"time": "22:30:00", "temperature": 17.5},
]


def test_all_weekdays_present():
    assert set(DAY_FIELDS) == set(WEEKDAYS)


def test_shorthand_fields_present():
    assert set(SET_FIELDS) == {"days", "events", "per_day"}
    assert SET_FIELDS["per_day"]["collapsed"] is True


def test_events_uses_the_same_selector_as_a_weekday():
    """The shorthand and the per-day fields must accept identical input."""
    for day in WEEKDAYS:
        assert SET_FIELDS["events"]["selector"] == DAY_FIELDS[day]["selector"]


def test_days_selector_offers_every_weekday():
    sel = SET_FIELDS["days"]["selector"]["select"]
    assert sel["multiple"] is True
    assert [o["value"] for o in sel["options"]] == list(WEEKDAYS)


@pytest.mark.parametrize("day", WEEKDAYS)
def test_selector_config_is_valid(day):
    """A malformed selector config raises here, as it would in hassfest."""
    make_selector(DAY_FIELDS[day]["selector"])


@pytest.mark.parametrize("day", WEEKDAYS)
def test_selector_accepts_a_list_of_events(day):
    sel = make_selector(DAY_FIELDS[day]["selector"])
    assert sel(UI_EVENTS) == UI_EVENTS


@pytest.mark.parametrize("day", WEEKDAYS)
def test_selector_declares_multiple_and_fields(day):
    """Assert the *config*, not just its behaviour.

    ObjectSelector.__call__ returns data untouched when no `fields` are
    declared, so the bare `object: {}` that shipped on 2026-08-27 passes every
    behavioural check above while still being unusable in the UI: the frontend
    is the only thing that enforces `multiple`, and it defaults to False. Only
    an explicit config assertion catches that from here.
    """
    config = DAY_FIELDS[day]["selector"]["object"]
    assert config.get("multiple") is True, "a day is a list of events"
    assert set(config.get("fields", {})) == {"time", "temperature", "active"}


@pytest.mark.parametrize("day", WEEKDAYS)
def test_selector_row_preview_can_show_active(day):
    """A collapsed row must be able to show all three fields, `active` included.

    The frontend gives an object selector exactly two display knobs,
    `label_field` and `description_field`, each naming one field — no per-row
    icon, badge or colour exists. Setting both showed time and temperature and
    silently dropped `active`, so an event that had been switched off looked
    identical to one that was on.

    Declaring neither makes ha-selector-object fall back to joining every
    declared field with " · ", which is the only arrangement that fits three.
    """
    config = DAY_FIELDS[day]["selector"]["object"]
    assert "label_field" not in config
    assert "description_field" not in config


@pytest.mark.parametrize("day", WEEKDAYS)
def test_selector_rejects_unknown_event_key(day):
    sel = make_selector(DAY_FIELDS[day]["selector"])
    with pytest.raises(vol.Invalid):
        sel([{"time": "06:00:00", "temperature": 22.0, "bogus": 1}])


@pytest.mark.parametrize("day", WEEKDAYS)
def test_selector_enforces_device_temperature_limits(day):
    """UI limits must match the device's 5.0-35.0 C range, or the UI can submit
    a value the device rejects only after a BLE round trip."""
    sel = make_selector(DAY_FIELDS[day]["selector"])
    for bad in (4.5, 35.5):
        with pytest.raises(vol.Invalid):
            sel([{"time": "06:00:00", "temperature": bad}])
    for ok in (5, 35, 22.5):
        assert sel([{"time": "06:00:00", "temperature": ok}])


@pytest.mark.parametrize("day", WEEKDAYS)
def test_example_matches_what_the_selector_accepts(day):
    """The example shown in the UI must itself be valid input."""
    import json

    sel = make_selector(DAY_FIELDS[day]["selector"])
    assert sel(json.loads(DAY_FIELDS[day]["example"]))


def test_targets_filter_on_entity_only():
    """`target:` must carry an entity filter and NOT a device filter.

    A `device:` filter here reads like the way to make the device picker offer
    the thermostat, and it is not: hassfest rejects it outright ("services do
    not support device filters on target, use a device selector instead"), and
    zero of the ~490 target blocks in HA core use one.

    Device targets still work. The picker offers devices and areas regardless,
    narrowed to those holding a matching entity -- exactly like `light.turn_on`,
    whose target is nothing but `entity: domain: light`. What actually made
    device targets work is the service schema accepting them and
    `_resolve_coordinator` expanding them; see test_resolve.py.
    """
    for svc in ("get_home_program", "set_home_program"):
        target = SERVICES[svc]["target"]
        assert "entity" in target, svc
        assert "device" not in target, svc


def test_home_assistant_own_yaml_loader_parses_it():
    """PyYAML is not the loader HA uses.

    HA parses services.yaml with its own loader, which is stricter (duplicate
    keys are an error, for one). The anchors/aliases used to keep the seven
    weekday fields identical are the kind of thing worth confirming against the
    real loader rather than assuming.
    """
    from homeassistant.util.yaml import load_yaml_dict

    loaded = load_yaml_dict(str(SERVICES_YAML))
    assert set(loaded) == {"get_home_program", "set_home_program"}
    day_fields = loaded["set_home_program"]["fields"]["per_day"]["fields"]
    assert set(day_fields) == set(WEEKDAYS)
    # aliases must have expanded to real, identical content
    assert all(day_fields[d] == day_fields["monday"] for d in WEEKDAYS)
    assert day_fields["monday"]["selector"]["object"]["multiple"] is True


def test_strings_json_documents_every_field_and_section():
    """A field with no translation renders blank in the UI."""
    import json

    strings = json.loads(
        (SERVICES_YAML.parent / "strings.json").read_text()
    )["services"]["set_home_program"]
    assert set(strings["fields"]) == set(WEEKDAYS) | {"days", "events"}
    assert set(strings["sections"]) == {"per_day"}
    assert all(v.get("name") and v.get("description") for v in strings["fields"].values())
