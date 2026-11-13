"""Publication MQTT (Home Assistant via EMQX) pour intégration domotique.

Aucun ajout côté stack (pas de broker local). Config dans ``AppConfig`` :
    - ``use_mqtt``        : False par défaut (publication désactivée)
    - ``mqtt_host``       : IP/hostname du broker (EMQX addon HAOS typique)
    - ``mqtt_port``       : 1883 par défaut (8883 si TLS)
    - ``mqtt_username``   / ``mqtt_password`` : auth EMQX
    - ``mqtt_base_topic`` : préfixe des topics (défaut "ha_menu")

Pour chaque enfant actif, publie 11 entités HA :
    1. 5 sensors "today" par catégorie :
       ``sensor.ha_menu_<child>_today_entree``, ``_plat``,
       ``_garnitures``, ``_laitage``, ``_dessert``.
       State = string simple (pluriel des plats joints par ' / '),
       pas de JSON. Empty string si pas de menu ce jour-là.
    2. 5 sensors "tomorrow" (mêmes noms suffixés ``_tomorrow_<cat>``).
    3. 1 calendar ``calendar.ha_menu_<child>`` avec tous les jours
       à venir en JSON array.

Topics :
    Discovery : ``homeassistant/sensor/ha_menu_<child>_today_<cat>/config``
    State     : ``{base_topic}/{child_slug}/today/<cat>``

Aucune exception ne remonte au pipeline : un broker HS = log warning
seulement.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from .config import AppConfig
from .models import Child, MenuDay

logger = logging.getLogger(__name__)

# Client MQTT importé paresseusement (publish()) — permet au module de
# charger même si paho-mqtt n'est pas installé (utile pour les tests et
# pour les installations minimales sans Home Assistant).
mqtt: Any = None
try:
    import paho.mqtt.client as _paho_client  # type: ignore[import]
    mqtt = _paho_client
except ImportError:
    pass


# ------------------------------------------------------------------------
# Découverte Home Assistant (MQTT)
# ------------------------------------------------------------------------

HA_DEVICE_BASE = "ha_menu"
APP_VERSION = "0.4.0"  # synchronisé à la main avec __init__.py


def _device_block(child: Child) -> dict[str, Any]:
    """Bloc ``device`` commun à toutes les entités HA d'un enfant."""
    slug = _slug(child)
    return {
        "identifiers": [f"{HA_DEVICE_BASE}_{slug}"],
        "name": f"Cantine {child.full_name or child.id}",
        "manufacturer": "HA - Menu",
        "model": f"v{APP_VERSION}",
        "sw_version": APP_VERSION,
    }


def _slug(child: Child) -> str:
    """Identifiant MQTT-safe pour un enfant (alphanumeric + underscore)."""
    name = (child.full_name or str(child.id)).lower()
    return "".join(c if c.isalnum() else "_" for c in name).strip("_") or str(child.id)


def _discovery_topic_prefix(child: Child) -> str:
    return f"homeassistant"


def _state_topic(config: AppConfig, child: Child, suffix: str) -> str:
    return f"{config.mqtt_base_topic}/{_slug(child)}/{suffix}"


def _config_topic(child: Child, domain: str, suffix: str) -> str:
    return f"homeassistant/{domain}/{HA_DEVICE_BASE}_{_slug(child)}_{suffix}/config"


def _build_discovery_messages(
    config: AppConfig, child: Child,
) -> list[tuple[str, dict[str, Any]]]:
    """Construit la liste (topic, payload) pour la discovery HA.

    Par enfant : 5 sensors "today" (1 par catégorie) + 5 sensors "tomorrow"
    + 1 calendar = 11 entités. State simple (string), pas de JSON.
    Topics retain=True pour que HA les voie après reboot.
    """
    from .models import SHORT_CATEGORY_LABELS, SHORT_CATEGORY_ORDER

    device = _device_block(child)

    # Icônes par catégorie (Heroicons / Material Design Icons cohérents).
    cat_icons = {
        "entree": "mdi:leaf",
        "plat": "mdi:food-drumstick",
        "garnitures": "mdi:food-variant",
        "laitage": "mdi:cheese",
        "dessert": "mdi:cake-variant",
    }

    out: list[tuple[str, dict[str, Any]]] = []
    slug = _slug(child)
    # 5 sensors today_<cat> ------------------------------------------------
    for code in SHORT_CATEGORY_ORDER:
        cat_key = _category_to_topic_key(code)
        cat_label = SHORT_CATEGORY_LABELS[code]
        suffix = f"today_{cat_key}"
        out.append((
            _config_topic(child, "sensor", suffix),
            {
                "name": f"{cat_label} aujourd'hui — {child.full_name or child.id}",
                "state_topic": _state_topic(config, child, f"today/{cat_key}"),
                "icon": cat_icons.get(cat_key, "mdi:silverware-fork-knife"),
                "unique_id": f"{HA_DEVICE_BASE}_{slug}_{suffix}",
                "device": device,
            },
        ))
    # 5 sensors tomorrow_<cat> --------------------------------------------
    for code in SHORT_CATEGORY_ORDER:
        cat_key = _category_to_topic_key(code)
        cat_label = SHORT_CATEGORY_LABELS[code]
        suffix = f"tomorrow_{cat_key}"
        out.append((
            _config_topic(child, "sensor", suffix),
            {
                "name": f"{cat_label} demain — {child.full_name or child.id}",
                "state_topic": _state_topic(config, child, f"tomorrow/{cat_key}"),
                "icon": cat_icons.get(cat_key, "mdi:silverware-fork-knife"),
                "unique_id": f"{HA_DEVICE_BASE}_{slug}_{suffix}",
                "device": device,
            },
        ))
    # 1 calendar ---------------------------------------------------------
    out.append((
        _config_topic(child, "calendar", "calendar"),
        {
            "name": f"Cantine — {child.full_name or child.id}",
            "calendar": {"topic": _state_topic(config, child, "calendar")},
            "unique_id": f"{HA_DEVICE_BASE}_{slug}_calendar",
            "device": device,
        },
    ))
    return out


