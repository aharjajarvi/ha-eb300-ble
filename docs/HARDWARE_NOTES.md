# Hardware and firmware notes

Behaviour found by running this integration against a real EB-Therm 300, that
the protocol specification does not describe. Reference device: **firmware 1.2,
batch 2603**.

Nothing here is a bug in this integration. It is the list of things that cost
real debugging time, kept so the next person does not have to pay for them
again.

---

## Device behaviour

### The screensaver has fewer real modes than the protocol

The protocol defines four `ScreensaverType` values: `off`, `time_temp`,
`time_date`, `temperature`. All four are correctly stored and read back — the
SET/GET round trip is completely clean for every one of them.

But only **`off` is visually distinct**. `time_temp`, `time_date` and
`temperature` all render identically: the display shows the current
temperature. The device does not implement a time or date screensaver on this
firmware.

This is why Ebeco's own app exposes only two options ("Only temperature" /
"None"). Its authors already knew.

**Decision taken here:** keep all four in the `select`. They are valid,
harmless, correctly stored, and a future firmware could plausibly implement the
other two. Trimming the list now would only have to be undone later.

### The display shows the current temperature, not the setpoint

If you set a target temperature from Home Assistant and walk over to the
thermostat to confirm, you will see the measured temperature, not the value you
just set. The device does show a confirmation checkmark glyph when a write
lands, but not the number.

This matters for anyone trying to verify writes: the device's own display
cannot confirm a setpoint. Use the Ebeco app, or read it back over BLE.

### `0x1082` is silently inert while the Home program is active

PID `0x1082` (Manual Control Temp) is the obvious way to set a temperature.
While the Home program is the active program, writing it does **nothing** — the
write is accepted, no error is returned, and the setpoint does not change.

PID `0x10D0` (Override Temperature) takes effect immediately regardless of the
active program, and is what this integration uses by default.

### Inactive schedule slots hold real values, not zeros

The device always stores 4 slots per weekday. A slot is disabled with its
active flag; it keeps a real, in-order time and a real temperature.

A validator here once assumed disabled slots would be zeroed and consequently
**rejected the device's own factory-shipped schedule** as invalid. Chronological
ordering is enforced across inactive slots too, which is why a slot cannot be
disabled by blanking its time.

### It advertises continuously

The EB300 does not sleep its radio and does not need the Ebeco app open or
connected to be discoverable. Verified across a 276-second window covering app
closed, app open and connected, and a physical button press: 29 sightings,
continuous throughout.

Worth stating because the opposite appeared to be true at first — a phone BLE
scanner seemed to show the device only while the app was running. That was an
artifact of the scanner UI. Had it been real, the device would have been
unconnectable to anything and a poll-based integration impossible.

**Advertising is not connectability**, though: see *"The phone app holds the
only connection"* below. A device that is discovered, in range and advertising
happily can still refuse every connect, and that is the failure mode users
actually hit.

### A sensor the device cannot read reports 20.0 °C, not an error

The `0x1004` status struct always carries a temperature for both sensors, even
one that is not wired. The value in it is a **placeholder: exactly 200
decidegrees**.

Observed 2026-08-28 on the reference device, floor sensor disconnected and the
control method switched to the room sensor:

| | Before | After |
|---|---|---|
| `floor_temperature` | 25.7 °C, tracking normally | **20.0 °C**, and pinned there for hours |
| `floor_sensor_error` | 0 | non-zero |
| `error_flags` | no floor bits | still no floor bits |
| `in_error_state` | false | false |

Two things make this worth a note:

- **20.0 °C is a plausible floor temperature.** Nothing about the value itself
  says "not a measurement". Only `floor_sensor_error` does.
- **It is a normal state, not a fault.** A thermostat run on the room sensor
  alone is a supported installation; the Ebeco app stops reporting an error
  once its control method is set to the room sensor. The device still flags the
  unwired sensor in its per-sensor error byte, but not in `ErrorFlags` — so the
  `Problem` binary sensor stays `off`, correctly.

