"""Tests pour ha_menu.mqtt_publisher — discovery + state (mock paho)."""

from __future__ import annotations

import json
import sys
import types
from datetime import date
from typing import Any
from unittest.mock import patch

# Stub `paho.mqtt.client` AVANT tout import du module testé — permet de
# lancer pytest sans avoir installé paho-mqtt. On crée un VRAI ModuleType
# (pas un MagicMock) pour que ``unittest.mock.patch`` puisse le retrouver
# dans ``sys.modules`` et patcher ses attributs.
class _StubCallbackAPIVersion:
    VERSION2 = "v2"


_paho_pkg = types.ModuleType("paho")
_paho_mqtt_pkg = types.ModuleType("paho.mqtt")
_paho_client_mod = types.ModuleType("paho.mqtt.client")
_paho_client_mod.CallbackAPIVersion = _StubCallbackAPIVersion
# ``Client`` est volontairement absent : les tests qui en ont besoin
# l'injectent via ``patch("paho.mqtt.client.Client", ...)``. Les tests
# qui ne touchent pas au Publisher n'en ont pas besoin.
sys.modules.setdefault("paho", _paho_pkg)
sys.modules.setdefault("paho.mqtt", _paho_mqtt_pkg)
sys.modules.setdefault("paho.mqtt.client", _paho_client_mod)

from ha_menu.config import AppConfig
from ha_menu.models import Child, MealCategory, MenuDay, MenuItem
from ha_menu.mqtt_publisher import (
    HA_DEVICE_BASE,
    MQTTPublisher,
    _build_discovery_messages,
    _build_state_payloads,
    _slug,
    publish_all,
)


# ------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------


def _child() -> Child:
    return Child(
        id=1001,
        first_name="Paul",
        last_name="Dupont",
        enabled=True,
    )


def _day(date_str: str, items: list[MenuItem] | None = None) -> MenuDay:
    return MenuDay(
        jour=date.fromisoformat(date_str),
        items=items or [
            MenuItem(label="Salade", category=MealCategory.ENTRY),
            MenuItem(label="Poulet", category=MealCategory.MAIN),
        ],
        raw={"llm_summary": "Entrée : Salade\nPlat : Poulet\nGarnitures : \nLaitage : \nDessert : ",
             "llm_source": "llm"},
    )


def _publisher_cfg() -> AppConfig:
    return AppConfig(
        use_mqtt=True,
        mqtt_host="mqtt.example.lan",
        mqtt_port=1883,
        mqtt_username="cantine",
        mqtt_password="secret",
        mqtt_base_topic="ha_menu",
    )


# ------------------------------------------------------------------------
# Helpers discovery
# ------------------------------------------------------------------------


def test_slug_basic() -> None:
    assert _slug(_child()) == "paul_dupont"


def test_slug_falls_back_to_id() -> None:
    c = Child(id=42, first_name="", last_name="")
    assert _slug(c) == "42"


def test_discovery_emits_eleven_messages() -> None:
    """5 sensors today + 5 sensors tomorrow + 1 calendar = 11 messages."""
    msgs = _build_discovery_messages(_publisher_cfg(), _child())
    topics = [m[0] for m in msgs]
    sensors = [t for t in topics if "sensor" in t]
    calendars = [t for t in topics if "calendar" in t]
    assert len(sensors) == 10
    assert len(calendars) == 1
    # 5 catégories distinctes × 2 jours
    cat_keys = ("entree", "plat", "garnitures", "laitage", "dessert")
    for ck in cat_keys:
        assert any(f"today_{ck}" in t for t in sensors)
        assert any(f"tomorrow_{ck}" in t for t in sensors)


def test_discovery_payloads_have_device_and_unique_id() -> None:
    msgs = _build_discovery_messages(_publisher_cfg(), _child())
    for _topic, payload in msgs:
        assert payload["unique_id"].startswith(f"{HA_DEVICE_BASE}_paul_dupont_")
        assert payload["device"]["manufacturer"] == "HA - Menu"


