"""Tests de la planification de synchro vers un agenda HA (sans réseau)."""

from __future__ import annotations

from datetime import date

from ha_menu.ha_calendar import MARKER, event_payload, marker_of, plan_sync
from ha_menu.models import Child, MenuDay

CHILD = Child(id=7, first_name="Paul")


def _day(d: date, summary: str = "Entrée : carottes") -> MenuDay:
    return MenuDay(jour=d, label="Menu", raw={"llm_summary": summary})


def _as_ha(payload: dict, uid: str) -> dict:
    """Simule le retour de GET /api/calendars (fins de ligne reformatées)."""
    return {
        "uid": uid,
        "summary": payload["summary"],
        "description": payload["description"].replace("\n", "\r\n") + "\n",
        "start": {"date": payload["start_date"]},
        "end": {"date": payload["end_date"]},
    }


def test_payload_is_all_day_and_marked() -> None:
    p = event_payload(_day(date(2026, 11, 16)), CHILD)
    assert p["start_date"] == "2026-11-16"
    assert p["end_date"] == "2026-11-17"
    assert marker_of(p["description"]) == "7-2026-11-16"


def test_plan_sync_keep_update_delete_and_ignore_foreign() -> None:
    d1, d2, d3 = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)
    same = event_payload(_day(d1), CHILD)
    old = event_payload(_day(d2, "Entrée : ancien"), CHILD)
    orphan = event_payload(_day(d3), CHILD)
    foreign = {"uid": "f", "summary": "Dentiste", "description": "rdv",
               "start": {"date": "2026-11-16"}, "end": {"date": "2026-11-17"}}
    existing = [_as_ha(same, "a"), _as_ha(same, "a-dup"), _as_ha(old, "b"),
                _as_ha(orphan, "c"), foreign]
    wanted = {
        "7-2026-11-16": same,
        "7-2026-11-17": event_payload(_day(d2, "Entrée : nouveau"), CHILD),
    }

    to_create, to_delete = plan_sync(wanted, existing)

    assert [p["start_date"] for p in to_create] == ["2026-11-17"]
    assert sorted(to_delete) == ["a-dup", "b", "c"]  # doublon, obsolète, orphelin ; "f" intact


def test_marker_ignores_unmarked() -> None:
    assert marker_of("rien ici") is None
    assert marker_of(f"x\n{MARKER}7-2026-01-01") == "7-2026-01-01"


def test_title_drops_default_label() -> None:
    assert event_payload(_day(date(2026, 11, 16)), CHILD)["summary"] == "🍽 Cantine Paul · Menu"
    day = MenuDay(jour=date(2026, 11, 16), label="Menu de Paul")
    assert event_payload(day, CHILD)["summary"] == "🍽 Cantine Paul"