The integration therefore reports `None` (state `unknown`) for a temperature
whose sensor error byte is non-zero — floor and room alike, and for the
`climate` entity's current temperature — rather than letting a constant
fiction into recorder, statistics and automations. Any non-zero code counts;
none of them are enumerated, because "not a measurement" is the only
distinction that matters at that point. `SensorErrorCode` is in the
diagnostics download for anyone who needs to tell a disconnected sensor from a
broken one.

### Device info is not what you would guess

- The serial-number PID returns the device's **MAC as a string**, not a
  distinct serial.
- `firmware_version` is `"1.2"` — an opaque string, not semver.
- PID `0x1020` (Energy Meter) returns `NOT_IMPLEMENTED` on this firmware. The
  energy figure is in the `0x1004` status struct anyway.

### Home Assistant may report the name differently than the device broadcasts

The device broadcasts `EB300`; HA/bleak has been observed reporting it as
`EBECO.EB300`. **Do not match on the name.** The config flow matches on
manufacturer ID (`0x0F93`) instead.

---

## Bluetooth behaviour

### Connect reliability

Measured on real hardware: roughly a **6 % single-attempt connect-timeout
rate**. That is why `CONNECT_RETRY_ATTEMPTS = 2` — one retry takes the compound
failure rate to about 0.36 %.

A typical successful poll takes ~1 second. Slow-but-successful outliers of
10.6 s, 27 s and 40.6 s are on record. These are normal BLE variance, not
symptoms.

### An unreachable device can starve a whole proxy

This one is worth understanding before tuning any timeouts.

`bleak_retry_connector.establish_connection()`'s `max_attempts` parameter gates
only its *timeouts and connect-errors* counters. What an offline device actually
produces — `BleakDeviceNotFoundError` / `BleakNotFoundError` — is classified as
**transient**, and gated instead by its module constant
`MAX_TRANSIENT_ERRORS = 9`, at up to `BLEAK_SAFETY_TIMEOUT = 60 s` each plus
out-of-slots backoff. (Verified against `bleak_retry_connector` 4.6.3.)

`max_attempts` cannot bound that. Single operations of **190 s and 319 s** were
observed occupying a shared BLE proxy connection slot, producing
`habluetooth.auto_scheduler` warnings about *unrelated* devices unable to get a
scanner slot.

The only lever that actually works is a hard ceiling on our side:
`BLE_OPERATION_TIMEOUT = 45 s`, sized well above the slow-but-successful
outliers and well below the pathological cases. `MAX_TRANSIENT_ERRORS` is not
exposed as a parameter and is shared module state — monkeypatching it would
affect every other Bluetooth integration in the process.

### The phone app holds the only connection

While the Ebeco Connect app is connected, Home Assistant's polls fail and the
entities go unavailable. Observed 2026-08-28 as repeated multi-minute
unavailable windows — 19:40–19:46, 20:01–20:07, 20:21–20:35 — on a device that
was in range and advertising throughout, during an evening of going in and out
of the app.

The tell is on the thermostat itself: **the Bluetooth glyph on the display is
lit while something is connected**, and goes out when the link is released.

Backgrounding the app does not release it. The phone keeps the link across a
screen lock and an app switch; only closing the app properly — swiped out of
the app switcher, or force-stopped — frees it, after which the next poll
succeeds.

**The mechanism is inferred, not proven.** The observations fit a device that
serves one connection at a time, but nobody has deliberately held two links
open to confirm that is the limit. What is established is the symptom, the
tell, and the cure — and that neither range nor the integration is at fault.

This is worth knowing before debugging anything else: sitting with the app open
while poking at Home Assistant is exactly the setup that produces it, and it
looks like a flaky integration.

### Poll interval is a shared-resource decision

A proxy has roughly three connection slots, shared with every BLE device in the
house. Going from a 300 s to a 60 s poll interval is a 5× increase in
connection attempts per hour. Fine for one thermostat; think again before
adding a second, or a pile of other BLE devices.

---

## Write-path design rules

These four rules came out of one test case — "write while the device is
unreachable" — which took five attended cycles to close. Four distinct bugs
were found, **three of them introduced by the previous cycle's fix**. If you
change this code, change it knowing why it looks the way it does.