def test_discovery_topic_format() -> None:
    msgs = _build_discovery_messages(_publisher_cfg(), _child())
    for topic, _ in msgs:
        assert topic.startswith("homeassistant/")
        assert topic.endswith("/config")


# ------------------------------------------------------------------------
# State payloads
# ------------------------------------------------------------------------


def test_state_today_payload_when_day_present() -> None:
    from datetime import datetime as _dt
    today = _dt.now().date()
    cfg = _publisher_cfg()
    days = [_day(today.isoformat())]
    msgs = _build_state_payloads(cfg, _child(), days)
    by_topic = {m[0]: m[1] for m in msgs}
    # 5 sensors today (state = string simple).
    today_entree = by_topic.get(f"{cfg.mqtt_base_topic}/paul_dupont/today/entree")
    today_plat = by_topic.get(f"{cfg.mqtt_base_topic}/paul_dupont/today/plat")
    assert today_entree == "Salade"
    assert today_plat == "Poulet"


def test_state_per_category_keys() -> None:
    """5 sensors today + 5 sensors tomorrow = 10 states + 1 calendar."""
    cfg = _publisher_cfg()
    msgs = _build_state_payloads(cfg, _child(), [])
    topics = [m[0] for m in msgs]
    cat_keys = ("entree", "plat", "garnitures", "laitage", "dessert")
    for ck in cat_keys:
        assert any(f"/today/{ck}" in t for t in topics)
        assert any(f"/tomorrow/{ck}" in t for t in topics)
    # + 1 calendar
    assert any(t.endswith("/calendar") for t in topics)


def test_state_per_category_empty_when_no_menu() -> None:
    """Aucun jour dispo → strings vides (state HA = unknown)."""
    cfg = _publisher_cfg()
    msgs = _build_state_payloads(cfg, _child(), [])
    for t, payload in msgs:
        if t.endswith("/calendar"):
            continue
        assert payload == "", f"{t} should be empty, got {payload!r}"


def test_state_calendar_only_future_days() -> None:
    """Le calendar n'inclut que J et au-delà (pas le passé)."""
    from datetime import datetime as _dt, timedelta as _td
    cfg = _publisher_cfg()
    today = _dt.now().date()
    days = [
        _day((today - _td(days=3)).isoformat()),  # passé : exclu
        _day(today.isoformat()),                    # aujourd'hui : inclus
        _day((today + _td(days=2)).isoformat()),    # futur : inclus
    ]
    msgs = _build_state_payloads(cfg, _child(), days)
    cal_payload = [p for t, p in msgs if t.endswith("/calendar")][0]
    cal = json.loads(cal_payload)
    assert len(cal) == 2
    assert all(e["all_day"] for e in cal)
    assert all(e["end"] > e["start"] for e in cal)


# ------------------------------------------------------------------------
# Publisher (mock paho)
# ------------------------------------------------------------------------


class _FakeMQTTInfo:
    def wait_for_publish(self, timeout: float = 10) -> None:
        return None


