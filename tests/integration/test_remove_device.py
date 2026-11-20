"""Un appareil d'enfant que l'app ne connaît plus peut être supprimé depuis HA."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")
from custom_components.ha_menu import async_remove_config_entry_device


def _remove(identifier: str, known=(661553,)) -> bool:
    entry = SimpleNamespace(runtime_data=SimpleNamespace(data={cid: {} for cid in known}))
    device = SimpleNamespace(identifiers={("ha_menu", identifier)})
    return asyncio.run(async_remove_config_entry_device(None, entry, device))


def test_stale_child_device_can_be_removed():
    assert _remove("child_655456") is True


def test_known_child_and_app_devices_are_kept():
    assert _remove("child_661553") is False
    assert _remove("app") is False
