"""Configuration persistante de ha_menu.

La config est stockée dans un fichier JSON (``config.json``) monté en volume
dans le container et éditable via l'UI web. Les credentials sensibles
(``sohappy.password``, clés API) ne sont jamais
sérialisés vers l'extérieur du container (jamais loggés, jamais exportés
en clair dans une réponse API).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Self

from dotenv import load_dotenv

from .models import Child

load_dotenv()

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_DIR = Path("/data")
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"
DEFAULT_API_BASE = "https://apim-production.so-happy.fr/api-app"
# Clé d'abonnement Azure API Management — extraite du bundle JS public
# (`main.*.js` du SPA so-happy.fr). Cette clé est embarquée côté client donc
# publique. Si l'API change de déploiement, l'utilisateur peut l'override via
# `Settings → Configuration → api_subscription_key` dans la WebUI.
DEFAULT_OCP_APIM_SUBSCRIPTION_KEY = "7d777a5c9c7a4def8f8e756688a0326a"

# --- LLM (reformatage des menus via API compatible OpenAI) ---------------
DEFAULT_MINIMAX_BASE_URL = "https://api.minimax.io"
DEFAULT_MINIMAX_MODEL = "MiniMax-M2.7"
DEFAULT_MINIMAX_TIMEOUT = 30.0
# Format d'endpoint MiniMax : "anthropic" (par défaut, recommandé — permet
# de désactiver le thinking via thinking.type=disabled) ou "openai".
DEFAULT_MINIMAX_ENDPOINT = "anthropic"
ANTHROPIC_API_VERSION = "2023-06-01"

# --- MQTT (publication vers Home Assistant via EMQX) -------------------
DEFAULT_MQTT_HOST = ""
DEFAULT_MQTT_PORT = 1883
DEFAULT_MQTT_USERNAME = ""
DEFAULT_MQTT_PASSWORD = ""
DEFAULT_MQTT_BASE_TOPIC = "ha_menu"
DEFAULT_MQTT_TIMEOUT = 10.0
DEFAULT_MQTT_QOS = 1
DEFAULT_MQTT_RETAIN = True


@dataclass(slots=True)
class SoHappyCredentials:
    """Identifiants So-Happy (compte parent)."""

    email: str = ""
    password: str = ""

    def is_set(self) -> bool:
        return bool(self.email and self.password)


@dataclass(slots=True)
class AppConfig:
    """Configuration complète, sérialisée en JSON."""

    sohappy: SoHappyCredentials = field(default_factory=SoHappyCredentials)
    children: list[Child] = field(default_factory=list)
    # Chemin du fichier config.json (pas sérialisé — utilisé pour dériver
    # l'emplacement du cache menus / logs / autres artefacts).
    config_path: Path | None = None
    api_base: str = DEFAULT_API_BASE
    api_subscription_key: str = DEFAULT_OCP_APIM_SUBSCRIPTION_KEY
    sync_cron: str = "0 2 * * *"  # 02h00 Paris tous les jours
    auto_sync: bool = True
    sync_lookback_days: int = 1
    sync_lookahead_days: int = 14
    timezone: str = "Europe/Paris"
    request_timeout: float = 30.0
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    # --- LLM (reformatage des menus via API compatible OpenAI) -----------
    use_llm_for_menus: bool = False
    minimax_api_key: str = ""
    minimax_base_url: str = DEFAULT_MINIMAX_BASE_URL
    minimax_model: str = DEFAULT_MINIMAX_MODEL
    minimax_timeout: float = DEFAULT_MINIMAX_TIMEOUT
    # Format d'endpoint ("anthropic" ou "openai", voir DEFAULT_MINIMAX_ENDPOINT).
    minimax_endpoint: str = DEFAULT_MINIMAX_ENDPOINT
    # Nombre de tentatives en cas d'échec de l'API avant fallback local.
    # 0 = pas de retry, 2 (défaut) = 3 essais au total.
    llm_max_retries: int = 2
    # Délai initial entre tentatives (backoff exponentiel : delay * 2^attempt).
    llm_retry_delay: float = 0.5
    # --- MQTT (Home Assistant via EMQX) ---------------------------------
    use_mqtt: bool = False
    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: int = DEFAULT_MQTT_PORT
    mqtt_username: str = DEFAULT_MQTT_USERNAME
    mqtt_password: str = DEFAULT_MQTT_PASSWORD
    mqtt_base_topic: str = DEFAULT_MQTT_BASE_TOPIC
    mqtt_timeout: float = DEFAULT_MQTT_TIMEOUT
    mqtt_qos: int = DEFAULT_MQTT_QOS
    mqtt_retain: bool = DEFAULT_MQTT_RETAIN

    @classmethod
    def empty(cls) -> Self:
        return cls()

    def enabled_children(self) -> list[Child]:
        return [c for c in self.children if c.enabled]

    def find_child(self, child_id: int) -> Child | None:
        for c in self.children:
            if c.id == child_id:
                return c
        return None


class ConfigError(RuntimeError):
    """Erreur de configuration (lecture, validation)."""


# ------------------------------------------------------------------------
# Sérialisation / IO
# ------------------------------------------------------------------------


def _child_to_dict(c: Child, *, include_password: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": c.id,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "school": c.school,
        "birth_date": c.birth_date.isoformat() if c.birth_date else None,
        "enabled": c.enabled,
        "ha_calendar": c.ha_calendar,
        "sohappy_menus": list(c.sohappy_menus or []),
    }
    return d


def _child_from_dict(d: dict[str, Any]) -> Child:
    bd_raw = d.get("birth_date")
    bd = date.fromisoformat(bd_raw) if bd_raw else None
    return Child(
        id=int(d["id"]),
        first_name=d.get("first_name", ""),
        last_name=d.get("last_name", ""),
        school=d.get("school", ""),
        birth_date=bd,
        clients=list(d.get("clients", [])),
        sohappy_menus=list(d.get("sohappy_menus", [])),
        enabled=bool(d.get("enabled", True)),
        ha_calendar=d.get("ha_calendar", ""),
    )


def to_dict(cfg: AppConfig, *, include_passwords: bool = False) -> dict[str, Any]:
    """Sérialise la config en dict. Les mots de passe sont masqués par défaut."""
    return {
        "sohappy": {
            "email": cfg.sohappy.email,
            "password_set": bool(cfg.sohappy.password),
        },
        "sohappy_full": (
            {"email": cfg.sohappy.email, "password": cfg.sohappy.password}
            if include_passwords
            else None
        ),
        "children": [_child_to_dict(c, include_password=include_passwords) for c in cfg.children],
        "api_base": cfg.api_base,
        "api_subscription_key": cfg.api_subscription_key,
        "sync_cron": cfg.sync_cron,
        "auto_sync": cfg.auto_sync,
        "sync_lookback_days": cfg.sync_lookback_days,
        "sync_lookahead_days": cfg.sync_lookahead_days,
        "timezone": cfg.timezone,
        # LLM : la clé API n'est JAMAIS exportée en clair, juste *_set.
        "use_llm_for_menus": cfg.use_llm_for_menus,
        "minimax_api_key_set": bool(cfg.minimax_api_key),
        "minimax_base_url": cfg.minimax_base_url,
        "minimax_model": cfg.minimax_model,
        "minimax_full": (
            {
                "api_key": cfg.minimax_api_key,
                "base_url": cfg.minimax_base_url,
                "model": cfg.minimax_model,
                "timeout": cfg.minimax_timeout,
            }
            if include_passwords
            else None
        ),
    }


def load(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Charge la config depuis le JSON. Retourne une config vide si absent."""
    if not path.exists():
        logger.info("Pas de %s, retour config vide.", path)
        return AppConfig.empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config corrompue : {exc}") from exc

    sohappy_raw = data.get("sohappy") or {}
    sohappy = SoHappyCredentials(
        email=sohappy_raw.get("email", ""),
        password=sohappy_raw.get("password", ""),
    )

    children = [_child_from_dict(c) for c in data.get("children", [])]

    return AppConfig(
        sohappy=sohappy,
        children=children,
        config_path=path,
        api_base=data.get("api_base", DEFAULT_API_BASE),
        api_subscription_key=data.get(
            "api_subscription_key", DEFAULT_OCP_APIM_SUBSCRIPTION_KEY,
        ),
        sync_cron=data.get("sync_cron", "0 2 * * *"),
        auto_sync=bool(data.get("auto_sync", True)),
        sync_lookback_days=int(data.get("sync_lookback_days", 1)),
        sync_lookahead_days=int(data.get("sync_lookahead_days", 14)),
        timezone=data.get("timezone", "Europe/Paris"),
        request_timeout=float(data.get("request_timeout", 30.0)),
        # LLM
        use_llm_for_menus=bool(data.get("use_llm_for_menus", False)),
        minimax_api_key=data.get("minimax_api_key", "") or "",
        minimax_base_url=data.get("minimax_base_url", DEFAULT_MINIMAX_BASE_URL),
        minimax_model=data.get("minimax_model", DEFAULT_MINIMAX_MODEL),
        minimax_timeout=float(data.get("minimax_timeout", DEFAULT_MINIMAX_TIMEOUT)),
        minimax_endpoint=data.get("minimax_endpoint", DEFAULT_MINIMAX_ENDPOINT),
        llm_max_retries=int(data.get("llm_max_retries", 2)),
        llm_retry_delay=float(data.get("llm_retry_delay", 0.5)),
        # MQTT
        use_mqtt=bool(data.get("use_mqtt", False)),
        mqtt_host=data.get("mqtt_host", DEFAULT_MQTT_HOST),
        mqtt_port=int(data.get("mqtt_port", DEFAULT_MQTT_PORT)),
        mqtt_username=data.get("mqtt_username", DEFAULT_MQTT_USERNAME) or "",
        mqtt_password=data.get("mqtt_password", DEFAULT_MQTT_PASSWORD) or "",
        mqtt_base_topic=data.get("mqtt_base_topic", DEFAULT_MQTT_BASE_TOPIC),
        mqtt_timeout=float(data.get("mqtt_timeout", DEFAULT_MQTT_TIMEOUT)),
        mqtt_qos=int(data.get("mqtt_qos", DEFAULT_MQTT_QOS)),
        mqtt_retain=bool(data.get("mqtt_retain", DEFAULT_MQTT_RETAIN)),
    )


