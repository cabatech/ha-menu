"""Tests pour ha_menu.config — round-trip JSON + masquage des mots de passe."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ha_menu.config import AppConfig, SoHappyCredentials, load, save, to_dict
from ha_menu.models import Child


def _full_config() -> AppConfig:
    return AppConfig(
        sohappy=SoHappyCredentials(email="a@b.c", password="secret"),
        children=[
            Child(
                id=1,
                first_name="Paul",
                last_name="Dupont",
                school="École X",
                birth_date=date(2015, 1, 1),
                enabled=True,
                ha_calendar="calendar.cantine_paul",
            ),
        ],
    )


def test_roundtrip(tmp_path: Path) -> None:
    cfg = _full_config()
    save(cfg, tmp_path / "c.json")
    loaded = load(tmp_path / "c.json")
    assert loaded.sohappy.email == "a@b.c"
    assert loaded.sohappy.password == "secret"
    assert loaded.children[0].first_name == "Paul"
    assert loaded.children[0].ha_calendar == "calendar.cantine_paul"


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    cfg = load(tmp_path / "missing.json")
    assert cfg.sohappy.email == ""
    assert cfg.children == []


def test_save_permissions_600(tmp_path: Path) -> None:
    cfg = _full_config()
    p = tmp_path / "c.json"
    save(cfg, p)
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600, f"permissions attendues 600, got {oct(mode)}"


def test_to_dict_masks_passwords() -> None:
    cfg = _full_config()
    d = to_dict(cfg, include_passwords=False)
    assert d["sohappy"]["password_set"] is True
    assert "password" not in d["sohappy"]


def test_to_dict_includes_passwords_when_requested() -> None:
    cfg = _full_config()
    d = to_dict(cfg, include_passwords=True)
    assert d["sohappy_full"]["password"] == "secret"


def test_enabled_children_filters_disabled() -> None:
    cfg = _full_config()
    cfg.children[0].enabled = False
    assert cfg.enabled_children() == []


def test_find_child_by_id() -> None:
    cfg = _full_config()
    assert cfg.find_child(1).first_name == "Paul"
    assert cfg.find_child(99) is None


# ------------------------------------------------------------------------
# LLM (reformatage MiniMax)
# ------------------------------------------------------------------------


def test_llm_defaults_present() -> None:
    cfg = AppConfig()
    assert cfg.use_llm_for_menus is False
    assert cfg.minimax_api_key == ""
    assert cfg.minimax_base_url.startswith("https://")
    assert cfg.minimax_model  # non-vide
    assert cfg.minimax_timeout > 0


def test_llm_roundtrip(tmp_path: Path) -> None:
    cfg = AppConfig(
        use_llm_for_menus=True,
        minimax_api_key="sk-secret-key",
        minimax_base_url="https://api.example/v1",
        minimax_model="custom-model",
        minimax_timeout=42.0,
    )
    p = tmp_path / "c.json"
    save(cfg, p)
    loaded = load(p)
    assert loaded.use_llm_for_menus is True
    assert loaded.minimax_api_key == "sk-secret-key"
    assert loaded.minimax_base_url == "https://api.example/v1"
    assert loaded.minimax_model == "custom-model"
    assert loaded.minimax_timeout == 42.0


def test_to_dict_masks_minimax_api_key() -> None:
    cfg = AppConfig(minimax_api_key="sk-very-secret")
    d = to_dict(cfg, include_passwords=False)
    assert d["minimax_api_key_set"] is True
    assert "minimax_api_key" not in d
    assert "minimax_full" not in d or d["minimax_full"] is None


def test_to_dict_includes_minimax_full_when_requested() -> None:
    cfg = AppConfig(minimax_api_key="sk-very-secret", minimax_model="my-m")
    d = to_dict(cfg, include_passwords=True)
    assert d["minimax_full"]["api_key"] == "sk-very-secret"
    assert d["minimax_full"]["model"] == "my-m"


def test_load_missing_llm_fields_uses_defaults(tmp_path: Path) -> None:
    """Une config legacy (sans champs LLM) doit charger avec les défauts."""
    import json as _json

    legacy = {
        "sohappy": {"email": "a@b.c", "password": "p"},
        "children": [],
        "api_base": "https://x",
        "sync_cron": "0 2 * * *",
        "auto_sync": True,
    }
    p = tmp_path / "legacy.json"
    p.write_text(_json.dumps(legacy), encoding="utf-8")
    loaded = load(p)
    assert loaded.use_llm_for_menus is False
    assert loaded.minimax_api_key == ""
    assert loaded.minimax_model  # default non-vide


def test_old_mqtt_keys_ignored(tmp_path: Path) -> None:
    """Un config.json de la v2 (clés MQTT) se charge en v3, sans garder le MQTT."""
    import json

    p = tmp_path / "config.json"
    p.write_text(json.dumps({"use_mqtt": True, "mqtt_host": "broker", "mqtt_password": "x"}))
    cfg = load(p)
    assert not hasattr(cfg, "mqtt_host")
    save(cfg, p)
    assert "mqtt_password" not in json.loads(p.read_text())
