"""Tests de la logique pure « cantine » (sans Home Assistant)."""

import importlib.util
from datetime import date, datetime
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[2] / "custom_components" / "ha_menu" / "cantine.py"
_spec = importlib.util.spec_from_file_location("ha_menu_cantine", _PATH)
cantine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cantine)


def test_parse_summary():
    assert cantine.parse_summary("Entrée : Carottes\nPlat : Poulet\nbruit") == [
        {"label": "Entrée", "valeur": "Carottes"},
        {"label": "Plat", "valeur": "Poulet"},
    ]


def test_parse_summary_empty():
    assert cantine.parse_summary("") == []


def test_displayed_day_switches_at_21h():
    assert cantine.displayed_day(datetime(2026, 11, 16, 20, 59)) == (date(2026, 11, 16), "aujourd_hui")
    assert cantine.displayed_day(datetime(2026, 11, 16, 21, 0)) == (date(2026, 11, 17), "demain")


@pytest.mark.parametrize("c,l,repas,exp", [
    ("on", None, True, True), ("off", "on", True, True), ("off", "on", False, False),
    ("off", None, True, False), ("off", "off", True, False),
    ("unknown", None, True, None), ("unavailable", "off", True, None),
    ("off", "unknown", True, None), ("on", "unknown", True, True),
    (None, None, True, None),
])
def test_inscription_guichet(c, l, repas, exp):
    assert cantine.inscription_guichet(c, l, repas) is exp


def _lesson(morning, canceled=False):
    return {"is_morning": morning, "is_afternoon": not morning, "canceled": canceled}


def test_inscription_pronote():
    assert cantine.inscription_pronote("5", [_lesson(True), _lesson(False)]) is True
    assert cantine.inscription_pronote("3", [_lesson(True)]) is False
    assert cantine.inscription_pronote("2", [_lesson(True), _lesson(False, canceled=True)]) is False
    assert cantine.inscription_pronote("0", []) is False
    assert cantine.inscription_pronote("unavailable", None) is None
    assert cantine.inscription_pronote(None, None) is None


def test_cantine_rule():
    assert cantine.cantine(False, True) is False
    assert cantine.cantine(False, None) is False
    assert cantine.cantine(True, None) is None
    assert cantine.cantine(True, True) is True
    assert cantine.cantine(True, False) is False
