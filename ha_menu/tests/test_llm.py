"""Tests pour ha_menu.llm — format_menu_short + enhance_days (mock httpx)."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from ha_menu.config import (
    DEFAULT_MINIMAX_BASE_URL,
    DEFAULT_MINIMAX_MODEL,
    AppConfig,
)
from ha_menu.llm import (
    LLMClient,
    LLMError,
    SYSTEM_PROMPT,
    _looks_like_short_format,
    _strip_think_tags,
    enhance_days,
)
from ha_menu.models import (
    MealCategory,
    MenuDay,
    MenuItem,
    format_menu_short,
)


# ------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------


def _day(items: list[MenuItem] | None = None) -> MenuDay:
    return MenuDay(
        jour=date(2026, 9, 16),
        items=items or [],
        label="Cantine",
    )


def _items() -> list[MenuItem]:
    return [
        MenuItem(label="Salade verte", category=MealCategory.ENTRY, allergens=["sulfites"]),
        MenuItem(label="Poulet rôti", category=MealCategory.MAIN, is_organic=True),
        MenuItem(label="Frites", category=MealCategory.SIDE),
        MenuItem(label="Haricots verts", category=MealCategory.SIDE),
        MenuItem(label="Yaourt", category=MealCategory.DAIRY),
        MenuItem(label="Compote", category=MealCategory.DESSERT),
    ]


class _MockTransport(httpx.BaseTransport):
    """Transport httpx minimaliste : renvoie une réponse pré-enregistrée."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self.response


