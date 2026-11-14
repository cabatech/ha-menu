"""Entité de base par enfant : menu et règle cantine, recalculés localement."""

from __future__ import annotations

from datetime import date, timedelta

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import cantine as logic
from .const import (
    CONF_CANTINE_TODAY,
    CONF_CANTINE_TOMORROW,
    CONF_CHILDREN,
    CONF_LOISIRS_TODAY,
    CONF_LOISIRS_TOMORROW,
    CONF_REPAS_LOISIRS,
    CONF_SOURCE,
    CONF_TIMETABLE_TODAY,
    CONF_TIMETABLE_TOMORROW,
    DOMAIN,
    SOURCE_GUICHET,
    SOURCE_NONE,
    SOURCE_PRONOTE,
)
from .coordinator import HaMenuCoordinator


def child_options(entry, child_id: int) -> dict:
    return entry.options.get(CONF_CHILDREN, {}).get(str(child_id), {})


def _state(hass: HomeAssistant, entity_id: str | None) -> tuple[str | None, dict]:
    st = hass.states.get(entity_id) if entity_id else None
    return (st.state, dict(st.attributes)) if st else (None, {})


class HaMenuChildEntity(CoordinatorEntity[HaMenuCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HaMenuCoordinator, child_id: int, key: str) -> None:
        super().__init__(coordinator)
        self.child_id = child_id
        self._attr_translation_key = key
        self._attr_unique_id = f"{DOMAIN}_{child_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"child_{child_id}")},
            name=f"Cantine {self.child_name}",
            manufacturer="HA - Menu",
        )

    @property
    def child_name(self) -> str:
        child = (self.coordinator.data or {}).get(self.child_id) or {}
        return (child.get("name") or str(self.child_id)).split()[0]

    @property
    def available(self) -> bool:
        return super().available and self.child_id in (self.coordinator.data or {})

    def _opts(self) -> dict:
        return child_options(self.coordinator.config_entry, self.child_id)

    def _linked_entities(self) -> list[str]:
        o = self._opts()
        keys = {
            SOURCE_GUICHET: (CONF_CANTINE_TODAY, CONF_CANTINE_TOMORROW,
                             CONF_LOISIRS_TODAY, CONF_LOISIRS_TOMORROW),
            SOURCE_PRONOTE: (CONF_TIMETABLE_TODAY, CONF_TIMETABLE_TOMORROW),
        }.get(o.get(CONF_SOURCE, SOURCE_NONE), ())
        return [o[k] for k in keys if o.get(k)]

    def compute(self, day: date) -> tuple[list[dict], bool | None]:
        """(lignes du menu, cantine) pour ``day`` (aujourd'hui ou demain)."""
        days = ((self.coordinator.data or {}).get(self.child_id) or {}).get("days", {})
        summary = days.get(day.isoformat())
        lines = logic.parse_summary(summary or "")
        o = self._opts()
        source = o.get(CONF_SOURCE, SOURCE_NONE)
        tomorrow = day != dt_util.now().date()
        if source == SOURCE_GUICHET:
            c, _ = _state(self.hass, o.get(CONF_CANTINE_TOMORROW if tomorrow else CONF_CANTINE_TODAY))
            lo, _ = _state(self.hass, o.get(CONF_LOISIRS_TOMORROW if tomorrow else CONF_LOISIRS_TODAY))
            inscription = logic.inscription_guichet(c, lo, o.get(CONF_REPAS_LOISIRS, True))
        elif source == SOURCE_PRONOTE:
            st, attrs = _state(
                self.hass, o.get(CONF_TIMETABLE_TOMORROW if tomorrow else CONF_TIMETABLE_TODAY),
            )
            inscription = logic.inscription_pronote(st, attrs.get("lessons"))
        else:
            inscription = True
        return lines, logic.cantine(summary is not None, inscription)

    @staticmethod
    def today() -> date:
        return dt_util.now().date()

    @staticmethod
    def tomorrow() -> date:
        return dt_util.now().date() + timedelta(days=1)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _refresh(_arg: Event | object = None) -> None:
            self.async_write_ha_state()

        for hour in (0, logic.SWITCH_HOUR):
            self.async_on_remove(
                async_track_time_change(self.hass, _refresh, hour=hour, minute=0, second=0),
            )
        if linked := self._linked_entities():
            self.async_on_remove(async_track_state_change_event(self.hass, linked, _refresh))
