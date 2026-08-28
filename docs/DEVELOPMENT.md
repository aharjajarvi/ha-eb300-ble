# Development

No thermostat is required to develop or test this. The device side is
implemented in Python (`tests/lib/fakes.py::FakeEB300`), and 232 of the tests
run with no radio and no hardware.

## Layout

```
custom_components/eb300_ble/     the integration — the only thing HACS ships
  brand/                         icon.png / icon@2x.png (HA 2026.3+ inline brands)
  eb300_ble/                     the protocol library — the ONLY copy of it
tests/lib/                       library suite, no `homeassistant` dependency
tests/ha/                        HA-glue suite, pinned `homeassistant`
tools/                           hardware bring-up CLIs (need a real device)
docs/
```

There is no `src/` tree. The library lives inside the component and the tests
point at it, so a green run says something about the artifact users install.
See [ARCHITECTURE.md](ARCHITECTURE.md#why-the-library-is-vendored).

## Running the tests

```sh
./tests/lib/run.sh          # 103 tests, ~0.2s, no HA, no radio
./tests/ha/run.sh           # 129 tests, needs the pinned homeassistant package
```

Both accept pytest arguments: `./tests/lib/run.sh -k crypto -v`.

`tests/ha/run.sh` stages the component into `tests/ha/.stage/eb300_ble` first,
because these tests must import it as `eb300_ble` — the name Home Assistant
itself loads it under, not `custom_components.eb300_ble`.

## Lint and types

```sh
uv run --project tests/lib ruff check .        # whole repo, config in ruff.toml
uv run --project tests/lib mypy                # strict, library only, config in mypy.ini
```

`mypy` covers `custom_components/eb300_ble/eb300_ble` only. The HA glue is
verified instead by `tests/ha` importing it against a real `homeassistant`
package — HA's type surface is not available in the library environment.

## Pin the HA version forward on every upgrade

**This is the one maintenance rule that is not optional.**

`tests/ha/pyproject.toml` pins `homeassistant==` to the version the target
instance actually runs (Settings → About). Import paths and selector semantics
move between HA releases, and that is what has broken this integration at load
time — three failed deploys from one helper relocating between modules.

After every HA upgrade: bump the pin, re-run `./tests/ha/run.sh`, fix what
breaks. The nightly hassfest job in CI is the backstop, not the primary check.

## Working against real hardware

`tools/` holds the bring-up CLIs. They need the library importable, which they
arrange themselves via `sys.path`, and a PSK file:

```sh
uv run --project tests/lib python tools/scan.py --timeout 60
uv run --project tests/lib python tools/gatt_dump.py --mac AA:BB:CC:DD:EE:FF
uv run --project tests/lib python tools/handshake.py --mac AA:BB:CC:DD:EE:FF --psk-file psk.txt
uv run --project tests/lib python tools/read_all.py --mac AA:BB:CC:DD:EE:FF --psk-file psk.txt --status --verbose
uv run --project tests/lib python tools/poll.py --mac AA:BB:CC:DD:EE:FF --psk-file psk.txt
```

`psk.txt` is gitignored. Keep it that way — it is a device credential.

| Tool | What it does |
|---|---|
| `scan.py` | Decode advertisements live. Confirms the device is visible and whether bit 5 (Open API PSK provisioned) is set. Needs no PSK. |
| `gatt_dump.py` | GATT layout and negotiated MTU. |
| `handshake.py` | Handshake only. `--wrong-psk` exercises the rejection path. |
| `read_all.py` | Read every readable PID, human-readable. The read-only workhorse. |
| `poll.py` | Repeated polling, the way the integration does it. |
| `session.py` | **Snapshot / restore every writable value.** |
| `w_tests.py` | The write-test sequence. |

### Writes actuate real floor heating

`session.py` and `w_tests.py` change the state of a physical heating system.

The working rule that kept this project safe: **read paths can be exercised
freely; writes need a deliberate, attended decision.** Before any write test:

```sh
uv run --project tests/lib python tools/session.py --mac ... --psk-file psk.txt --snapshot
# ... run write tests ...
uv run --project tests/lib python tools/session.py --mac ... --psk-file psk.txt --restore --file snapshot-<timestamp>.json
```

`w_tests.py` keeps writes within ±1.0 °C of the setpoint measured when it
starts, and does not restore anything itself. Snapshot first, always.

## Releasing

1. Make the change; run both suites, `ruff` and `mypy`.
2. Bump `version` in `custom_components/eb300_ble/manifest.json`.
3. Tag `v<same version>` and publish a GitHub release. **HACS matches the tag
   against the manifest version** — a mismatch shows users the wrong version
   after install.
4. HACS users get the release; anyone tracking the default branch gets it on
   the next download.

## Adding a translation

`custom_components/eb300_ble/translations/<lang>.json`, mirroring `en.json`.
`strings.json` is the source; `en.json` must stay in sync with it. Remember the
brace rule in [HARDWARE_NOTES.md](HARDWARE_NOTES.md#braces-in-stringsjson-break-the-frontend).