class _FakeMQTTClient:
    """Stub minimaliste de paho.mqtt.client.Client."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.connected = False
        self.published: list[tuple[str, str, int, bool]] = []
        self.username: str | None = None
        self.password: str | None = None

    def username_pw_set(self, user: str, pwd: str | None) -> None:
        self.username = user
        self.password = pwd

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        self.host = host
        self.port = port
        self.connected = True

    def loop_start(self) -> None:
        return None

    def loop_stop(self) -> None:
        return None

    def disconnect(self) -> None:
        self.connected = False

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False):
        self.published.append((topic, payload, qos, retain))
        return _FakeMQTTInfo()


def test_publisher_is_not_configured_when_disabled() -> None:
    cfg = AppConfig(use_mqtt=False, mqtt_host="x")
    assert MQTTPublisher(cfg).is_configured() is False


def test_publisher_is_configured_only_with_host_and_enabled() -> None:
    cfg = AppConfig(use_mqtt=True, mqtt_host="")
    assert MQTTPublisher(cfg).is_configured() is False


def test_publisher_emits_22_messages_per_child() -> None:
    """11 discovery + 11 state = 22 messages MQTT publiés par enfant."""
    cfg = _publisher_cfg()
    fake_holder: dict[str, Any] = {}

    def _factory(*args, **kwargs):
        client = _FakeMQTTClient(*args, **kwargs)
        fake_holder["client"] = client
        return client

    from ha_menu import mqtt_publisher
    with patch.object(mqtt_publisher, "mqtt") as fake_mqtt:
        fake_mqtt.CallbackAPIVersion = _StubCallbackAPIVersion
        fake_mqtt.Client.side_effect = _factory
        stats = MQTTPublisher(cfg).publish([_child()], {_child().id: []})

    client = fake_holder["client"]
    assert stats["published"] == 22  # 11 discovery + 11 state
    assert client.connected is False
    assert client.username == "cantine"
    assert client.password == "secret"
    topics = [t for (t, _, _, _) in client.published]
    assert sum(1 for t in topics if t.startswith("homeassistant/")) == 11
    assert sum(1 for t in topics if t.startswith("ha_menu/")) == 11
    for _t, _p, _q, retain in client.published:
        assert retain is True


def test_publisher_skips_when_not_configured() -> None:
    cfg = AppConfig(use_mqtt=True, mqtt_host="")
    stats = MQTTPublisher(cfg).publish([_child()], {})
    assert stats["skipped"] == "not_configured"


def test_publish_all_returns_summary() -> None:
    cfg = _publisher_cfg()
    from ha_menu import mqtt_publisher
    with patch.object(mqtt_publisher, "mqtt") as fake_mqtt:
        fake_mqtt.CallbackAPIVersion = _StubCallbackAPIVersion
        fake_mqtt.Client.side_effect = _FakeMQTTClient
        stats = publish_all(cfg, [_child()], {_child().id: []})
    assert "published" in stats
    assert stats["published"] == 22  # 11 discovery + 11 state


def test_payloads_are_valid_json_or_string() -> None:
    """22 messages : 11 discovery (dict JSON) + 10 sensors (str simple)
    + 1 calendar (JSON array string). Le test vérifie qu'aucun payload
    ne plante au décodage et que les discovery configs ont bien un champ
    'device'.
    """
    cfg = _publisher_cfg()
    raw_payloads: list[tuple[str, str]] = []

    def _factory(*args, **kwargs):
        client = _FakeMQTTClient(*args, **kwargs)

        def _capture(topic, payload, qos=0, retain=False):
            raw_payloads.append((topic, payload))
            return _FakeMQTTInfo()

        client.publish = _capture  # type: ignore[assignment]
        return client

    from ha_menu import mqtt_publisher
    with patch.object(mqtt_publisher, "mqtt") as fake_mqtt:
        fake_mqtt.CallbackAPIVersion = _StubCallbackAPIVersion
        fake_mqtt.Client.side_effect = _factory
        MQTTPublisher(cfg).publish([_child()], {_child().id: []})

    discovery_count = 0
    sensor_state_count = 0
    calendar_count = 0
    for topic, payload in raw_payloads:
        if topic.startswith("homeassistant/"):
            data = json.loads(payload)
            assert "device" in data
            discovery_count += 1
        elif topic.endswith("/calendar"):
            # JSON array (vide si aucun jour à venir). On vérifie juste
            # qu'il se décode sans erreur et que la longueur est >= 0.
            calendar_data = json.loads(payload)
            assert calendar_data == [] or len(calendar_data) >= 0
            calendar_count += 1
        else:
            assert isinstance(payload, str)
            sensor_state_count += 1
    assert discovery_count == 11
    assert sensor_state_count == 10
    assert calendar_count == 1
