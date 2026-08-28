"""Home program services: `eb300_ble.get_home_program` / `set_home_program`.

Domain-level services, not entity services — `eb300_ble` is the
service domain, not `climate`. Each call must resolve to exactly one climate
entity belonging to this integration; the handler resolves target -> entity ->
config entry -> coordinator itself, since there is no per-entity service
registration involved.

Because these are domain services, HA does not expand device/area/label targets
for us the way it does for entity platform services — `_resolve_coordinator`
does that expansion explicitly, so picking the device in the UI's target picker
works as well as picking the entity.
"""

from __future__ import annotations

import re
from datetime import time as dt_time
from typing import Any, cast

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids

from .const import DOMAIN, SERVICE_GET_HOME_PROGRAM, SERVICE_SET_HOME_PROGRAM, WEEKDAYS
from .coordinator import EB300Coordinator
from .eb300_ble.exceptions import ValidationError
from .eb300_ble.models import HomeProgram
from .eb300_ble.protocol import validate_home_program_event_temperature

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$")


def _valid_time(value: Any) -> str:
    """Normalise an event time to `HH:MM`.

    Seconds are accepted and dropped: HA's `time` selector submits `HH:MM:SS`,
    so rejecting that form would make the UI unable to submit anything, but the
    device's schedule resolution is one minute and has nowhere to put seconds.
    A `datetime.time` also gets here when a YAML author writes an unquoted
    sexagesimal-looking value.
    """
    if isinstance(value, dt_time):
        return f"{value.hour:02d}:{value.minute:02d}"
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise vol.Invalid(f"'{value}' is not a valid HH:MM time")
    hour, minute = value.split(":")[:2]
    return f"{hour}:{minute}"


# `active` mirrors what `get_home_program` reports, so a program can be read,
# edited and written back unchanged. It is also the only way to turn a slot off:
# the device keeps four slots per day always, and an inactive one still carries a
# time (it has to — `_validate_home_program` enforces chronological order across
# inactive events too), so "disable" means active=False, not a blank time.
_EVENT_SCHEMA = vol.Schema(
    {
        vol.Required("time"): _valid_time,
        vol.Required("temperature"): vol.Coerce(float),
        vol.Optional("active", default=True): cv.boolean,
    }
)

_DAY_SCHEMA = vol.All(cv.ensure_list, [_EVENT_SCHEMA], vol.Length(max=4))

GET_HOME_PROGRAM_SCHEMA = cv.make_entity_service_schema({})

