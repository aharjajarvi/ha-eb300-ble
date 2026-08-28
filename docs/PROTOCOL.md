# Protocol notes

**The wire format is not documented here.** It is specified by Ebeco, publicly:

> **Ebeco EB300 Open Local API, Rev 1.0** — *source: Ebeco AB*
> <https://www.ebeco.com/en/solutions/underfloor-heating/ebeco-open-api>

Read that first. Ebeco's terms ask that they be named as the source, and note
that available features vary by model and firmware, and that some functions are
reserved for their own apps and backend.

This document covers only what implementing it taught us that the specification
does not say. Section references like "§5.7" point into Ebeco's document.

---

## Shape of the implementation

```
crypto.py         HKDF key derivation, HMAC transcript, AES-GCM wrap/unwrap
protocol.py       pure codec — no I/O, no crypto: inner messages, counters,
                  the status struct, the home-program struct
client.py         EB300Client (session state machine) + BleakTransport
advertisement.py  advert parsing, including burst-chunk reassembly
```

`protocol.py` being pure — bytes in, dataclasses out — is what makes the whole
thing testable without a radio. `EB300Client` talks to a `Transport` protocol;
`tests/lib/fakes.py::FakeEB300` implements the device side entirely in Python.
`BleakTransport` is the only part that needs hardware.

---

## Handshake

### A device with no PSK answers with an error, not a nonce

If the Open API key slot is not provisioned, step 1 of the handshake returns
**`0x4F 0x07`** (`INVALID_PARAMETER`) instead of `0x41` + the server nonce.

The trap: that response is two bytes, and a client that does not check the
header will happily treat it as a server nonce. Key derivation then proceeds
with garbage and fails several steps later at the HMAC comparison — reported as
a crypto mismatch, which sends you looking at your AES implementation instead
of at the actual cause, which is "this device has no key".

`client.py` checks the header explicitly and raises a distinct error. It is
worth doing the same in any other implementation.

You can detect this condition *before* connecting: bit 5 of the advertisement's
encryption-flags byte is set when an Open API PSK is provisioned.

### Key derivation

Session key and HMAC key are both derived from the PSK with HKDF, salted with
the literal ASCII string `bg22-psk-salt-ebeco`, over the two nonces. The vectors
in `tests/lib/test_crypto.py` pin the derivation — if you refactor `crypto.py`,
those are what tell you the key schedule still matches the device.

---

## Counters and sessions

Message counters are monotonic within a session and are part of the AEAD
construction, so they cannot be reset or reused. The library tracks them in
`protocol.py` and raises `SessionExhaustedError` when the counter space
approaches its limit (`COUNTER_REHANDSHAKE_THRESHOLD`) — at which point the
correct move is a fresh handshake, not a wrap-around.

In practice a connect-per-poll design never gets close, because each poll is
its own session. It matters for any long-lived-connection implementation.

---

## Advertisements

The EB300 advertises **continuously while powered**. It does not sleep its
radio, and it does not need the Ebeco Connect app to be open or connected. This
was verified over a 276-second window with the app closed, the app open, and
during a physical button press: 29 sightings, continuous throughout.

That matters because it was not obvious. A phone BLE scanner appeared to only
show the device while the app was running, which suggested the radio was gated
on app activity — which, if true, would have made the device unconnectable to
anything and killed the whole poll-based design. It was an artifact of the
phone's scanner UI.

### Offsets depend on where you start counting

Ebeco's document gives offsets into the **raw advertisement frame** — package
type at offset 7, encryption flags at offset 14. Bleak (and therefore Home
Assistant) hands you the manufacturer-specific data with the AD header and the
2-byte company ID already stripped, which shifts everything by 7.

`advertisement.py` works in bleak's frame, so its offsets are relative to the
**16-byte manufacturer payload**:

| Offset | Field |
|---|---|
| 0 | Package type |
| 1–6 | Device MAC (6 raw bytes) |
| 7 | Encryption flags |
| 8–9 | Event flags (`<H`) |
| 10–11 | Regulator error flags (`<H`) |

