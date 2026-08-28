#!/bin/sh
# Offline library suite — no thermostat, no radio, no homeassistant package.
set -eu
cd "$(dirname "$0")"
uv sync --quiet
uv run pytest "$@"