def _category_to_topic_key(code: str) -> str:
    """Mappe MealCategory.value → clé sluguée pour les topics.

    - entrée → entree
    - plat → plat
    - accompagnement → garnitures (alias historique)
    - produit_laitier → laitage
    - dessert → dessert
    """
    return {
        "entree": "entree",
        "plat": "plat",
        "accompagnement": "garnitures",
        "produit_laitier": "laitage",
        "dessert": "dessert",
    }.get(code, code)


def _build_state_payloads(
    config: AppConfig, child: Child, days: list[MenuDay],
) -> list[tuple[str, str]]:
    """Construit les payloads state, un (topic, string) par sensor.

    - 5 strings "today/<cat>" par catégorie (state = texte simple,
      pas de JSON). Empty string si pas de menu ce jour-là.
    - 5 strings "tomorrow/<cat>" par catégorie (idem pour demain).
    - 1 JSON array "calendar" (tous les jours à venir).

    Les states sont des ``str`` simples (HA sensor state classique),
    pas des ``dict`` — l'utilisateur a demandé des entités séparées
    par catégorie, donc chaque sensor expose juste la valeur utile.
    """
    from .models import SHORT_CATEGORY_ORDER, format_menu_short

    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    by_date = _group_by_date(days)

    today_day = by_date.get(today.isoformat())
    tomorrow_day = by_date.get(tomorrow.isoformat())

    out: list[tuple[str, str]] = []

    def _parts(day_obj: MenuDay | None) -> dict[str, str]:
        """Pour un MenuDay, retourne dict {cat_key: 'plat1 / plat2'}."""
        if day_obj is None:
            return {}
        # On parse le summary court plutôt que d'agréger depuis day.items
        # (qui pourrait donner des libellés sales non nettoyés par LLM).
        # Si pas de summary, on tombe sur le fallback format_menu_short.
        summary = day_obj.summary or format_menu_short(day_obj)
        result: dict[str, str] = {}
        for line in summary.splitlines():
            for code in SHORT_CATEGORY_ORDER:
                cat_key = _category_to_topic_key(code)
                cat_label = _short_label(code)
                prefix = f"{cat_label} :"
                if line.startswith(prefix):
                    value = line[len(prefix):].strip()
                    result[cat_key] = value
                    break
        return result

    today_parts = _parts(today_day)
    tomorrow_parts = _parts(tomorrow_day)

    for code in SHORT_CATEGORY_ORDER:
        cat_key = _category_to_topic_key(code)
        out.append((
            _state_topic(config, child, f"today/{cat_key}"),
            today_parts.get(cat_key, ""),
        ))
    for code in SHORT_CATEGORY_ORDER:
        cat_key = _category_to_topic_key(code)
        out.append((
            _state_topic(config, child, f"tomorrow/{cat_key}"),
            tomorrow_parts.get(cat_key, ""),
        ))

    # Calendar (json array)
    calendar_events: list[dict[str, Any]] = []
    for day in sorted(days, key=lambda d: d.jour):
        if day.jour < today:
            continue
        summary = day.summary.strip()
        if not summary:
            continue
        calendar_events.append({
            "uid": f"ha_menu-{child.id}-{day.jour.isoformat()}",
            "summary": f"Cantine {child.first_name or child.full_name or child.id}",
            "start": day.jour.isoformat(),
            "end": (day.jour + timedelta(days=1)).isoformat(),
            "description": summary,
            "all_day": True,
        })
    out.append((
        _state_topic(config, child, "calendar"),
        json.dumps(calendar_events, ensure_ascii=False),
    ))
    return out


