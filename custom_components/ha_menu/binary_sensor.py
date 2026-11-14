"""Capteurs « Cantine aujourd'hui / demain »."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import HaMenuChildEntity


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        CantineSensor(coordinator, cid, tomorrow)
        for cid in coordinator.data for tomorrow in (False, True)
    )


class CantineSensor(HaMenuChildEntity, BinarySensorEntity):
    _attr_icon = "mdi:food-fork-drink"

    def __init__(self, coordinator, child_id: int, tomorrow: bool) -> None:
        super().__init__(coordinator, child_id, "cantine_demain" if tomorrow else "cantine_aujourdhui")
        self._tomorrow = tomorrow

    @property
    def is_on(self) -> bool | None:
        return self.compute(self.tomorrow() if self._tomorrow else self.today())[1]
