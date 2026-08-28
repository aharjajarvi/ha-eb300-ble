import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: F401


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