def save(cfg: AppConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Sauvegarde atomique (write sur tmp puis rename) + chmod 600."""
    path.parent.mkdir(parents=True, exist_ok=True)

    # Sérialise enfants (avec mots de passe) et creds sohappy.
    payload = {
        "sohappy": {
            "email": cfg.sohappy.email,
            "password": cfg.sohappy.password,
        },
        "children": [_child_to_dict(c, include_password=True) for c in cfg.children],
        "api_base": cfg.api_base,
        "api_subscription_key": cfg.api_subscription_key,
        "sync_cron": cfg.sync_cron,
        "auto_sync": cfg.auto_sync,
        "sync_lookback_days": cfg.sync_lookback_days,
        "sync_lookahead_days": cfg.sync_lookahead_days,
        "timezone": cfg.timezone,
        "request_timeout": cfg.request_timeout,
        # LLM (la clé API est écrite dans le fichier chiffré chmod 600).
        "use_llm_for_menus": cfg.use_llm_for_menus,
        "minimax_api_key": cfg.minimax_api_key,
        "minimax_base_url": cfg.minimax_base_url,
        "minimax_model": cfg.minimax_model,
        "minimax_timeout": cfg.minimax_timeout,
        "minimax_endpoint": cfg.minimax_endpoint,
        "llm_max_retries": cfg.llm_max_retries,
        "llm_retry_delay": cfg.llm_retry_delay,
        # MQTT (le mot de passe est écrit dans le fichier chmod 600).
        "use_mqtt": cfg.use_mqtt,
        "mqtt_host": cfg.mqtt_host,
        "mqtt_port": cfg.mqtt_port,
        "mqtt_username": cfg.mqtt_username,
        "mqtt_password": cfg.mqtt_password,
        "mqtt_base_topic": cfg.mqtt_base_topic,
        "mqtt_timeout": cfg.mqtt_timeout,
        "mqtt_qos": cfg.mqtt_qos,
        "mqtt_retain": cfg.mqtt_retain,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }

    fd, tmp_path = tempfile.mkstemp(
        prefix=".config.", suffix=".json.tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        logger.info("Config sauvegardée : %s", path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


# ------------------------------------------------------------------------
# Helpers UI
# ------------------------------------------------------------------------


def redacted_env(cfg: AppConfig) -> dict[str, str]:
    """Résumé non-sensible des variables de la config (pour la page Settings)."""
    return {
        "CANTINE_CONFIG_PATH": str(DEFAULT_CONFIG_PATH),
        "SOHAPPY_EMAIL": cfg.sohappy.email or "(non configuré)",
        "SOHAPPY_PASSWORD_SET": "oui" if cfg.sohappy.password else "non",
        "API_BASE": cfg.api_base,
        "API_SUBSCRIPTION_KEY": (
            cfg.api_subscription_key[:8] + "…"
            if len(cfg.api_subscription_key) > 8
            else cfg.api_subscription_key
        ),
        "SYNC_CRON": cfg.sync_cron,
        "AUTO_SYNC": "1" if cfg.auto_sync else "0",
        "TIMEZONE": cfg.timezone,
        "LOOKBACK_DAYS": str(cfg.sync_lookback_days),
        "LOOKAHEAD_DAYS": str(cfg.sync_lookahead_days),
        "ENABLED_CHILDREN": str(len(cfg.enabled_children())),
        "TOTAL_CHILDREN": str(len(cfg.children)),
        "USE_LLM": "1" if cfg.use_llm_for_menus else "0",
        "MINIMAX_API_KEY_SET": "oui" if cfg.minimax_api_key else "non",
        "MINIMAX_MODEL": cfg.minimax_model,
        "USE_MQTT": "1" if cfg.use_mqtt else "0",
        "MQTT_HOST": cfg.mqtt_host or "(non configuré)",
        "MQTT_PASSWORD_SET": "oui" if cfg.mqtt_password else "non",
        "MQTT_BASE_TOPIC": cfg.mqtt_base_topic,
    }
