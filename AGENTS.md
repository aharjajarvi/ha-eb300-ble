# AGENTS.md

Context for AI-assisted development on this repo. Read this first; it is short
on purpose. The four documents under `docs/` hold the detail.

## What this is

A Home Assistant custom integration for the Ebeco EB-Therm 300 underfloor
heating thermostat, over Ebeco's local BLE Open API. Local polling, no cloud.
Feature-complete and hardware-verified: read-only sensors, full control
(setpoint, power, program, key lock, language, screensaver, calibration, clock
sync), and weekly schedule read/write via services.

## Repo map

| Path | What |
|---|---|
| `custom_components/eb300_ble/` | The integration. **The only thing HACS ships.** |
| `custom_components/eb300_ble/eb300_ble/` | The protocol library. **The only copy of it.** |
| `tests/lib/` | Library suite, 112 tests, no `homeassistant` dependency |
| `tests/ha/` | HA-glue suite, 327 tests, pinned `homeassistant` |
| `tools/` | Hardware bring-up CLIs — need a real device |
| `docs/` | ARCHITECTURE, PROTOCOL, HARDWARE_NOTES, DEVELOPMENT |

## Rules that are not negotiable

1. **One copy of the library.** It lives inside the component; `tests/lib`
   points `pythonpath` at it. Never create a `src/` tree or a second copy —
   the drift between them is a bug class this layout exists to eliminate.

2. **The library never imports `homeassistant`.** It uses relative imports
   internally and is tested with HA absent. That boundary is load-bearing.

3. **Pin `tests/ha/pyproject.toml` forward on every HA upgrade.** Import paths
   and selector semantics move between HA releases. That has broken this
   integration at load time three times. `py_compile` does not catch it; only
   importing every module against a real `homeassistant` package does.

   The pin tracks *the instance*, not the newest release — that is what makes a
   green run mean something about the deployment. The `ha-latest` job in
   `.github/workflows/tests.yml` covers the other half: it runs the same suite
   against whatever HA published most recently, nightly, and never blocks a PR.
   It tells you an upgrade is safe. It does not tell you the instance upgraded,
   and it is not a substitute for moving the pin when it does.

4. **Writes actuate real floor heating.** Read paths (`scan.py`, `read_all.py`,
   `poll.py`, the whole offline suite) can be exercised freely. Anything that
   writes to a device — `session.py --restore`, `w_tests.py`, any HA service
   call against a live thermostat — needs an explicit human go-ahead, every
   time. Snapshot with `tools/session.py --snapshot` before, restore after.

5. **Never commit a PSK.** `psk.txt`, `*.psk` and `secrets*.txt` are
   gitignored. A PSK is a device credential; `diagnostics.py` redacts it and
   must keep doing so.

## Before claiming anything works

```sh
./tests/lib/run.sh
./tests/ha/run.sh
uv run --project tests/lib ruff check .
uv run --project tests/lib mypy
```

439 tests, all four clean. None of them need hardware.

Coverage is worth checking before believing a path is tested — `./tests/ha/run.sh
--cov=eb300_ble --cov-report=term-missing`. Everything under
`custom_components/eb300_ble/*.py` is at 100%. (Until 2026-09-26 this said
so while the energy sensor's value was never computed by any test; it is now.)

## Code you should not casually refactor

`climate.py`'s debounce/cancel logic and the coordinator's write error handling
encode four rules that came out of five attended debugging cycles against real
hardware — three of those cycles found a bug introduced by the previous one's
fix. The rules and their failure modes are in
[docs/HARDWARE_NOTES.md](docs/HARDWARE_NOTES.md#write-path-design-rules). Read
them before touching either file; the code looks over-careful because it is
exactly as careful as it needs to be.

Rules 1, 3 and 4 now have regression tests — `tests/ha/test_climate_write_path.py`
and `tests/ha/test_coordinator.py`, both mutation-checked. Rule 2 does not, and
cannot: it describes a hazard the current single-task design removed. See the
correction under rule 2 in HARDWARE_NOTES before reinstating it as a
constraint.

Similarly, `protocol.py`'s home-program validation looks fussy because the
device enforces chronological ordering across *inactive* slots, and stores real
temperatures in them. An earlier "obvious" simplification rejected the device's
own factory schedule.

## Release checklist

1. Both suites, ruff, mypy — clean.
2. Bump `version` in `custom_components/eb300_ble/manifest.json`.
3. Tag `v<same version>`; HACS matches the tag against the manifest.
4. Publish the GitHub release.

## Working style that suited this project

The private development history behind this repo was a long, honest engineering
log: every hardware finding written down, including the ones that contradicted
an earlier assumption, and explicit "this is not verified" notes where evidence
was weaker than it looked. That is why `docs/HARDWARE_NOTES.md` can state
firmware behaviour with confidence and can also say plainly which gaps
remain untested.

Worth continuing. When a test passes for a reason you did not predict, say so
rather than smoothing it over.
