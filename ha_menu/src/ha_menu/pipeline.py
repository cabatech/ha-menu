"""Orchestration de l'extraction : login → enfants → menus → agenda Home Assistant."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import AppConfig
from .ha_calendar import publish_to_ha
from .llm import enhance_days
from .models import Child, ExtractionResult, MenuDay
from .mqtt_publisher import publish_all as mqtt_publish
from .sohappy import SoHappyClient, SoHappyError

logger = logging.getLogger(__name__)

# Répertoire par défaut du cache menus (relatif à AppConfig.config_path).
DEFAULT_MENUS_DIR_NAME = "menus"


def discover_children(config: AppConfig) -> list[Child]:
    """Login + récupération des enfants liés au compte parent So-Happy.

    Met à jour ``config.children`` (préserve les enfants existants s'ils
    ne sont plus dans la liste, pour ne pas perdre leur agenda HA).
    """
    with SoHappyClient(config) as client:
        client.login()
        fresh = client.list_children()

    # Merge : on garde les enfants déjà configurés (avec leur agenda HA)
    # et on complète avec les métadonnées fraîches de So-Happy.
    by_id: dict[int, Child] = {c.id: c for c in config.children}
    merged: list[Child] = []
    for new in fresh:
        existing = by_id.get(new.id)
        if existing:
            # Préserve enabled + agenda HA, rafraîchit le reste.
            existing.first_name = new.first_name or existing.first_name
            existing.last_name = new.last_name or existing.last_name
            existing.school = new.school or existing.school
            existing.birth_date = new.birth_date or existing.birth_date
            existing.clients = new.clients or existing.clients
            existing.sohappy_menus = new.sohappy_menus  # toujours rafraîchir
            merged.append(existing)
        else:
            merged.append(new)

    # Enfants configurés absents du compte (compte désactivé par ex.)
    # → on les retire pour rester en phase avec So-Happy.
    fresh_ids = {c.id for c in fresh}
    merged = [c for c in merged if c.id in fresh_ids]

    config.children = merged
    return merged


def run_extraction(
    config: AppConfig,
    *,
    publish: bool = True,
    delete_orphans: bool = True,
    target_child_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Cycle complet : login → enfants → menus → agenda HA.

    Retourne un dict de stats par enfant + global. Chaque enfant peut
    être en échec (login OK mais menu inaccessible, agenda HA KO, etc.).
    """
    if not config.sohappy.is_set():
        raise SoHappyError(
            "Identifiants So-Happy manquants. Configurez-les dans "
            "l'interface web (Settings → Identifiants So-Happy).",
        )

    now = datetime.now()
    per_child: list[dict[str, Any]] = []
    overall = {"ok": True, "errors": 0, "children_ok": 0, "children_ko": 0}
    # Accumule les jours par enfant pour publication MQTT globale (1
    # seule connexion TCP au broker par sync).
    mqtt_days_by_child: dict[int, list[MenuDay]] = {}

    with SoHappyClient(config) as client:
        client.login()
        # Rafraîchit la liste d'enfants si besoin
        if not config.children:
            config.children = client.list_children()
        target_children = [
            c for c in config.enabled_children()
            if target_child_ids is None or c.id in target_child_ids
        ]
        if not target_children:
            logger.warning("Aucun enfant actif configuré.")
            return {"extracted_at": now.isoformat(), "children": per_child, **overall}

        for child in target_children:
            try:
                days = client.fetch_menus(child)
            except SoHappyError as exc:
                logger.error("Échec menus %s : %s", child.full_name, exc)
                overall["ok"] = False
                overall["errors"] += 1
                overall["children_ko"] += 1
                per_child.append({
                    "child_id": child.id,
                    "name": child.full_name,
                    "ok": False,
                    "error": str(exc),
                    "days_count": 0,
                    "ha": None,
                })
                continue

            # Reformatage via LLM (ou fallback déterministe local).
            # Ne lève jamais : un échec LLM (après retries) bascule sur le
            # format court Python (cf. models.format_menu_short).
            days, llm_stats = enhance_days(days, config=config)

            source_url = (
                f"https://www.so-happy.fr/tabs/menu-parents"
                f"?child_id={child.id}"
            )

            # Persistance cache local (pour WebUI / API REST). Ne lève pas :
            # un échec de cache ne doit pas faire échouer le sync.
            try:
                save_child_menu_cache(
                    child, days, default_menus_dir(config),
                    source_url=source_url,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Échec cache menus %s : %s", child.full_name, exc)

            result = ExtractionResult(
                extracted_at=now,
                child=child,
                days=days,
                source_url=source_url,
            )

            ha_stats: dict[str, Any] | None = None
            if publish and child.ha_calendar:
                ha_stats = publish_to_ha(result, child, delete_orphans=delete_orphans)
                if ha_stats.get("error"):
                    logger.error("Agenda HA %s : %s", child.full_name, ha_stats["error"])
                    overall["ok"] = False
                    overall["errors"] += 1
            elif publish:
                logger.warning(
                    "Pas d'agenda HA configuré pour %s — publication ignorée.",
                    child.full_name,
                )

            overall["children_ok"] += 1
            overall["llm_total"] = overall.get("llm_total", 0) + llm_stats["total"]
            overall["llm_ok"] = overall.get("llm_ok", 0) + llm_stats["llm"]
            overall["llm_fallback"] = overall.get("llm_fallback", 0) + llm_stats["fallback"]
            per_child.append({
                "child_id": child.id,
                "name": child.full_name,
                "ok": True,
                "days_count": len(days),
                "llm": llm_stats,
                "ha": ha_stats,
            })
            if llm_stats["fallback"] > 0:
                logger.warning(
                    "LLM : %d/%d jours en fallback pour %s — vérifier clé/clé API",
                    llm_stats["fallback"], llm_stats["total"], child.full_name,
                )
            # Accumulation pour publication MQTT (une seule connexion
            # par sync, après la boucle agenda).
            mqtt_days_by_child[child.id] = days

    # Publication MQTT globale (Home Assistant via EMQX). Ne lève jamais :
    # un broker HS = log warning seulement (cf. mqtt_publisher).
    if config.use_mqtt:
        try:
            mqtt_stats = mqtt_publish(
                config, target_children, mqtt_days_by_child,
            )
            overall["mqtt"] = mqtt_stats
        except Exception as exc:  # noqa: BLE001
            logger.warning("MQTT : publication globale échouée : %s", exc)
            overall["mqtt"] = {"published": 0, "errors": [str(exc)]}
    else:
        overall["mqtt"] = {"published": 0, "skipped": "disabled"}

    return {
        "extracted_at": now.isoformat(),
        "children": per_child,
        **overall,
    }


# ------------------------------------------------------------------------
# Helpers I/O
# ------------------------------------------------------------------------


def write_menus_json(
    config: AppConfig,
    results: list[ExtractionResult],
    output_dir: Path,
) -> list[Path]:
    """Sérialise les menus en JSON (un fichier par enfant) — pour debug/UI."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for r in results:
        path = output_dir / f"menus_{r.child.id}.json"
        path.write_text(
            _json_safe(r.to_dict()),
            encoding="utf-8",
        )
        written.append(path)
    return written


# ------------------------------------------------------------------------
# Cache menus local (post-sync)
# ------------------------------------------------------------------------
# Format : /data/menus/{child_id}.json
#   {
#     "child_id": 1001,
#     "child_name": "Paul",
#     "extracted_at": "2026-09-15T13:00:00",
#     "source_url": "https://www.so-happy.fr/tabs/menu-parents?child_id=1001",
#     "days": [
#       {"date": "2026-09-14", "service": "déjeuner", "label": "Menu dePaul",
#        "summary": "Entrée : ...\nPlat : ..."},
#       ...
#     ]
#   }
# Lu par la WebUI (/menus) et l'API REST (/api/menus). L'agenda HA reste la
# source de vérité calendrier ; ce cache sert juste à consulter sans
# redemander à So-Happy.


def default_menus_dir(config: AppConfig) -> Path:
    """Répertoire cache : ``<config_dir>/menus`` (à côté de config.json).

    Fallback : ``/data/menus`` si ``config.config_path`` est inconnu
    (ex : tests, CLI sans --config).
    """
    if config.config_path is not None:
        return config.config_path.parent / DEFAULT_MENUS_DIR_NAME
    return Path("/data") / DEFAULT_MENUS_DIR_NAME


def _cache_path(menus_dir: Path, child_id: int) -> Path:
    return menus_dir / f"{child_id}.json"


def save_child_menu_cache(
    child: Child,
    days: list[MenuDay],
    menus_dir: Path,
    *,
    source_url: str = "",
) -> Path:
    """Écrit le cache JSON pour un enfant (après reformatage LLM).

    Idempotent : ``os.replace`` atomique. Crée ``menus_dir`` si besoin.
    """
    menus_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "child_id": child.id,
        "child_name": child.full_name,
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        "source_url": source_url,
"days": [
                {
                    "date": d.jour.isoformat(),
                    "service": d.service,
                    "label": d.label,
                    "summary": d.summary,
                    "llm_source": (d.raw.get("llm_source") if d.raw else None),
                    "llm_raw": (d.raw.get("llm_raw") if d.raw else None),
                }
                for d in days
            ],
    }
    final = _cache_path(menus_dir, child.id)
    fd, tmp = tempfile.mkstemp(prefix=".menus_", suffix=".json.tmp", dir=str(menus_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, final)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
    logger.debug("Cache menus écrit : %s (%d jours)", final, len(days))
    return final


def load_child_menu_cache(child_id: int, menus_dir: Path) -> dict[str, Any] | None:
    """Charge le cache d'un enfant. Renvoie ``None`` si absent / corrompu."""
    path = _cache_path(menus_dir, child_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Cache menus illisible pour %s : %s", child_id, exc)
        return None


def list_cached_children(menus_dir: Path) -> list[int]:
    """Liste les ``child_id`` qui ont un cache présent."""
    if not menus_dir.exists():
        return []
    out: list[int] = []
    for p in menus_dir.glob("*.json"):
        if p.name.startswith("."):
            continue
        try:
            out.append(int(p.stem))
        except ValueError:
            continue
    return sorted(out)


def _json_safe(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def next_menu_day(child: Child, results: list[ExtractionResult]) -> MenuDay | None:
    """Renvoie le prochain MenuDay (>= aujourd'hui) pour un enfant donné."""
    today = date.today()
    for r in results:
        if r.child.id == child.id:
            for d in r.days:
                if d.jour >= today:
                    return d
    return None
