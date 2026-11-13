"""Publication des menus vers une entité ``calendar.*`` de Home Assistant.

Un événement « journée entière » par jour de menu. Fonctionne avec toute
intégration qui sait créer/supprimer (Local Calendar, Google…) :

- lecture : ``GET /api/calendars/<entity>`` (REST, renvoie les ``uid``)
- création : service ``calendar.create_event`` (REST)
- suppression : commande WebSocket ``calendar/event/delete`` (pas de service)

HA ne laisse pas choisir l'UID : chaque événement porte une ligne marqueur
``ha-menu-id: <enfant>-<date>`` dans sa description. Seuls les événements
marqués sont touchés ; une modification = suppression + recréation.

Accès HA : ``SUPERVISOR_TOKEN`` dans l'app, sinon ``HA_URL`` + ``HA_TOKEN`` (dev).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta

import httpx
from websockets.sync.client import connect

from .models import Child, ExtractionResult, MenuDay

logger = logging.getLogger(__name__)

MARKER = "ha-menu-id: "


def make_key(child_id: int, jour: date) -> str:
    return f"{child_id}-{jour.isoformat()}"


def event_payload(day: MenuDay, child: Child) -> dict:
    """Données ``calendar.create_event`` pour un jour de menu."""
    title = f"🍽 Cantine {child.first_name or child.full_name}"
    label = (day.label or "").strip()
    # « Menu de <prénom> » (libellé So-Happy par défaut) n'apporte rien au titre.
    if label.lower() not in ("", "standard", "default") and not label.lower().startswith("menu de"):
        title += f" · {label}"
    return {
        "summary": title,
        "description": f"{day.summary}\n{MARKER}{make_key(child.id, day.jour)}",
        "start_date": day.jour.isoformat(),
        "end_date": (day.jour + timedelta(days=1)).isoformat(),  # fin exclusive
    }


def marker_of(description: str | None) -> str | None:
    for line in (description or "").splitlines():
        if line.startswith(MARKER):
            return line[len(MARKER):].strip()
    return None


def _fingerprint(summary: str, description: str, start: str, end: str) -> tuple:
    # HA/Google peuvent changer les fins de ligne et les espaces de bord.
    return (summary, "\n".join(description.strip().splitlines()), start, end)


def plan_sync(wanted: dict[str, dict], existing: list[dict]) -> tuple[list[dict], list[str]]:
    """Calcule (payloads à créer, uids HA à supprimer).

    Les événements sans marqueur sont ignorés ; doublons, versions obsolètes
    et orphelins marqués sont supprimés.
    """
    to_delete: list[str] = []
    kept: set[str] = set()
    for ev in existing:
        key = marker_of(ev.get("description"))
        if key is None:
            continue
        want = wanted.get(key)
        start, end = ev["start"].get("date"), ev["end"].get("date")
        if want and key not in kept and _fingerprint(
            ev.get("summary", ""), ev.get("description", ""), start or "", end or "",
        ) == _fingerprint(
            want["summary"], want["description"], want["start_date"], want["end_date"],
        ):
            kept.add(key)
        else:
            to_delete.append(ev["uid"])
    return [p for k, p in wanted.items() if k not in kept], to_delete


class HomeAssistant:
    """Client minimal REST + WebSocket vers HA, pour une entité agenda."""

    def __init__(self, entity: str, timeout: float = 30.0) -> None:
        token = os.getenv("SUPERVISOR_TOKEN") or os.getenv("HA_TOKEN", "")
        if not token:
            raise ValueError("Jeton HA manquant (SUPERVISOR_TOKEN ou HA_TOKEN).")
        default_url = "http://supervisor/core" if os.getenv("SUPERVISOR_TOKEN") else ""
        self.url = (os.getenv("HA_URL") or default_url).rstrip("/")
        if not self.url:
            raise ValueError("URL HA manquante (HA_URL).")
        self.token = token
        self.entity = entity
        self.timeout = timeout
        self.http = httpx.Client(
            headers={"Authorization": f"Bearer {token}"}, timeout=timeout,
        )

    def list_events(self, start: date, end: date) -> list[dict]:
        r = self.http.get(
            f"{self.url}/api/calendars/{self.entity}",
            params={"start": start.isoformat(), "end": end.isoformat()},
        )
        r.raise_for_status()
        return r.json()

    def create_event(self, payload: dict) -> None:
        r = self.http.post(
            f"{self.url}/api/services/calendar/create_event",
            json={"entity_id": self.entity, **payload},
        )
        r.raise_for_status()

    def delete_events(self, uids: list[str]) -> int:
        """Supprime via WebSocket ; retourne le nombre d'échecs."""
        if not uids:
            return 0
        ws_url = self.url.replace("http", "ws", 1) + "/websocket"
        errors = 0
        with connect(ws_url, open_timeout=self.timeout) as ws:
            json.loads(ws.recv())  # auth_required
            ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            if json.loads(ws.recv()).get("type") != "auth_ok":
                raise PermissionError("Authentification WebSocket HA refusée.")
            for i, uid in enumerate(uids, start=1):
                ws.send(json.dumps({
                    "id": i, "type": "calendar/event/delete",
                    "entity_id": self.entity, "uid": uid,
                }))
                res = json.loads(ws.recv(timeout=self.timeout))
                if not res.get("success"):
                    logger.warning("Échec suppression %s : %s", uid, res.get("error"))
                    errors += 1
        return errors


