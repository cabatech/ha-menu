"""Tests pour ha_menu.pipeline — découverte + run_extraction (sans réseau)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from ha_menu.config import AppConfig, SoHappyCredentials
from ha_menu.models import Child, MealCategory, MenuDay, MenuItem


def _cfg() -> AppConfig:
    return AppConfig(
        sohappy=SoHappyCredentials(email="a@b.c", password="p"),
        children=[
            Child(
                id=42, first_name="Paul", enabled=True,
                clients=[{"id": 1, "menus": [{"id": 7}]}],
            ),
        ],
    )


def test_run_extraction_requires_sohappy_credentials():
    from ha_menu.pipeline import run_extraction
    from ha_menu.sohappy import SoHappyError

    cfg = AppConfig()  # pas de creds
    with pytest.raises(SoHappyError):
        run_extraction(cfg, publish=False)


def test_run_extraction_login_failure(tmp_path: Path):
    from ha_menu.pipeline import run_extraction
    from ha_menu.sohappy import SoHappyError

    cfg = _cfg()
    with patch("ha_menu.pipeline.SoHappyClient") as cli:
        cli.return_value.__enter__.return_value.login.side_effect = SoHappyError("nope")
        with pytest.raises(SoHappyError):
            run_extraction(cfg, publish=False)


def test_run_extraction_success_no_publish():
    from ha_menu.pipeline import run_extraction

    cfg = _cfg()
    day = MenuDay(
        jour=date(2026, 9, 16),
        items=[MenuItem(label="Salade", category=MealCategory.ENTRY)],
    )
    with patch("ha_menu.pipeline.SoHappyClient") as cli:
        ctx = cli.return_value.__enter__.return_value
        ctx.login.return_value = None
        ctx.fetch_menus.return_value = [day]
        stats = run_extraction(cfg, publish=False)
    assert stats["ok"] is True
    assert stats["children_ok"] == 1
    assert stats["children"][0]["days_count"] == 1


def test_run_extraction_target_child_ids_filters():
    from ha_menu.pipeline import run_extraction

    cfg = _cfg()
    cfg.children.append(Child(id=99, first_name="Other", enabled=True))
    with patch("ha_menu.pipeline.SoHappyClient") as cli:
        ctx = cli.return_value.__enter__.return_value
        ctx.login.return_value = None
        ctx.fetch_menus.return_value = []
        stats = run_extraction(cfg, publish=False, target_child_ids=[42])
    assert len(stats["children"]) == 1
    assert stats["children"][0]["child_id"] == 42


def test_discover_children_merges_with_existing():
    from ha_menu.pipeline import discover_children

    cfg = AppConfig(sohappy=SoHappyCredentials(email="a@b.c", password="p"))
    cfg.children = [Child(id=42, first_name="Paul", ha_calendar="calendar.keep")]
    fresh = Child(
        id=42, first_name="Paul", school="École X",
        clients=[{"id": 1, "menus": [{"id": 2}]}],
    )
    with patch("ha_menu.pipeline.SoHappyClient") as cli:
        ctx = cli.return_value.__enter__.return_value
        ctx.login.return_value = None
        ctx.list_children.return_value = [fresh]
        out = discover_children(cfg)
    assert len(out) == 1
    # Agenda HA préservé
    assert out[0].ha_calendar == "calendar.keep"
    # Métadonnées fraîches écrasées
    assert out[0].school == "École X"


# ------------------------------------------------------------------------
# Cache menus (pipeline.save_child_menu_cache / load_child_menu_cache)
# ------------------------------------------------------------------------


def test_save_and_load_child_menu_cache(tmp_path: Path):
    from ha_menu.pipeline import (
        load_child_menu_cache,
        save_child_menu_cache,
    )

    child = Child(id=42, first_name="Paul", last_name="X")
    day = MenuDay(
        jour=date(2026, 9, 16),
        service="déjeuner",
        items=[
            MenuItem(label="Salade", category=MealCategory.ENTRY),
            MenuItem(label="Poulet", category=MealCategory.MAIN),
            MenuItem(label="Yaourt", category=MealCategory.DAIRY),
        ],
        label="Menu standard",
    )
    save_child_menu_cache(
        child, [day], tmp_path,
        source_url="https://www.so-happy.fr/x?child_id=42",
    )
    data = load_child_menu_cache(42, tmp_path)
    assert data is not None
    assert data["child_id"] == 42
    assert data["child_name"] == "Paul X"
    assert data["source_url"].endswith("child_id=42")
    assert len(data["days"]) == 1
    d = data["days"][0]
    assert d["date"] == "2026-09-16"
    assert d["service"] == "déjeuner"
    assert d["label"] == "Menu standard"
    assert "Entrée :" in d["summary"]
    assert "Plat :" in d["summary"]
    assert "Poulet" in d["summary"]


def test_load_child_menu_cache_missing(tmp_path: Path):
    from ha_menu.pipeline import load_child_menu_cache
    assert load_child_menu_cache(999, tmp_path) is None


def test_load_child_menu_cache_corrupted(tmp_path: Path):
    from ha_menu.pipeline import load_child_menu_cache
    (tmp_path / "1.json").write_text("{not json", encoding="utf-8")
    assert load_child_menu_cache(1, tmp_path) is None


def test_list_cached_children(tmp_path: Path):
    from ha_menu.pipeline import (
        list_cached_children,
        save_child_menu_cache,
    )

    save_child_menu_cache(Child(id=1, first_name="A"), [], tmp_path)
    save_child_menu_cache(Child(id=42, first_name="B"), [], tmp_path)
    (tmp_path / "garbage.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".tmp.json").write_text("{}", encoding="utf-8")
    assert list_cached_children(tmp_path) == [1, 42]


def test_run_extraction_writes_menu_cache(tmp_path: Path):
    """run_extraction doit écrire /data/menus/{id}.json (sans réseau)."""
    from ha_menu.pipeline import (
        load_child_menu_cache,
        run_extraction,
    )

    cfg = _cfg()
    cfg.config_path = tmp_path / "config.json"  # force menus_dir sous tmp_path
    day = MenuDay(
        jour=date(2026, 9, 16),
        items=[MenuItem(label="Salade", category=MealCategory.ENTRY)],
    )
    with patch("ha_menu.pipeline.SoHappyClient") as cli:
        ctx = cli.return_value.__enter__.return_value
        ctx.login.return_value = None
        ctx.fetch_menus.return_value = [day]
        run_extraction(cfg, publish=False)
    cache = load_child_menu_cache(42, tmp_path / "menus")
    assert cache is not None
    assert cache["child_id"] == 42
    assert len(cache["days"]) == 1
