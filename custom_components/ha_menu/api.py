"""Client de l'API REST de l'app HA - Menu (réseau interne, sans jeton)."""

from __future__ import annotations

import asyncio

import aiohttp


class HaMenuApiError(Exception):
    """App injoignable ou réponse invalide."""


class HaMenuApi:
    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        self._session = session
        self.base_url = base_url.rstrip("/")

    async def _request(self, method: str, path: str, **kwargs) -> tuple[int, dict]:
        try:
            async with self._session.request(
                method, f"{self.base_url}{path}", timeout=aiohttp.ClientTimeout(total=15), **kwargs,
            ) as resp:
                if resp.status >= 500:
                    raise HaMenuApiError(f"{path} : HTTP {resp.status}")
                return resp.status, await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise HaMenuApiError(f"{path} : {exc}") from exc

    async def status(self) -> dict:
        code, data = await self._request("GET", "/api/status")
        if code != 200:
            raise HaMenuApiError(f"/api/status : HTTP {code}")
        return data

    async def children(self) -> list[dict]:
        code, data = await self._request("GET", "/api/menus")
        if code != 200:
            raise HaMenuApiError(f"/api/menus : HTTP {code}")
        return data.get("children", [])

    async def menus(self, child_id: int) -> dict:
        code, data = await self._request("GET", f"/api/menus/{child_id}")
        if code == 404:
            return {"days": []}
        if code != 200:
            raise HaMenuApiError(f"/api/menus/{child_id} : HTTP {code}")
        return data

    async def sync(self) -> bool:
        """Lance une synchro ; ``False`` si une synchro tourne déjà."""
        code, _ = await self._request("POST", "/api/sync", json={})
        return code == 200
