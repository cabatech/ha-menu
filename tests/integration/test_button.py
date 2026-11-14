"""Le bouton rend la main tout de suite : l'attente de fin de synchro tourne en tâche de fond."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant")
from custom_components.ha_menu.button import SyncButton


def test_press_returns_without_waiting_for_sync():
    api = SimpleNamespace(sync=AsyncMock(return_value=True), status=AsyncMock(return_value={"syncing": True}))
    button = SyncButton(MagicMock(api=api))
    tasks = []
    button.hass = SimpleNamespace(async_create_background_task=lambda coro, name: tasks.append(coro))

    asyncio.run(asyncio.wait_for(button.async_press(), timeout=1))

    api.sync.assert_awaited_once()
    assert len(tasks) == 1
    tasks[0].close()