def list_ha_events(child: Child, lookback_days: int = 60, lookahead_days: int = 60) -> list[dict]:
    """Événements HA - Menu (marqués) de l'agenda d'un enfant."""
    today = date.today()
    events = HomeAssistant(child.ha_calendar).list_events(
        today - timedelta(days=lookback_days), today + timedelta(days=lookahead_days),
    )
    return [e for e in events if marker_of(e.get("description"))]


def publish_to_ha(
    result: ExtractionResult, child: Child, *, delete_orphans: bool = True,
) -> dict[str, object]:
    """Synchronise les jours de menu d'un enfant vers son agenda HA."""
    stats: dict[str, object] = {
        "created": 0, "updated": 0, "deleted": 0, "errors": 0, "error": None,
    }
    try:
        ha = HomeAssistant(child.ha_calendar)
        days = [d.jour for d in result.days]
        today = date.today()
        # Fenêtre : les jours publiés, élargie pour retrouver les orphelins récents.
        start = min([*days, today]) - timedelta(days=31)
        end = max([*days, today]) + timedelta(days=31)
        existing = ha.list_events(start, end)
    except Exception as exc:  # noqa: BLE001
        stats.update(error=f"Agenda HA {child.ha_calendar} : {exc}", errors=1)
        return stats

    wanted = {make_key(child.id, d.jour): event_payload(d, child) for d in result.days}
    to_create, to_delete = plan_sync(wanted, existing)
    if not delete_orphans:
        stale = {e["uid"] for e in existing if marker_of(e.get("description")) in wanted}
        to_delete = [u for u in to_delete if u in stale]
    logger.info(
        "Agenda %s : %d à créer, %d à supprimer", ha.entity, len(to_create), len(to_delete),
    )

    try:
        delete_errors = ha.delete_events(to_delete)
    except Exception as exc:  # noqa: BLE001
        stats.update(error=f"Suppression impossible : {exc}", errors=1)
        return stats
    replaced = {marker_of(e.get("description")) for e in existing if e.get("uid") in to_delete}
    for payload in to_create:
        try:
            ha.create_event(payload)
        except httpx.HTTPError as exc:
            logger.warning("Échec création %s : %s", payload["start_date"], exc)
            stats["errors"] += 1
            continue
        key = "updated" if marker_of(payload["description"]) in replaced else "created"
        stats[key] += 1
    stats["errors"] += delete_errors
    stats["deleted"] = max(0, len(to_delete) - delete_errors - stats["updated"])
    logger.info(
        "Agenda HA %s : +%d, ~%d, -%d, %d erreur(s)", child.full_name,
        stats["created"], stats["updated"], stats["deleted"], stats["errors"],
    )
    return stats
