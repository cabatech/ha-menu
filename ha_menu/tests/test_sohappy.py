"""Tests pour ha_menu.sohappy — parsing du payload menus + erreurs."""

from __future__ import annotations

from datetime import date

import pytest

from ha_menu.sohappy import _extract_date, _parse_menus_payload


def test_parse_payload_list_of_items():
    payload = [
        {
            "date": "2026-09-16",
            "label": "Salade verte",
            "category_code": "entree",
            "is_organic": True,
        },
        {
            "date": "2026-09-16",
            "label": "Pâtes bolo",
            "category_code": "plat",
        },
        {
            "date": "2026-09-17",
            "label": "Compote",
            "category_code": "dessert",
            "allergens": ["gluten", "lait"],
        },
    ]
    days = _parse_menus_payload(payload, menu_label="Cantine")
    assert len(days) == 2
    assert days[0].jour == date(2026, 9, 16)
    assert len(days[0].items) == 2
    assert days[1].items[0].allergens == ["gluten", "lait"]


def test_parse_payload_dict_with_items():
    payload = {
        "items": [{
            "date": "2026-09-16",
            "menus": [{
                "categories": [{"code": "PLAT", "products": [{"commercial_label": "Repas"}]}],
                "name": "X",
            }],
        }],
    }
    days = _parse_menus_payload(payload, menu_label="X")
    assert len(days) == 1


def test_parse_payload_skips_items_without_date():
    payload = [{"label": "Orphelin", "category_code": "plat"}]
    assert _parse_menus_payload(payload, menu_label="X") == []


def test_parse_payload_skips_items_without_label():
    payload = [{"date": "2026-09-16", "category_code": "plat"}]
    assert _parse_menus_payload(payload, menu_label="X") == []


def test_extract_date_various_keys():
    assert _extract_date({"date": "2026-09-16"}) == date(2026, 9, 16)
    assert _extract_date({"service_date": "2026-09-16"}) == date(2026, 9, 16)
    assert _extract_date({}) is None
    assert _extract_date({"date": "not-a-date"}) is None


def test_parse_payload_allergen_string_split():
    payload = [
        {
            "date": "2026-09-16",
            "label": "Gâteau",
            "category_code": "dessert",
            "allergens": "gluten, lait, oeuf",
        },
    ]
    days = _parse_menus_payload(payload, menu_label="X")
    assert days[0].items[0].allergens == ["gluten", "lait", "oeuf"]


@pytest.mark.parametrize(
    "raw_label,expected",
    [
        ("entree", "Entrée"),
        ("plat", "Plat"),
        ("dessert", "Dessert"),
        ("accompagnement", "Accompagnement"),
        ("produit_laitier", "Produit laitier"),
        ("unknown", "Autre"),
    ],
)
def test_meal_category_labels(raw_label, expected):
    from ha_menu.models import classify_category
    cat = classify_category(raw_label)
    assert cat.label == expected
