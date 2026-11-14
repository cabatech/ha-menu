"""Logique « cantine » pure (sans import Home Assistant), testée seule."""

from __future__ import annotations

from datetime import date, datetime, timedelta

UNSURE = ("unknown", "unavailable")
SWITCH_HOUR = 21  # à partir de 21 h, la carte montre le menu du lendemain


def parse_summary(summary: str) -> list[dict[str, str]]:
    """Lignes « Libellé : valeur » du résumé de l'app → liste ordonnée."""
    out = []
    for line in (summary or "").splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip():
            out.append({"label": label.strip(), "valeur": value.strip()})
    return out


def displayed_day(now: datetime) -> tuple[date, str]:
    if now.hour < SWITCH_HOUR:
        return now.date(), "aujourd_hui"
    return now.date() + timedelta(days=1), "demain"


def inscription_guichet(cantine: str | None, loisirs: str | None, repas_loisirs: bool) -> bool | None:
    """États HA bruts des capteurs Guichet Unique ; ``None`` = entité absente.

    Une entité accueil de loisirs absente vaut « non inscrit » ; une cantine
    absente ou incertaine ne permet pas de conclure, sauf inscription certaine ailleurs.
    """
    loisirs_on = repas_loisirs and loisirs == "on"
    if cantine == "on" or loisirs_on:
        return True
    if cantine is None or cantine in UNSURE:
        return None
    if repas_loisirs and loisirs in UNSURE:
        return None
    return False


def inscription_pronote(state: str | None, lessons: list[dict] | None) -> bool | None:
    """Présent le midi = au moins un cours non annulé le matin et l'après-midi."""
    if state is None or state in UNSURE:
        return None
    active = [x for x in lessons or [] if not x.get("canceled")]
    return any(x.get("is_morning") for x in active) and any(x.get("is_afternoon") for x in active)


def cantine(has_menu: bool, inscription: bool | None) -> bool | None:
    if not has_menu:
        return False
    return inscription
