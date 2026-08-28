# Architecture

## Layers

```
Home Assistant
  │
  ├─ config_flow.py     discovery (manufacturer ID 0x0F93 / service UUID), PSK entry,
  │                     validated with a real handshake before the entry is created
  │
  ├─ coordinator.py     EB300Coordinator (DataUpdateCoordinator)
  │                     connect → handshake → read → disconnect, every cycle
  │                     all writes funnel through here too
  │
  ├─ entity.py          EB300Entity base: device info, availability
  ├─ climate.py         the control surface (setpoint, on/off, program preset)
  ├─ sensor.py  binary_sensor.py  switch.py  select.py  number.py  button.py
  ├─ services.py        get_home_program / set_home_program (domain-level)
  ├─ diagnostics.py     redacts the PSK
  │
  └─ eb300_ble/         vendored protocol library — no homeassistant imports
       ├─ client.py         EB300Client + BleakTransport
       ├─ protocol.py       pure codec: inner messages, counters, status/program structs
       ├─ crypto.py         HKDF derive, HMAC transcript, AES-GCM wrap/unwrap
       ├─ advertisement.py  normal advert parsing + burst-chunk reassembly
       ├─ models.py         ThermostatStatus, HomeProgram, DeviceInfo
       ├─ const.py          PIDs, UUIDs, enums, error codes
       └─ exceptions.py
```

## Why the library is vendored

`custom_components/eb300_ble/eb300_ble/` is the **only** copy of the library.
There is no `src/` tree, no PyPI package, and no sync script.

Home Assistant convention is to publish the protocol library separately and
list it in `manifest.json` `requirements`. That is required if an integration
is ever proposed for HA core. It is not required for a custom integration, and
it costs a PyPI release on every protocol change plus a window where the
component and the library disagree about the wire format.

The rule that makes vendoring safe: **the tests import the shipped code.**
`tests/lib/pyproject.toml` sets `pythonpath` to the component directory, so a
green library suite is a statement about the artifact users install, not about
a parallel source tree.

The library keeps a hard boundary anyway — it imports no `homeassistant`
module, uses only relative imports internally, and is tested with HA absent.
If it ever does need to move to PyPI, it can be lifted out unchanged.

## Connection strategy

Every poll cycle is a **full connect → handshake → read → disconnect**. The
integration holds no persistent BLE connection.

That is deliberate. A BLE proxy has roughly three connection slots, shared
across every Bluetooth device in the house. Holding one open permanently for a
thermostat that changes state every few minutes would be antisocial, and the
handshake is cheap enough (one round trip) that reconnecting is affordable at a
300-second default interval.

Three mechanisms keep this bounded:

| Mechanism | Value | Why |
|---|---|---|
| `_CONNECTION_SEMAPHORE` | 1, process-global | Multiple thermostats queue rather than race for slots. Stronger than a stagger-offset scheme, and makes one unnecessary. |
| `CONNECT_RETRY_ATTEMPTS` | 2 | Measured ~6 % single-attempt connect-timeout rate on real hardware. One retry takes the compound failure rate to ~0.36 %. |
| `BLE_OPERATION_TIMEOUT` | 45 s | Hard ceiling on one connect+operation. The only lever that actually bounds an unreachable device — see [HARDWARE_NOTES.md](HARDWARE_NOTES.md). |

## What one poll reads

1. `read_device_info()` — **first successful poll only**. Model, batch, serial
   and firmware never change post-pairing, so they are cached.
2. `read_status()` — PID `0x1004`, the big status struct: temperatures, relay
   state, power, active program, error flags, energy meter.
3. One **batched** request carrying four GETs — key lock, language, screensaver,
   user calibration. These are not part of the status struct. Batching keeps
   the cycle at one extra round trip rather than four.
4. RSSI, read from HA's Bluetooth stack rather than from the device.

The energy meter is taken from the field embedded in the `0x1004` status
struct, not from PID `0x1020` — that PID returns `NOT_IMPLEMENTED` on at least
one observed firmware, while the struct field is part of every poll anyway.

## Writes

All writes funnel through the coordinator, which:

- wraps failures in `HomeAssistantError` at the write boundary, so a failing
  service call shows the user a readable message instead of a traceback;
- refreshes immediately after a successful write, so state reflects the device
  rather than an assumption;
- normalizes `BleakError` and bare `TimeoutError` into the integration's own
  exception types.

The `climate` setpoint additionally debounces (see
[HARDWARE_NOTES.md](HARDWARE_NOTES.md) — the design there is load-bearing and
was arrived at the hard way).

The default temperature write path is PID `0x10D0` (Override Temperature), not
`0x1082` (Manual Control Temp). `0x1082` is silently inert while the Home
program is active.

## The schedule is services, not entities

The weekly Home program is 7 days × 4 events, each with a time, a temperature
and an active flag. There is no HA entity shape that fits it: `climate`
presets are single values, and the `schedule` helper is HA-side state rather
than a device write.

So it is exposed as two domain-level services, `get_home_program` and
`set_home_program`, with `get`'s output deliberately shaped to be valid `set`
input.

Being **domain-level** rather than entity services has one consequence worth
knowing: HA does not run entity-platform target expansion for them, so a raw
`device_id` reaches the schema. Both schemas therefore use
`cv.make_entity_service_schema` and expand targets themselves via
`homeassistant.helpers.target`, resolving to the **config entry** — a device
target legitimately expands to every entity of that thermostat, all of which
share one coordinator.

## Test environments

Two, deliberately separate:

| | `tests/lib` | `tests/ha` |
|---|---|---|
| Tests | The vendored library | The Home Assistant glue |
| `homeassistant` installed | **No** | Yes, version-pinned |
| Python | 3.12+ | 3.14+ (whatever HA needs) |
| Device double | `fakes.py::FakeEB300` | HA test harness |
| Count | 103 | 129 |

They cannot merge. The library suite's value is precisely that it proves the
protocol code works with HA absent. The HA suite's value is precisely that it
imports every component module against a **real** `homeassistant` package —
the only check that catches HA relocating a helper between releases, which has
broken this integration at load time before.

See [DEVELOPMENT.md](DEVELOPMENT.md) for how to run them.
