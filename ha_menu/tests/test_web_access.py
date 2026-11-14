"""Accès à l'API depuis le réseau interne des apps HA (intégration HA - Menu)."""

from __future__ import annotations

import inspect
from pathlib import Path

from starlette.testclient import TestClient

from ha_menu import web


def _client(tmp_path: Path, ip: str) -> TestClient:
    return TestClient(web.create_app(tmp_path / "config.json"), client=(ip, 40000))


def test_ha_core_on_internal_network_is_allowed(tmp_path: Path) -> None:
    assert _client(tmp_path, "172.30.32.1").get("/api/status").status_code == 200
    assert _client(tmp_path, "172.30.33.7").get("/api/status").status_code == 200


def test_other_private_network_still_requires_auth(tmp_path: Path) -> None:
    assert _client(tmp_path, "172.31.5.5").get("/api/status").status_code == 401


def test_supervisor_announce_does_not_block_event_loop() -> None:
    assert inspect.iscoroutinefunction(web.announce_to_supervisor)
