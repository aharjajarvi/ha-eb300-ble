"""Every component module must import against the real HA version in use.

This is the check that was missing on 2026-08-27: `services.py` imported a
helper from a module it had moved out of, and HA refused to set the
integration up at all. `py_compile` passes on that; only a real import fails.
"""
import importlib
import pkgutil

import eb300_ble
import pytest

MODULES = sorted(m.name for m in pkgutil.walk_packages(eb300_ble.__path__, "eb300_ble."))


def test_module_list_is_not_empty():
    assert MODULES, "no submodules discovered - staging copy is probably missing"


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)