1. **One cancellable task owns both the debounce wait and the write.**
   Not a timer callback plus a separate write task. The split version needed
   two cancel paths and put the flush callback on a worker thread, which broke
   `async_write_ha_state`'s thread-safety contract. A newer edit must be able
   to preempt an in-flight older one with a single cancel.

   Without cancellation, a write stuck retrying against an unreachable device
   (1–2+ minutes) is not stopped by a newer edit. Whichever write happens to
   catch the device reconnecting wins — which can silently apply a stale,
   superseded value. Observed on hardware.

2. **Store the pending optimistic value *before* cancelling the previous
   task.** A task being cancelled is suspended at an `await` and cannot run
   another statement in between, so it can never clear a value stored on the
   preceding line. The reverse order races.

3. **Raise `HomeAssistantError` at the coordinator write boundary.** HA treats
   any other exception escaping a service call as an integration bug: full
   traceback at ERROR, no readable message for the user. Doing this once at the
   boundary beats doing it in each of the seven entity write methods.

4. **Normalize `BleakError` and bare `TimeoutError`.** Both leak through
   otherwise. `str(TimeoutError())` is empty, so any message built from it
   trails off after the colon — fall back to the type name.

Result after these four: zero thread-safety warnings, setpoint reverts to truth
in ~1.0 s, no unhandled tracebacks, no stale write landing after a reconnect.

### Two known gaps, stated honestly

- **Burst cancellation is not hardware-verified.** Three rapid edits inside the
  debounce window are covered by unit tests, but that path has never been
  exercised against a real device.
- **Availability is not flipped on a failed write.** Doing so would cut the
  observed ~4-minute lag before a device shows as unavailable to roughly 2.
  Deliberately not implemented — it trades a faster unavailable signal for more
  UI flapping on transient failures, and that trade has not been made.

---

## Home Assistant integration gotchas

Not device behaviour, but the same category: things that broke a deploy.

### Import paths move between HA releases

`async_extract_referenced_entity_ids` moved from `homeassistant.helpers.service`
to `homeassistant.helpers.target`. The integration failed to load at all. This
class of breakage accounted for three failed deploys.

`py_compile` does not catch it. Only importing every module against a real
`homeassistant` package does — which is the entire reason `tests/ha` exists,
separately from `tests/lib`. **Pin `tests/ha/pyproject.toml` forward on every
HA upgrade.**

### Domain-level services do not get target expansion

`get_home_program` / `set_home_program` are domain-level, not entity services.
HA does not run entity-platform target expansion for them, so a raw `device_id`
reaches the service schema directly — a schema requiring `entity_id` rejects
device targets outright, while entity targets work by coincidence.

Use `cv.make_entity_service_schema` and expand targets yourself. Resolve to the
**config entry**, not to a single entity: a device target legitimately expands
to every entity of that thermostat, all sharing one coordinator.

Also add `device: integration: <domain>` under `target:` in `services.yaml`, or
the device picker will not offer the device at all.

### Braces in `strings.json` break the frontend

HA's frontend runs translated strings through **intl-messageformat**, where
`{...}` delimits a placeholder. A description reading
`Up to 4 events: [{time: "HH:MM", ...}]` makes the parser read `time` as an
argument name, hit `:` where it wants `}` or `,`, and fail with
`MALFORMED_ARGUMENT` — once per field.

**Rule:** any literal brace in `strings.json` / `translations/*.json` must be a
real HA placeholder. Concrete JSON examples belong in `services.yaml`'s
`example:` key, which is *not* run through the translation layer.

### An `object` selector needs `multiple: true` to hold a list

`selector: object: {}` defaults to `multiple: false`, which rejects a list
outright and leaves the UI with no way to enter one. Declaring `fields:` under
the selector also swaps the raw-YAML box for a real per-event editor — a time
picker and a temperature box — which is what makes the field usable at all.

Declare every key the service accepts, including optional ones: the object
selector rejects any key it does not declare, so an undeclared `active` key
means `get_home_program`'s own output cannot be pasted back into
`set_home_program`.
