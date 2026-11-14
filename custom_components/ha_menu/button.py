"""Bouton « Synchroniser » (une synchro de l'app couvre tous les enfants)."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import HaMenuApiError
from .const import DOMAIN, VERSION

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([SyncButton(entry.runtime_data)])


class SyncButton(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "synchroniser"
    _attr_icon = "mdi:sync"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_synchroniser"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "app")}, name="HA - Menu", manufacturer="HA - Menu",
            sw_version=VERSION,
        )

    async def async_press(self) -> None:
        try:
            if not await self.coordinator.api.sync():
                _LOGGER.info("Synchro déjà en cours")
        except HaMenuApiError as exc:
            _LOGGER.warning("Synchro impossible : %s", exc)
            return
        # Ne pas bloquer l'appel de service (automatisations) pendant la synchro.
        self.hass.async_create_background_task(self._refresh_when_done(), "ha_menu_sync")

    async def _refresh_when_done(self) -> None:
        api = self.coordinator.api
        try:
            for _ in range(60):  # 5 min max
                await asyncio.sleep(5)
                if not (await api.status()).get("syncing"):
                    break
        except HaMenuApiError as exc:
            _LOGGER.warning("Synchro impossible : %s", exc)
            return
        await self.coordinator.async_request_refresh()
