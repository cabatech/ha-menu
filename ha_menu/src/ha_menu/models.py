"""Modèle de données pour les menus cantine So-Happy."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class MealCategory(StrEnum):
    """Catégorie de plat retournée par l'API So-Happy."""

    ENTRY = "entree"
    MAIN = "plat"
    SIDE = "accompagnement"
    DAIRY = "produit_laitier"
    DESSERT = "dessert"
    OTHER = "autre"

    @property
    def label(self) -> str:
        labels = {
            self.ENTRY: "Entrée",
            self.MAIN: "Plat",
            self.SIDE: "Accompagnement",
            self.DAIRY: "Produit laitier",
            self.DESSERT: "Dessert",
            self.OTHER: "Autre",
        }
        return labels[self]


@dataclass(slots=True)
class MenuItem:
    """Un plat composant un menu."""

    label: str
    category: MealCategory = MealCategory.OTHER
    allergens: list[str] = field(default_factory=list)
    is_vegetarian: bool = False
    is_organic: bool = False
    is_local: bool = False
    is_homemade: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "category": self.category.value,
            "allergens": list(self.allergens),
            "is_vegetarian": self.is_vegetarian,
            "is_organic": self.is_organic,
            "is_local": self.is_local,
            "is_homemade": self.is_homemade,
        }


@dataclass(slots=True)
class MenuDay:
    """Le menu d'un jour pour un enfant."""

    jour: date
    service: str = "cantine"  # ex: cantine, cantine_adulte
    items: list[MenuItem] = field(default_factory=list)
    label: str = ""  # ex: "Repas standard", "Repas sans porc"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """Résumé court 5 lignes (Entrée / Plat / Garnitures / Laitage / Dessert).

        Si ``raw['llm_summary']`` est présent (renvoyé par le module LLM), on
        l'utilise tel quel. Sinon, on produit le rendu déterministe local
        (utilisé comme fallback quand le LLM est désactivé ou indisponible).
        """
        llm_text = self.raw.get("llm_summary")
        if isinstance(llm_text, str) and llm_text.strip():
            return llm_text.strip()
        return format_menu_short(self)

    @property
    def detailed_description(self) -> str:
        """Description verbeuse avec allergènes + flags (debug / outils CLI).

        Différent de ``summary`` (court, agenda) — utilisé par ``cmd_check``
        et la WebUI pour inspecter ce qui vient vraiment de So-Happy.
        """
        if not self.items:
            return "Menu indisponible"
        parts: list[str] = []
        for item in self.items:
            tag = ""
            if item.is_vegetarian:
                tag = " 🌱"
            elif item.is_organic:
                tag = " 🌿"
            parts.append(f"{item.category.label} : {item.label}{tag}")
            if item.allergens:
                parts.append(f"    allergènes : {', '.join(item.allergens)}")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "jour": self.jour.isoformat(),
            "service": self.service,
            "label": self.label,
            "items": [i.to_dict() for i in self.items],
            "summary": self.summary,
        }


# ------------------------------------------------------------------------
# Format court déterministe — utilisé en fallback quand le LLM est KO
# ------------------------------------------------------------------------

# Libellés courts fixes pour l'affichage agenda (cf. AGENTS.md « format LLM »).
SHORT_CATEGORY_LABELS: dict[str, str] = {
    MealCategory.ENTRY.value: "Entrée",
    MealCategory.MAIN.value: "Plat",
    MealCategory.SIDE.value: "Garnitures",
    MealCategory.DAIRY.value: "Laitage",
    MealCategory.DESSERT.value: "Dessert",
}

# Ordre d'affichage des lignes (les catégories absentes restent affichées vides).
SHORT_CATEGORY_ORDER: tuple[str, ...] = (
    MealCategory.ENTRY.value,
    MealCategory.MAIN.value,
    MealCategory.SIDE.value,
    MealCategory.DAIRY.value,
    MealCategory.DESSERT.value,
)


# Regex de bruit à retirer des libellés de plats (menus familiaux).
# - (Elémentaire), (Maternelle), (de porc), (VF), (PMD)... : mentions parasites.
# Note : So-Happy écrit « Elémentaire » (1 seul é) — on accepte les deux.
# Les flags (BIO, LOCAL, etc.) sont déjà exclus via MenuItem.is_* et filtrés
# à la source par le LLM ; ici on s'occupe juste des chaînes parasites.
_NOISE_PAREN_RE = re.compile(
    r"\s*\([^)]*(?:[EÉ]l[ée]mentaire|Maternelle|Primaire|VF|PMD|LR|AOP|BIO|LOCAL)"
    r"[^)]*\)",
    re.IGNORECASE,
)
_NOISE_PAREN_GENERIC_RE = re.compile(r"\s*\([^)]*(?:de porc|de veau)\)", re.IGNORECASE)


def _clean_label(label: str) -> str:
    """Nettoie un libellé de plat : retire parenthèses parasites, espaces."""
    out = _NOISE_PAREN_RE.sub("", label)
    out = _NOISE_PAREN_GENERIC_RE.sub("", out)
    # Tokens inline parasites (qualificatifs fournisseurs/certifs) :
    # « PMD » (poisson durable), « VF » (viande française), « LR » (label rouge),
    # « BIO » (souvent remonté comme flag mais aussi inline), « AOP/AOC ».
    # On les retire uniquement comme mots entiers (entourés d'espaces).
    out = re.sub(r"\s+(?:PMD|VF|LR|AOP|AOC|BIO)\b", "", out, flags=re.IGNORECASE)
    # « et vinaigrette… » en début de label parasite quand collé à la
    # catégorie précédente → minuscule après « et » est OK, on retire juste
    # le « et » initial quand la phrase commence par « et / et sa / et son ».
    out = re.sub(r"^(?:et|et sa|et son|et ses|sa|son|ses)\s+", "", out, flags=re.IGNORECASE)
    # Espaces multiples en début/fin.
    return out.strip()


def format_menu_short(day: MenuDay) -> str:
    """Produit le rendu court 5 lignes (Entrée/Plat/Garnitures/Laitage/Dessert).

    - Plusieurs choix d'une même catégorie sont joints par `` / `` (le LLM,
      quand actif, cherche à fusionner pour un rendu plus naturel — c'est
      précisément la valeur ajoutée du LLM vs ce fallback déterministe).
    - Pas d'emoji, pas d'allergène, pas de flag végé/bio.
    - Les catégories absentes renvoient une ligne ``"Catégorie :"`` vide
      (utile pour repérer d'un coup d'œil un menu incomplet).
    - Utilisé en fallback quand le LLM n'est pas configuré / indisponible.
      Le LLM (quand activé) vise à produire exactement la même structure,
      mais avec un phrasé plus naturel.
    """
    grouped: dict[str, list[str]] = {code: [] for code in SHORT_CATEGORY_ORDER}
    for item in day.items:
        code = item.category.value
        if code not in grouped:
            # Catégorie « Autre / Goûter » → on l'ignore dans le format court.
            continue
        label = _clean_label(item.label or "")
        if label and label not in grouped[code]:
            grouped[code].append(label)
    lines: list[str] = []
    for code in SHORT_CATEGORY_ORDER:
        cat_label = SHORT_CATEGORY_LABELS[code]
        choices = grouped[code]
        lines.append(f"{cat_label} : {' / '.join(choices)}")
    if not day.items:
        return "Menu indisponible"
    return "\n".join(lines)


@dataclass(slots=True)
class Child:
    """Un enfant lié au compte parent So-Happy."""

    id: int
    first_name: str = ""
    last_name: str = ""
    school: str = ""
    birth_date: date | None = None
    clients: list[dict[str, Any]] = field(default_factory=list)
    # Menus So-Happy découverts via ?verbose=true (utilisés pour fetch_menus)
    sohappy_menus: list[dict[str, Any]] = field(default_factory=list)
    # Sélection / agenda HA cible (géré par le front, pas par So-Happy)
    enabled: bool = True
    ha_calendar: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()

    def to_dict(self) -> dict[str, Any]:
        # Récapitulatif des menus So-Happy (pour affichage debug UI)
        menus_summary = [
            {
                "id": m.get("id"),
                "name": m.get("name"),
                "client_id": (m.get("client") or {}).get("id"),
                "school_id": m.get("school_id"),
                "menu_type_id": m.get("menu_type_id"),
                "city_id": m.get("city_id"),
                "zone_restauration_id": m.get("zone_restauration_id"),
            }
            for m in (self.sohappy_menus or [])
        ]
        return {
            "id": self.id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "school": self.school,
            "birth_date": self.birth_date.isoformat() if self.birth_date else None,
            "enabled": self.enabled,
            "ha_calendar": self.ha_calendar,
            "sohappy_menus": menus_summary,
            "has_menus": len(menus_summary) > 0,
        }


@dataclass(slots=True)
class ExtractionResult:
    """Résultat d'une extraction pour un enfant."""

    extracted_at: datetime
    child: Child
    days: list[MenuDay] = field(default_factory=list)
    source_url: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "extracted_at": self.extracted_at.isoformat(),
            "child": self.child.to_dict(),
            "days": [d.to_dict() for d in self.days],
            "source_url": self.source_url,
            "error": self.error,
        }


