"""Capteur « Menu affiché » : midi jusqu'à 21 h, puis lendemain."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import cantine as logic
from .entity import HaMenuChildEntity

_JOURS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
_MOIS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août",
         "sept.", "oct.", "nov.", "déc.")


async def async_setup_entry(
    hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(MenuSensor(coordinator, cid) for cid in coordinator.data)


class MenuSensor(HaMenuChildEntity, SensorEntity):
    _attr_icon = "mdi:silverware-fork-knife"

    def __init__(self, coordinator, child_id: int) -> None:
        super().__init__(coordinator, child_id, "menu_affiche")

    def _current(self):
        day, jour = logic.displayed_day(dt_util.now())
        lines, cantine = self.compute(day)
        return day, jour, lines, cantine

    @property
    def native_value(self) -> str:
        day = self._current()[0]
        return f"{_JOURS[day.weekday()]} {day.day} {_MOIS[day.month - 1]}"

    @property
    def extra_state_attributes(self) -> dict:
        day, jour, lines, cantine = self._current()
        return {
            "date": day.isoformat(),
            "jour": jour,
            "lignes": lines,
            "cantine": cantine,
            "inscription_verifiee": cantine is not None,
            "enfant": self.child_name,
        }
