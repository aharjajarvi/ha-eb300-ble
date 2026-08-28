# Ebeco EB-Therm 300 (BLE) — Home Assistant integration

[![Validate](https://github.com/aharjajarvi/ha-eb300-ble/actions/workflows/validate.yml/badge.svg)](https://github.com/aharjajarvi/ha-eb300-ble/actions/workflows/validate.yml)
[![Tests](https://github.com/aharjajarvi/ha-eb300-ble/actions/workflows/tests.yml/badge.svg)](https://github.com/aharjajarvi/ha-eb300-ble/actions/workflows/tests.yml)
[![hacs](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz)

> [!NOTE]
> **Built with AI assistance.** This integration was written largely by an AI
> coding assistant (Claude), directed and reviewed by a human maintainer.
>
> What that does *not* mean: it is not untested or unverified. Every feature
> here was exercised against a real EB-Therm 300 — including the write paths,
> run attended with device state snapshotted and restored — and the repo
> carries 267 automated tests that run without hardware.
>
> What it does mean: it has been validated on exactly **one** device
> (firmware 1.2, batch 2603) by **one** person, and no third party has
> reviewed the code. It controls a heating system in your home. Read
> [`docs/HARDWARE_NOTES.md`](docs/HARDWARE_NOTES.md) — including its
> *"Two known gaps, stated honestly"* section — and satisfy yourself before
> pointing automations at it.

Local Bluetooth control of the **Ebeco EB-Therm 300** underfloor heating
thermostat, over Ebeco's own Open Local API. No cloud, no account, no polling
of Ebeco's servers — Home Assistant talks straight to the thermostat over an
encrypted BLE session.

Everything runs locally: `iot_class` is `local_polling`.

---

## Before you install: you need a PSK

The Open API channel is encrypted with a 32-byte pre-shared key. You enable it
yourself in the Ebeco Connect app, and Ebeco emails you the key:

> **Ebeco Connect → your device → Settings → Functions → Enable local API**
>
> The key arrives by email, at the address on your Ebeco account.

That is the whole process — no support ticket, no waiting on Ebeco to
provision anything by hand.

Two things worth knowing:

- **The key is per device.** Two thermostats mean enabling local API twice and
  getting two different keys.
- **Nothing works without it.** With local API disabled, the handshake fails
  immediately — the thermostat answers with an error instead of a session, and
  setup stops at the PSK step. Retrying, re-pairing or moving the device closer
  will not help.

You can confirm a device is enabled before installing anything. The EB300's BLE
advertisement carries an encryption-flags byte, and **bit 5 set means the Open
API key is provisioned**. `tools/scan.py` in this repo decodes and prints
exactly that, and needs no key of its own.

Paste the key into Home Assistant exactly as it arrives — **base64**, 44
characters for 32 bytes. The config flow validates it with a real handshake
against the thermostat before creating the entry, so a wrong or mistyped key
fails during setup rather than silently later.

### Other requirements

| | |
|---|---|
| Home Assistant | **2026.8.0** or newer |
| Radio | A Bluetooth adapter on the HA host, **or** an ESPHome Bluetooth proxy in range. Proxies are tested and work. |
| Hardware | Ebeco EB-Therm 300. Developed against firmware 1.2, batch 2603. |

---

## Install

### HACS (recommended)

1. HACS → three-dot menu → **Custom repositories**
2. Repository: `https://github.com/aharjajarvi/ha-eb300-ble`, category:
   **Integration**
3. Download it, then restart Home Assistant.

### Manual

Copy `custom_components/eb300_ble/` into your Home Assistant `config/custom_components/`
directory and restart.

### Set it up

The thermostat is **discovered automatically** over Bluetooth — you should get
a notification offering to configure it. If not, go to
**Settings → Devices & Services → Add Integration → Ebeco EB-Therm 300** and
pick it from the list of visible devices (or type its MAC).

Then paste the base64 PSK. That's the whole setup.

---

## Entities

One device. A `climate` entity for control, plus twenty sensor, switch and
settings entities — and two optional derived sensors, power and energy.

### Control

| Entity | Platform | Notes |
|---|---|---|
| Thermostat | `climate` | Target temperature (5–35 °C, 0.5 °C steps), heat/off, and a preset for the active program (`manual` / `home`). |
| Key lock | `switch` | Locks the physical buttons on the unit. |
| Language | `select` | Display language. |
| Screensaver | `select` | See [the note below](#screensaver-has-fewer-real-modes-than-the-protocol) — on the tested firmware only `off` differs visually. |
| Room sensor calibration | `number` | −5.0 … +5.0 °C offset. |
| Floor sensor calibration | `number` | −5.0 … +5.0 °C offset. |
| Sync clock | `button` | Pushes Home Assistant's time to the thermostat. |

### Readings

| Entity | Platform | Notes |
|---|---|---|
| Floor temperature | `sensor` | `unknown` when the thermostat cannot read that sensor — see [No floor sensor](#no-floor-sensor-installed-the-floor-reading-goes-unknown). |
| Room temperature | `sensor` | `unknown` on a room-sensor fault, same rule. |
| Target temperature | `sensor` | The setpoint currently in force. |
| Program | `sensor` | `manual` or `home`. |
| Heating time | `sensor` | Cumulative relay-on hours, read from the device. |
| Energy | `sensor` | **Only created if you set a heating element wattage.** Derived from the device's relay-on minute counter × wattage — the thermostat has no energy metering of its own. |
| Power | `sensor` | **Only created if you set a heating element wattage.** The wattage while the relay is closed, 0 W while it is open. [Simulated](#power-and-energy-are-simulated), not measured. |
| Heating | `binary_sensor` | Relay state. |
| Powered on | `binary_sensor` | Whether the thermostat itself is switched on — not to be confused with the `Power` sensor above. |
| Problem | `binary_sensor` | Any active error flag. |
| Relay temperature | `sensor` | Diagnostic. |
| Time to target | `sensor` | Diagnostic. |
| Signal strength | `sensor` | Diagnostic, disabled by default. |
| Limited by limiting sensor | `binary_sensor` | Diagnostic. |
| Room / floor sensor fault | `binary_sensor` | Diagnostic. |

### Options

**Settings → Devices & Services → Ebeco EB-Therm 300 → Configure**

| Option | Default | Notes |
|---|---|---|
| Poll interval | 300 s | 60–1800 s. Each poll is a full BLE connect; see [Bluetooth budget](#bluetooth-budget) before lowering it. |
| Heating element wattage | 0 (off) | Enables the derived `Power` and `Energy` sensors. Both are created together, or not at all. |
| Use room sensor for climate | off | By default the `climate` entity reports the **floor** sensor as current temperature. Turn this on if no floor sensor is wired. |

---

## Schedule (the Home program)

The thermostat's weekly schedule — 7 days × up to 4 events each — is exposed as
two services rather than as entities.

### `eb300_ble.get_home_program`

Returns the current schedule. Its output is deliberately shaped so it can be
pasted straight back into `set_home_program`.

```yaml
action: eb300_ble.get_home_program
target:
  entity_id: climate.hallway
```

### `eb300_ble.set_home_program`

Two ways in, both optional and combinable:

- **`days` + `events`** — apply one event list to several weekdays at once.
  This is what most real edits look like.
- **per-weekday fields** (`monday`, `tuesday`, …) — for days that differ.

A day may not be named both ways in one call; the service rejects that rather
than silently picking a winner.

```yaml
action: eb300_ble.set_home_program
target:
  entity_id: climate.hallway
data:
  days: [monday, tuesday, wednesday, thursday, friday]
  events:
    - { time: "06:00", temperature: 22.0 }
    - { time: "08:00", temperature: 17.0 }
    - { time: "15:30", temperature: 22.0 }
    - { time: "22:00", temperature: 17.0 }
  saturday:
    - { time: "08:00", temperature: 22.0 }
    - { time: "23:00", temperature: 17.0 }
```

Two rules the device enforces, and so does this integration:

- **Events must be in chronological order within a day** — including events
  that are switched off.
- **A slot is disabled with `active: false`, never by blanking its time.** The
  device always stores 4 slots per day; an inactive one still needs a valid,
  in-order time. It keeps its time and temperature and simply stops firing.

```yaml
  monday:
    - { time: "06:00", temperature: 22.0 }
    - { time: "22:00", temperature: 17.0 }
    - { time: "23:00", temperature: 17.0, active: false }
    - { time: "23:30", temperature: 17.0, active: false }
```

In the UI's event editor each row shows all three fields —
`23:00:00 · 17 °C · false` — so a slot that has been switched off is visible
without opening it. A row with no flag shown is active; that is the default
when the key is omitted.

Setting the schedule does **not** switch the thermostat into the Home program —
change the `climate` entity's preset to `home` for that.

---

## Automation example

```yaml
alias: Drop the floor overnight
triggers:
  - trigger: time
    at: "23:00:00"
actions:
  - action: climate.set_temperature
    target:
      entity_id: climate.hallway
    data:
      temperature: 17
```

---

## Things worth knowing

### Bluetooth budget

Every poll and every write is a full BLE connect. A Bluetooth proxy has only a
few connection slots, shared with **every** BLE device in the house — not just
this one. Dropping the poll interval from 300 s to 60 s is a 5× increase in
connection attempts. That is fine for a single thermostat, but worth
remembering before adding a second one or a pile of other BLE devices.

The integration serializes its own connections so multiple thermostats never
contend with each other, and it caps any single operation at 45 seconds so an
unreachable device cannot occupy a proxy slot for minutes.

### Writes are debounced

Dragging the target-temperature slider fires a burst of `set_temperature`
calls. Only the last value in a burst is written, one second after you stop
moving. The UI shows the new value immediately; if the write then fails, the
displayed value reverts to what the device actually holds.

### Screensaver has fewer real modes than the protocol

The protocol defines four screensaver modes; the `select` exposes all four and
all four are correctly stored and read back. But on the tested firmware only
`off` looks different — `time_temp`, `time_date` and `temperature` all just
show the current temperature. This is a device limitation, not an integration
bug, and it is why Ebeco's own app only offers two of the four. See
[docs/HARDWARE_NOTES.md](docs/HARDWARE_NOTES.md).

### Power and energy are simulated

The EB-Therm 300 measures neither. Both sensors are the heating element
wattage you entered, multiplied by what the relay is doing:

| Sensor | Derived from | Caveat |
|---|---|---|
| `Power` (W) | the relay's state **at poll time** | A burst of heating that falls between two polls is invisible; one that spans a poll reads as if it ran the whole interval. |
| `Energy` (kWh) | the device's own cumulative **relay-on minute counter** | Misses nothing — the counter runs on the thermostat, so the poll interval does not matter. |

So `Power` is for seeing what the floor is doing right now, and `Energy` is
what you point the energy dashboard at. Reading totals off the power sensor
instead will undercount or overcount, depending on the poll interval.

Both assume the element draws its rated wattage whenever the relay is closed,
which is what a resistive heating cable does. Neither reacts to mains voltage,
and an inaccurate wattage propagates straight through. If you want measured
figures, a smart plug or an energy meter on the circuit is the answer.

### No floor sensor installed: the floor reading goes `unknown`

Running the thermostat on its room sensor alone, with nothing wired to the
floor-sensor terminals, is a supported installation — set the control method to
the room sensor in the Ebeco app and the app stops reporting an error.

The thermostat still sends a floor temperature in that state, and **it is a
placeholder: a constant 20.0 °C**. It is not a measurement, and 20.0 °C is
plausible enough to be believed — on the tested firmware the reading snapped
from 25.7 °C to exactly 20.0 °C the moment the sensor was disconnected, and
stayed there.

This integration suppresses it. When the thermostat reports an error for a
sensor, that sensor's entity is `unknown` rather than a fabricated number, so
nothing fake reaches history, statistics or your automations:

| Entity | With a working floor sensor | With none wired |
|---|---|---|
| `sensor.…_floor_temperature` | the measurement | `unknown` |
| `binary_sensor.…_floor_sensor_fault` | `off` | `on` |
| `climate.…` current temperature | the floor reading | `unknown`, **unless** you turn on *Use room sensor for climate* |

So on a room-sensor-only installation, turn on **Use room sensor for climate**
in the integration options — otherwise the thermostat card has no current
temperature to show. The `Floor sensor fault` binary sensor staying `on` is
correct and expected there; the exact code the device reports is in the
diagnostics download if you want to tell a disconnected sensor from a broken
one.

### The display does not show the setpoint

If you change the target temperature from Home Assistant and then look at the
thermostat expecting to see the new number: it shows the **current** measured
temperature, not the setpoint. Use the Ebeco app or Home Assistant to confirm
a write landed.

---

## Troubleshooting

**Setup fails with "Handshake failed — the PSK was rejected."**
The key is wrong, or it belongs to a different thermostat. Keys are per-device.
Re-check the email Ebeco sent when you enabled local API for *this* device, and
paste it whole — it is base64, 44 characters.

**Setup fails with "Could not connect."**
The device is out of range or powered off. Note that the thermostat advertises
continuously while powered — it does *not* need the Ebeco app to be open, and
it does not sleep its radio. If nothing sees it, it is a range or adapter
problem. (The reverse *does* bite — see the next entry.)

> [!IMPORTANT]
> **Close the Ebeco app properly, or Home Assistant cannot reach the thermostat.**
>
> While the Ebeco Connect app on your phone is connected to the thermostat,
> every Home Assistant poll fails and the entities go unavailable — often for
> minutes at a stretch, coming back only when the app lets go. The thermostat
> appears to serve **one Bluetooth connection at a time**, and the app is
> holding it.
>
> It looks like a range problem and is not. The device keeps advertising while
> the app is connected, so it is still discovered and still shows up in scans.
> Only the *connect* fails.
>
> | Look at the thermostat | What it means |
> |---|---|
> | Bluetooth glyph **lit** | Something is connected right now — that is the app, not HA |
> | Bluetooth glyph **off** | The link is free; the next poll will land |
>
> **Backgrounding the app is not closing it.** Locking the phone, switching
> apps, or leaving the app on screen with the phone in your pocket all keep the
> connection open. Swipe the app out of the app switcher (or force-stop it),
> confirm the glyph on the thermostat goes out, and Home Assistant recovers on
> its next poll — up to one poll interval, 5 minutes by default. Reloading the
> integration polls straight away if you would rather not wait.
>
> Being in and out of the app while debugging something in Home Assistant is
> exactly the situation that produces this, and it is easy to blame the
> integration for it.

**The device is never discovered.**
Confirm HA's Bluetooth integration is loaded and an adapter or proxy is in
range. `tools/scan.py` can confirm the device is advertising and whether bit 5
(Open API PSK provisioned) is set.

**Entities go unavailable, then come back.**
Expected if the device is briefly out of reach. A failed poll does not
immediately mark the device unavailable; it takes a few cycles. If it keeps
happening, check whether the Ebeco app is open on a phone in the house — that
is the common cause, and the Bluetooth glyph on the thermostat tells you.

**Filing a bug.** Include the diagnostics download from the device page — the
PSK, the device address and the serial are redacted automatically — plus your
HA version and whether you use a proxy.

---

## Development

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for environments and test
commands, [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the pieces fit,
[docs/PROTOCOL.md](docs/PROTOCOL.md) for implementation notes on the wire
protocol, and [docs/HARDWARE_NOTES.md](docs/HARDWARE_NOTES.md) for firmware
behaviour found on real hardware.

Two test suites, 267 tests, no thermostat required:

```sh
./tests/lib/run.sh      # library: protocol, crypto, advertisements, client
./tests/ha/run.sh       # the Home Assistant glue, against a real HA package
```

---

## Credits and legal

Implements the **Ebeco EB300 Open Local API, Rev 1.0** — *source: Ebeco AB*,
<https://www.ebeco.com/en/solutions/underfloor-heating/ebeco-open-api>.

"Ebeco" and "EB-Therm" are trademarks of Ebeco AB. This is an independent,
community-maintained integration: **not affiliated with, endorsed by, or
supported by Ebeco AB**, and not part of Home Assistant core. Available
features vary by model and firmware, and some device functions are reserved
for Ebeco's own apps and backend.

Licensed under the [MIT License](LICENSE). See [NOTICE](NOTICE) for
attributions.