def classify_category(raw_label: str) -> MealCategory:
    """Mappe un code catégorie So-Happy vers MealCategory.

    Codes observés dans la vraie API (juin 2025+) :
        ENTREE, PLAT, GARNITURE, LAITAGE, DESSERT, GOUTER.
    On garde aussi les anciens codes (entree/plat/accompagnement/...) pour
    rétrocompat avec d'anciens formats.
    """
    norm = raw_label.strip().lower()
    mapping = {
        # Codes réels 2025+
        "entree": MealCategory.ENTRY,
        "entrée": MealCategory.ENTRY,
        "starter": MealCategory.ENTRY,
        "plat": MealCategory.MAIN,
        "plat_principal": MealCategory.MAIN,
        "main": MealCategory.MAIN,
        "garniture": MealCategory.SIDE,
        "side": MealCategory.SIDE,
        "accompagnement": MealCategory.SIDE,
        "laitage": MealCategory.DAIRY,
        "produit_laitier": MealCategory.DAIRY,
        "laitier": MealCategory.DAIRY,
        "fromage": MealCategory.DAIRY,
        "dessert": MealCategory.DESSERT,
        "gouter": MealCategory.OTHER,  # goûter = collation
    }
    return mapping.get(norm, MealCategory.OTHER)


def detect_label_flags(raw: dict[str, Any]) -> dict[str, bool]:
    """Extrait les flags (vegetarian/organic/…) depuis le payload brut."""
    flags = {
        "is_vegetarian": False,
        "is_organic": False,
        "is_local": False,
        "is_homemade": False,
    }
    for key in ("is_vegetarian", "vegetarian", "is_veggie", "veggie", "vegetal"):
        if raw.get(key):
            flags["is_vegetarian"] = True
            break
    for key in ("is_organic", "organic", "bio"):
        if raw.get(key):
            flags["is_organic"] = True
            break
    for key in ("is_local", "local"):
        if raw.get(key):
            flags["is_local"] = True
            break
    for key in ("is_homemade", "homemade", "fait_maison"):
        if raw.get(key):
            flags["is_homemade"] = True
            break
    return flags