def _short_label(code: str) -> str:
    """Libellé court de la catégorie (Entrée/Plat/Garnitures/Laitage/Dessert)."""
    from .models import SHORT_CATEGORY_LABELS
    return SHORT_CATEGORY_LABELS.get(code, code)


def _group_by_date(days: list[MenuDay]) -> dict[str, MenuDay]:
    return {d.jour.isoformat(): d for d in days if d.items}


# ------------------------------------------------------------------------
# Publisher
# ------------------------------------------------------------------------


class MQTTPublisher:
    """Publish-once vers un broker MQTT. Connexion par appel pour éviter
    de garder un client TCP ouvert dans le daemon.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def is_configured(self) -> bool:
        return bool(self.config.use_mqtt and self.config.mqtt_host)

    def publish(
        self, children: list[Child], days_by_child: dict[int, list[MenuDay]],
    ) -> dict[str, Any]:
        """Publie discovery + state pour tous les enfants.

        ``days_by_child[child_id]`` = liste des jours pour cet enfant.

        Renvoie un dict ``{"published": N, "errors": [...]}`` pour les
        logs / stats du sync.
        """
        if not self.is_configured():
            return {"published": 0, "errors": [], "skipped": "not_configured"}

        # Import paresseux pour ne pas casser un environnement sans paho.
        if mqtt is None:
            logger.warning(
                "MQTT : paho-mqtt non installé, publication ignorée. "
                "Ajoutez 'paho-mqtt>=1.6' à pyproject.toml [project.dependencies].",
            )
            return {"published": 0, "errors": ["paho-mqtt absent"], "skipped": "no_client"}

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"ha_menu-{datetime.now().timestamp():.0f}",
        )
        if self.config.mqtt_username:
            client.username_pw_set(
                self.config.mqtt_username, self.config.mqtt_password,
            )
        published = 0
        errors: list[str] = []
        # Connexion synchrone : on évite le threading daemon pour rester
        # dans le thread du pipeline.
        try:
            client.connect(
                self.config.mqtt_host,
                self.config.mqtt_port,
                keepalive=30,
            )
            client.loop_start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "MQTT : connexion à %s:%s impossible : %s",
                self.config.mqtt_host, self.config.mqtt_port, exc,
            )
            return {"published": 0, "errors": [str(exc)]}

        try:
            for child in children:
                days = days_by_child.get(child.id, [])
                # 1. Discovery configs (retain) — HA les voit une fois
                for topic, payload in _build_discovery_messages(self.config, child):
                    try:
                        info = client.publish(
                            topic,
                            json.dumps(payload, ensure_ascii=False),
                            qos=self.config.mqtt_qos,
                            retain=self.config.mqtt_retain,
                        )
                        info.wait_for_publish(timeout=self.config.mqtt_timeout)
                        published += 1
                    except Exception as exc:  # noqa: BLE001
                        err = f"{topic}: {exc}"
                        errors.append(err)
                        logger.warning("MQTT publish KO %s : %s", topic, exc)
                # 2. State (retain aussi pour persister J/J+1)
                for topic, payload in _build_state_payloads(self.config, child, days):
                    try:
                        info = client.publish(
                            topic,
                            json.dumps(payload, ensure_ascii=False),
                            qos=self.config.mqtt_qos,
                            retain=self.config.mqtt_retain,
                        )
                        info.wait_for_publish(timeout=self.config.mqtt_timeout)
                        published += 1
                    except Exception as exc:  # noqa: BLE001
                        err = f"{topic}: {exc}"
                        errors.append(err)
                        logger.warning("MQTT publish KO %s : %s", topic, exc)
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass

        return {"published": published, "errors": errors}


def publish_all(
    config: AppConfig,
    children: list[Child],
    days_by_child: dict[int, list[MenuDay]],
) -> dict[str, Any]:
    """Helper fonctionnel — publie et logge le résultat."""
    if not children:
        return {"published": 0, "errors": [], "skipped": "no_children"}
    publisher = MQTTPublisher(config)
    stats = publisher.publish(children, days_by_child)
    if stats.get("published", 0) > 0:
        logger.info(
            "MQTT : %d message(s) publié(s) sur %s:%s (erreurs=%d)",
            stats["published"], config.mqtt_host, config.mqtt_port,
            len(stats.get("errors") or []),
        )
    elif stats.get("errors"):
        logger.warning(
            "MQTT : publication échouée (%d erreur(s))",
            len(stats["errors"]),
        )
    return stats
