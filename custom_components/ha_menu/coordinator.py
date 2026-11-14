"""Coordinateur : menus de tous les enfants, lus depuis l'app."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HaMenuApi, HaMenuApiError
from .const import DOMAIN, MAX_FAILURES, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class HaMenuCoordinator(DataUpdateCoordinator[dict[int, dict]]):
    """``data = {child_id: {"name": str, "days": {"YYYY-MM-DD": summary}}}``."""

    def __init__(self, hass: HomeAssistant, entry, api: HaMenuApi) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL, config_entry=entry,
        )
        self.api = api
        self._failures = 0

    async def _async_update_data(self) -> dict[int, dict]:
        try:
            data: dict[int, dict] = {}
            for child in await self.api.children():
                cid = int(child["child_id"])
                menus = await self.api.menus(cid)
                data[cid] = {
                    "name": child.get("child_name") or str(cid),
                    "days": {d["date"]: d.get("summary", "") for d in menus.get("days", [])},
                }
        except HaMenuApiError as exc:
            self._failures += 1
            # Une mise à jour de l'app coupe l'API quelques secondes : garder les données.
            if self.data is not None and self._failures < MAX_FAILURES:
                _LOGGER.debug("App injoignable (%s), données conservées", exc)
                return self.data
            raise UpdateFailed(str(exc)) from exc
        self._failures = 0
        return data
