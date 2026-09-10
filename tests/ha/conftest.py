import json
import pathlib

import pytest
from homeassistant import loader
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: F401

COMPONENT_DIR = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "eb300_ble"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def registered_integration(hass):
    """Make HA able to load this component by domain (config flows, entry setup).

    HA discovers custom integrations by scanning `<config>/custom_components`,
    which in this harness is a directory inside pytest-homeassistant-custom-
    component's own package -- our component is not there, it is staged onto
    `PYTHONPATH` as a top-level `eb300_ble` package (see run.sh). Registering it
    in the loader cache directly is what keeps that staging honest: `pkg_path`
    is the same `eb300_ble` the tests import and patch, so there is exactly one
    copy of the module in play. Symlinking it under a `custom_components/`
    config dir instead would import it a second time under a second name, and
    patches applied to one would not be seen by the other.
    """
    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text())
    integration = loader.Integration(
        hass,
        "eb300_ble",
        COMPONENT_DIR,
        manifest,
        {path.name for path in COMPONENT_DIR.iterdir()},
    )
    hass.data[loader.DATA_CUSTOM_COMPONENTS] = {integration.domain: integration}
    return integration
