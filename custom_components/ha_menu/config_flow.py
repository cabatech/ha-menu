"""Config flow : découverte par le Supervisor ou URL saisie."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import HaMenuApi, HaMenuApiError
from .const import CONF_URL, DEFAULT_PORT, DEFAULT_URL, DOMAIN


class HaMenuConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._url: str | None = None

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
