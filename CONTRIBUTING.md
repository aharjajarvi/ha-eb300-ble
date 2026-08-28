# Contributing

Bug reports, protocol findings and pull requests are all welcome.

## Before opening a PR

```sh
./tests/lib/run.sh
./tests/ha/run.sh
uv run --project tests/lib ruff check .
uv run --project tests/lib mypy
```

All four must be clean. See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for what
each one covers and why there are two test suites.

## Things worth knowing before you change things

- **The library under `custom_components/eb300_ble/eb300_ble/` is the only copy.**
  Do not add a second one; the tests deliberately import the shipped code.
- **The library must not import `homeassistant`.** That boundary is what makes
  it testable without HA installed, and what would let it move to PyPI later.
- **The write path looks the way it does for reasons.**
  [docs/HARDWARE_NOTES.md](docs/HARDWARE_NOTES.md#write-path-design-rules) lists
  four rules that came out of five attended debugging cycles, three of which
  found a bug introduced by the previous cycle's fix. Read them before
  refactoring `climate.py`'s debounce or the coordinator's error handling.
- **Pin `tests/ha/pyproject.toml` forward** when you test against a newer Home
  Assistant.

## If you have different hardware

The most useful contribution is confirming — or contradicting — what
[docs/HARDWARE_NOTES.md](docs/HARDWARE_NOTES.md) says. Everything there was
found on firmware 1.2, batch 2603. If your device behaves differently, that is
worth an issue even if nothing is broken.

The same goes for anything the Open API document describes that this
integration does not implement. Note that Ebeco reserves some functions for
their own apps and backend, so not everything is reachable.

## Reporting a security issue

If you find something affecting the confidentiality of a device PSK, please
open a private security advisory on GitHub rather than a public issue.