# `days` + `events` is the multi-day shorthand; the per-weekday keys stay for
# days that differ. They arrive flat even though services.yaml groups the
# weekdays in a section — a section is UI grouping and nothing more.
#
# The two forms constrain each other (both halves of the shorthand required
# together, no day named twice), which voluptuous would express as a wrapping
# `vol.All`. That is done in the handler instead: a wrapper would drop the
# `_entity_service_schema` marker `make_entity_service_schema` sets, and these
# are user-input mistakes, which want `ServiceValidationError`'s clean message
# rather than a voluptuous traceback.
SET_HOME_PROGRAM_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Optional("days"): vol.All(cv.ensure_list, [vol.In(WEEKDAYS)], vol.Length(min=1)),
        vol.Optional("events"): _DAY_SCHEMA,
        **{vol.Optional(day): _DAY_SCHEMA for day in WEEKDAYS},
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the two domain-level services, once per HA process.

    A second config entry (a second thermostat) calls this again on its own
    setup; `has_service` makes that a no-op instead of a duplicate
    registration.
    """
    if hass.services.has_service(DOMAIN, SERVICE_GET_HOME_PROGRAM):
        return

    async def _async_get_home_program(call: ServiceCall) -> ServiceResponse:
        coordinator = _resolve_coordinator(hass, call)
        program = await coordinator.async_get_home_program()
        return _program_to_response(program)

    async def _async_set_home_program(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass, call)
        updates = _collect_updates(call)
        # Per-event range/exactness check on the given data alone, before any
        # BLE connection: catches the common invalid-schedule case (bad
        # temperature) with zero BLE traffic. Constraints that depend on the
        # device's existing schedule (chronological order after merging) can
        # only be caught once the current program has been read — that path
        # still raises ServiceValidationError, just after one GET instead of
        # before it (coordinator.async_set_home_program).
        for day_name, events in updates.items():
            for event in events:
                try:
                    validate_home_program_event_temperature(round(float(event["temperature"]) * 10))
                except ValidationError as exc:
                    raise ServiceValidationError(f"{day_name}: {exc}") from exc
        await coordinator.async_set_home_program(updates)

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_HOME_PROGRAM,
        _async_get_home_program,
        schema=GET_HOME_PROGRAM_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_HOME_PROGRAM,
        _async_set_home_program,
        schema=SET_HOME_PROGRAM_SCHEMA,
    )


def _collect_updates(call: ServiceCall) -> dict[str, list[dict[str, Any]]]:
    """Fold the `days` + `events` shorthand and the per-weekday fields into one map.

    Kept out of the handler so it can be tested without a coordinator or a
    device. Every failure here is a user-input mistake, hence
    `ServiceValidationError` throughout — HA renders those without a traceback.
    """
    updates: dict[str, list[dict[str, Any]]] = {
        day: call.data[day] for day in WEEKDAYS if day in call.data
    }

    days = call.data.get("days")
    events = call.data.get("events")
    if (days is None) != (events is None):
        missing, given = ("events", "days") if events is None else ("days", "events")
        raise ServiceValidationError(
            f"'{given}' was given without '{missing}' — the two go together: "
            "'events' is the schedule, 'days' is which weekdays to apply it to"
        )

    if days is not None and events is not None:
        # Naming a day both ways is ambiguous in a way that matters — the two
        # forms would write different schedules to the same day — so say so
        # rather than letting precedence decide silently.
        if clashes := sorted(set(days) & set(updates), key=WEEKDAYS.index):
            raise ServiceValidationError(
                f"{', '.join(clashes)} given twice: once in 'days' and once as its own "
                "field. Use one or the other for a given day"
            )
        for day in days:
            updates[day] = events

    if not updates:
        raise ServiceValidationError(
            "No weekday given — set 'days' and 'events', or fill in at least one "
            "weekday under Per day"
        )
    return updates


def _resolve_coordinator(hass: HomeAssistant, call: ServiceCall) -> EB300Coordinator:
    """Resolve any target form (entity / device / area / floor / label) to one coordinator.

    Resolution goes to the *config entry*, not to a single entity: a device
    target expands to every entity of that thermostat (climate, sensors,
    button), all of which share one entry and therefore one coordinator. That
    makes "one thermostat" the real constraint, instead of rejecting a device
    target for naming more than one entity.
    """
    selected = async_extract_referenced_entity_ids(hass, TargetSelection(call.data))
    registry = er.async_get(hass)

    # Explicitly named entities must be ours — a wrong entity_id is a user
    # mistake worth reporting, not something to filter away silently. Entities
    # pulled in indirectly (device/area/label) are filtered, since those targets
    # legitimately cover unrelated entities too.
    for entity_id in selected.referenced:
        entry = registry.async_get(entity_id)
        if entry is None or entry.platform != DOMAIN:
            raise ServiceValidationError(f"{entity_id} is not an eb300_ble entity")

    config_entry_ids: set[str] = set()
    for entity_id in selected.referenced | selected.indirectly_referenced:
        entry = registry.async_get(entity_id)
        if entry is not None and entry.platform == DOMAIN and entry.config_entry_id is not None:
            config_entry_ids.add(entry.config_entry_id)

    if len(config_entry_ids) != 1:
        raise ServiceValidationError(
            f"{call.service} targets exactly one EB-Therm 300 thermostat, "
            f"got {len(config_entry_ids)}"
        )

    config_entry = hass.config_entries.async_get_entry(next(iter(config_entry_ids)))
    if config_entry is None or not hasattr(config_entry, "runtime_data"):
        raise ServiceValidationError("The targeted EB-Therm 300 thermostat is not loaded")
    return cast(EB300Coordinator, config_entry.runtime_data)


def _program_to_response(program: HomeProgram) -> ServiceResponse:
    return {
        day_name: [
            {
                "time": f"{event.hour:02d}:{event.minute:02d}",
                "temperature": event.temperature_decideg / 10,
                "active": event.active,
            }
            for event in program.days[day_idx]
        ]
        for day_idx, day_name in enumerate(WEEKDAYS)
    }
