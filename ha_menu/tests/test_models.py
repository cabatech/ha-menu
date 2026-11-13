"""Tests pour ha_menu.models — parsing des catégories, flags, MenuDay.summary."""

from __future__ import annotations

from datetime import date

from ha_menu.models import (
    Child,
    MealCategory,
    MenuDay,
    MenuItem,
    classify_category,
    detect_label_flags,
)


def test_classify_category_known():
    assert classify_category("entree") == MealCategory.ENTRY
    assert classify_category("plat") == MealCategory.MAIN
    assert classify_category("dessert") == MealCategory.DESSERT
    assert classify_category("accompagnement") == MealCategory.SIDE


def test_classify_category_unknown_falls_back_to_other():
    assert classify_category("unknown_thing") == MealCategory.OTHER


def test_detect_label_flags_all_true():
    raw = {
        "is_vegetarian": True,
        "organic": True,
        "is_local": True,
        "homemade": True,
    }
    flags = detect_label_flags(raw)
    assert all(flags.values())


def test_detect_label_flags_missing():
    flags = detect_label_flags({})
    assert not any(flags.values())


def test_menu_day_summary_with_items():
    """summary() expose les plats groupés par catégorie, sans tags
    végé/bio (format court CalDAV, v0.6.0+)."""
    day = MenuDay(
        jour=date(2026, 9, 16),
        items=[
            MenuItem(label="Salade verte", category=MealCategory.ENTRY, is_organic=True),
            MenuItem(label="Pâtes bolo", category=MealCategory.MAIN),
            MenuItem(label="Yaourt", category=MealCategory.DAIRY),
        ],
    )
    s = day.summary
    assert "Salade verte" in s
    assert "Pâtes bolo" in s
    assert "Yaourt" in s
    # Aucun tag dans le format court.
    assert "🌿" not in s
    assert "🌱" not in s


def test_menu_day_summary_empty():
    day = MenuDay(jour=date(2026, 9, 16))
    assert day.summary == "Menu indisponible"


def test_child_full_name():
    assert Child(id=1, first_name="Paul", last_name="Dupont").full_name == "Paul Dupont"
    assert Child(id=1, first_name="Paul").full_name == "Paul"
    assert Child(id=1).full_name == ""
