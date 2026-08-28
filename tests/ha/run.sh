#!/bin/sh
# Stage the component under an importable name, then run the HA-side tests.
#
# Staging is required because these tests must import the component as
# `eb300_ble`, not as `custom_components.eb300_ble` — that is how Home Assistant
# itself loads it.
set -eu
cd "$(dirname "$0")"
rm -rf .stage && mkdir -p .stage
cp -r ../../custom_components/eb300_ble .stage/
uv sync --quiet
PYTHONPATH="$PWD/.stage" uv run pytest "$@"