def _make_llm_response(content: str, status: int = 200) -> httpx.Response:
    import json

    payload = {
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    return httpx.Response(
        status_code=status,
        content=json.dumps(payload).encode("utf-8"),
        request=httpx.Request("POST", "http://test.local/chat/completions"),
    )


# ------------------------------------------------------------------------
# format_menu_short
# ------------------------------------------------------------------------


def test_format_menu_short_5_lines_exact_labels() -> None:
    day = _day(_items())
    out = format_menu_short(day)
    expected = (
        "Entrée : Salade verte\n"
        "Plat : Poulet rôti\n"
        "Garnitures : Frites / Haricots verts\n"
        "Laitage : Yaourt\n"
        "Dessert : Compote"
    )
    assert out == expected


def test_format_menu_short_empty_categories_kept() -> None:
    day = _day([MenuItem(label="Salade", category=MealCategory.ENTRY)])
    out = format_menu_short(day)
    lines = out.split("\n")
    assert lines == [
        "Entrée : Salade",
        "Plat : ",
        "Garnitures : ",
        "Laitage : ",
        "Dessert : ",
    ]


def test_format_menu_short_no_items_returns_unavailable() -> None:
    assert format_menu_short(_day([])) == "Menu indisponible"


def test_format_menu_short_dedupes_within_category() -> None:
    day = _day([
        MenuItem(label="Salade", category=MealCategory.ENTRY),
        MenuItem(label="Salade", category=MealCategory.ENTRY),
        MenuItem(label=" Crudités", category=MealCategory.ENTRY),
    ])
    out = format_menu_short(day)
    assert out.split("\n")[0] == "Entrée : Salade / Crudités"


def test_format_menu_short_ignores_other_category() -> None:
    day = _day([
        MenuItem(label="Barquette", category=MealCategory.OTHER),
        MenuItem(label="Salade", category=MealCategory.ENTRY),
    ])
    out = format_menu_short(day)
    lines = out.split("\n")
    assert "Barquette" not in out
    assert lines[0] == "Entrée : Salade"


def test_format_menu_short_no_allergens_no_flags() -> None:
    day = _day([
        MenuItem(label="X", category=MealCategory.MAIN, is_vegetarian=True, is_organic=True),
    ])
    out = format_menu_short(day)
    assert "🌱" not in out
    assert "🌿" not in out
    assert "allergène" not in out.lower()
    assert "végé" not in out.lower()
    assert "bio" not in out.lower()


def test_format_menu_short_strips_noise_parentheses() -> None:
    """Les mentions parasites (Elémentaire/Maternelle/VF/PMD/LR) sont retirées."""
    day = _day([
        MenuItem(label="Salade de tomates (Elémentaire)", category=MealCategory.ENTRY),
        MenuItem(label="Aiguillette de poulet VF sauce moutarde (Elémentaire)", category=MealCategory.MAIN),
        MenuItem(label="Pont l'Evêque AOP (Elémentaire)", category=MealCategory.DAIRY),
    ])
    out = format_menu_short(day)
    assert "(Elémentaire)" not in out
    assert "VF" not in out
    assert "AOP" not in out
    # Mais le reste du libellé est conservé.
    assert "Salade de tomates" in out
    assert "Aiguillette de poulet sauce moutarde" in out
    assert "Pont l'Evêque" in out


# ------------------------------------------------------------------------
# MenuDay.summary
# ------------------------------------------------------------------------


def test_menu_day_summary_uses_llm_when_present() -> None:
    """Si raw['llm_summary'] est posé, day.summary le retourne tel quel
    (sans retraitement), permettant au LLM de piloter le rendu."""
    day = _day(_items())
    custom = (
        "Entrée : Salade\n"
        "Plat : Poulet\n"
        "Garnitures : Riz\n"
        "Laitage : Yaourt\n"
        "Dessert : Pomme"
    )
    day.raw["llm_summary"] = custom
    assert day.summary == custom


def test_menu_day_summary_falls_back_to_local_when_no_llm() -> None:
    day = _day(_items())
    assert day.summary == format_menu_short(day)


def test_menu_day_detailed_description_keeps_allergens_and_flags() -> None:
    day = _day(_items())
    desc = day.detailed_description
    assert "Salade verte" in desc
    assert "sulfites" in desc
    assert "🌿" in desc  # is_organic


# ------------------------------------------------------------------------
# enhance_days — fallback local
# ------------------------------------------------------------------------



# ------------------------------------------------------------------------
# _looks_like_short_format
# ------------------------------------------------------------------------


def test_looks_like_short_format_accepts_canonical() -> None:
    text = (
        "Entrée : Salade\n"
        "Plat : Poulet\n"
        "Garnitures : Riz\n"
        "Laitage : Yaourt\n"
        "Dessert : "
    )
    assert _looks_like_short_format(text) is True


def test_looks_like_short_format_rejects_partial() -> None:
    assert _looks_like_short_format("Entrée : X\nPlat : Y") is False
    assert _looks_like_short_format("") is False



def test_strip_think_tags_removes_think_block() -> None:
    raw = (
        "<think>\nAnalyse interne du modèle sur plusieurs lignes.\n"
        "Multi-ligne OK.\n</think>\n\n"
        "Entrée : Salade\nPlat : Poulet\nGarnitures : Riz\n"
        "Laitage : Yaourt\nDessert : Pomme\n"
    )
    stripped = _strip_think_tags(raw)
    assert "<think>" not in stripped
    assert "Analyse interne" not in stripped
    assert "Multi-ligne OK" not in stripped
    assert stripped.startswith("Entrée : Salade")
    assert _looks_like_short_format(stripped) is True


def test_strip_think_tags_handles_no_think_block() -> None:
    raw = "Entrée : X\nPlat : Y\nGarnitures : Z\nLaitage : W\nDessert : V"
    assert _strip_think_tags(raw) == raw


def test_strip_think_tags_handles_multiline() -> None:
    raw = (
        "<think>\nligne 1\nligne 2\nligne 3\n</think>\n\nEntrée : X\n"
        "Plat : Y\nGarnitures : Z\nLaitage : W\nDessert : V"
    )
    out = _strip_think_tags(raw)
    assert "<think>" not in out
    assert "\n" not in out.split("Entrée")[0] or out.startswith("Entrée")


# ------------------------------------------------------------------------
# LLMClient
# ------------------------------------------------------------------------


def test_llm_client_is_configured_requires_key() -> None:
    cfg = AppConfig(minimax_api_key="")
    client = LLMClient(cfg)
    assert client.is_configured() is False
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "x"}])


# ------------------------------------------------------------------------
# SYSTEM_PROMPT v2 : phrasé naturel, pas de "Régime", strip bruit
# ------------------------------------------------------------------------


def test_llm_client_test_connection_no_key() -> None:
    cfg = AppConfig(minimax_api_key="")
    client = LLMClient(cfg)
    assert client.test_connection() == {"ok": False, "error": "Clé API non configurée"}
