"""Config flow : découverte par le Supervisor ou URL saisie."""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import HaMenuApi, HaMenuApiError
from .const import (
    CONF_CANTINE_TODAY,
    CONF_CANTINE_TOMORROW,
    CONF_CHILD,
    CONF_CHILDREN,
    CONF_LOISIRS_TODAY,
    CONF_LOISIRS_TOMORROW,
    CONF_REPAS_LOISIRS,
    CONF_SOURCE,
    CONF_TIMETABLE_TODAY,
    CONF_TIMETABLE_TOMORROW,
    CONF_URL,
    DEFAULT_PORT,
    DEFAULT_URL,
    DOMAIN,
    SOURCE_GUICHET,
    SOURCE_NONE,
    SOURCES,
)


class HaMenuConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._url: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HaMenuOptionsFlow()

    async def _validate(self, url: str) -> bool:
        try:
            await HaMenuApi(async_get_clientsession(self.hass), url).status()
        except HaMenuApiError:
            return False
        return True

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors = {}
        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            if await self._validate(url):
                return self.async_create_entry(title="HA - Menu", data={CONF_URL: url})
            errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_URL, default=DEFAULT_URL): str}),
            errors=errors,
        )

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        cfg = discovery_info.config
        self._url = f"http://{cfg['host']}:{cfg.get('port', DEFAULT_PORT)}"
        self._abort_if_unique_id_configured(updates={CONF_URL: self._url})
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        if user_input is not None:
            if await self._validate(self._url):
                return self.async_create_entry(title="HA - Menu", data={CONF_URL: self._url})
            return self.async_abort(reason="cannot_connect")
        return self.async_show_form(step_id="hassio_confirm")


def options_children(entry) -> dict:
    """Enfants connus du coordinateur ; vide si l'entrée n'est pas chargée."""
    coordinator = getattr(entry, "runtime_data", None)
    return dict(getattr(coordinator, "data", None) or {})


class HaMenuOptionsFlow(OptionsFlow):
    """Par enfant : source d'inscription et entités à croiser."""

    def __init__(self) -> None:
        self._child: str | None = None
        self._source: str = SOURCE_NONE

    def _children_opts(self) -> dict:
        return dict(self.config_entry.options.get(CONF_CHILDREN, {}))

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        data = options_children(self.config_entry)
        if not data:
            return self.async_abort(reason="not_ready")
        if user_input is not None:
            self._child = user_input[CONF_CHILD]
            return await self.async_step_source()
        choices = [
            SelectOptionDict(value=str(cid), label=c.get("name", str(cid))) for cid, c in data.items()
        ]
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Required(CONF_CHILD): SelectSelector(SelectSelectorConfig(options=choices)),
        }))

    async def async_step_source(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = self._children_opts().get(self._child, {})
        if user_input is not None:
            self._source = user_input[CONF_SOURCE]
            if self._source == SOURCE_NONE:
                return self._save({CONF_SOURCE: SOURCE_NONE})
            return await self.async_step_entities()
        return self.async_show_form(step_id="source", data_schema=vol.Schema({
            vol.Required(CONF_SOURCE, default=current.get(CONF_SOURCE, SOURCE_NONE)): SelectSelector(
                SelectSelectorConfig(options=list(SOURCES), translation_key="source"),
            ),
        }))

    def _guess(self, *candidates: str) -> str | None:
        for pattern in candidates:
            for st in self.hass.states.async_all():
                if fnmatch(st.entity_id, pattern):
                    return st.entity_id
        return None

    async def async_step_entities(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = self._children_opts().get(self._child, {})
        if current.get(CONF_SOURCE) != self._source:
            current = {}
        if user_input is not None:
            return self._save({CONF_SOURCE: self._source, **user_input})

        def ent(key: str, domain: str, *guess: str, required: bool = True):
            default = current.get(key) or self._guess(*guess)
            marker = vol.Required if required else vol.Optional
            k = marker(key, description={"suggested_value": default}) if default else marker(key)
            return k, EntitySelector(EntitySelectorConfig(domain=domain))

        if self._source == SOURCE_GUICHET:
            fields = dict([
                ent(CONF_CANTINE_TODAY, "binary_sensor", "binary_sensor.guichet_cantine_aujourdhui"),
                ent(CONF_CANTINE_TOMORROW, "binary_sensor", "binary_sensor.guichet_cantine_demain"),
                ent(CONF_LOISIRS_TODAY, "binary_sensor",
                    "binary_sensor.guichet_accueil_loisirs_aujourdhui", required=False),
                ent(CONF_LOISIRS_TOMORROW, "binary_sensor",
                    "binary_sensor.guichet_accueil_loisirs_demain", required=False),
            ])
            fields[vol.Required(
                CONF_REPAS_LOISIRS, default=current.get(CONF_REPAS_LOISIRS, True),
            )] = BooleanSelector()
        else:
            fields = dict([
                ent(CONF_TIMETABLE_TODAY, "sensor", "sensor.pronote_*_today_s_timetable"),
                ent(CONF_TIMETABLE_TOMORROW, "sensor", "sensor.pronote_*_tomorrow_s_timetable"),
            ])
        return self.async_show_form(step_id="entities", data_schema=vol.Schema(fields))

    def _save(self, child_opts: dict) -> ConfigFlowResult:
        children = self._children_opts()
        children[self._child] = child_opts
        return self.async_create_entry(data={**self.config_entry.options, CONF_CHILDREN: children})