Two advertisement types, by package type:

| Value | Meaning |
|---|---|
| `0x01` | Normal advertisement |
| `0x03` | Burst data chunk (status-advertisement mode) |

And in the encryption-flags byte:

| Bit | Mask | Meaning |
|---|---|---|
| 0 | `0x01` | Channel 1 (Backend) PSK provisioned |
| 3 | `0x08` | Channel 2 (User) PSK provisioned |
| 4 | `0x10` | Channel 3 (Gateway) PSK provisioned |
| **5** | `0x20` | **Open API PSK provisioned** |
| 6 | `0x40` | Advert channel is Open API |
| 7 | `0x80` | Status-advertisement mode active |

Bit 5 is the one to check before attempting a connection.

---

## Device info quirks

Two things `read_device_info()` returns that are not what you would guess:

- **The serial-number PID (`0x0003`) returns the device's own MAC as a
  string** — e.g. `"AA:BB:CC:DD:EE:FF"` — not a distinct serial number. Fine
  as a `unique_id`, but do not expect it to differ from the MAC.
- **`firmware_version` is an opaque string.** The observed value is `"1.2"`,
  not the three-component `"1.2.0"` the spec's example format suggests. Do not
  parse it as semver.

Separately, **PID `0x1020` (Energy Meter) returns `NOT_IMPLEMENTED` (error 9)**
on at least one shipping firmware. Any capability probe should treat error 9 as
"not available on this firmware", alongside the `UNKNOWN_PID` / `UNAUTHORIZED`
cases. The energy figure is available anyway as a field inside the `0x1004`
status struct, which is what this integration uses.

---

## The home program struct

112 bytes: 7 days × 4 events × 4 bytes.

Three rules the device enforces, all of which produced bugs before they were
understood:

1. **All four slots always exist.** There is no "empty" slot. A slot is
   switched off with its active flag; it keeps a real time and a real
   temperature.

2. **Chronological order is validated across inactive slots too.** An inactive
   slot with an out-of-order time is rejected by the device. This is why a slot
   cannot be disabled by blanking its time.

3. **Inactive slots hold real temperatures, not zeros.** A real device's
   factory schedule stores in-range temperatures in its disabled weekend slots.
   An early validator here assumed inactive slots would be zeroed, and
   consequently rejected the device's own live schedule as invalid. The
   round-trip test in `tests/lib/test_protocol.py` pins a real device's actual
   112 bytes against this.

Ordering is validated on a **virtual timeline anchored at 02:00**, not on raw
clock time — a schedule may legitimately span midnight, which makes raw
"first ≥ 02:00 / last ≤ 01:50" checks mutually unsatisfiable.

### Temperature resolution

One `s8` unit is 5 decidegrees, i.e. **0.5 °C**. A value that is not a multiple
of 5 decidegrees would be silently truncated on encode, so `protocol.py`
rejects it instead — storing something other than what the caller asked for is
worse than an error.

Device range: **5.0 – 35.0 °C**.

---

## Writes worth knowing about

### `0x1082` is inert while the Home program is active

PID `0x1082` (Manual Control Temp) is the obvious-looking way to set a
temperature. While the Home program is the active program it **does nothing** —
no error, no change; the write is accepted and ignored.

The default write path is therefore PID `0x10D0` (Override Temperature), which
takes effect immediately regardless of the active program. That matches what a
user dragging a thermostat slider expects.

### Calibration is one triplet, not three values

PID `0x10B2` is written as a single `s16[3]` — room, floor, relay. The relay
axis is always forced to `0` by the device. Writing one axis means writing back
the other's current value alongside it, which is why the two calibration
entities read each other's state before writing.

---

## Error codes

`ErrorCode` in `const.py` mirrors Ebeco's §6 table. The one worth calling out is
`CRYPTO_ERROR` (10) — "decryption or key error". In practice that almost always
means the PSK is wrong for this device, not that the message was corrupted.
