"""Interface WebUI FastAPI : status / sync / config / enfants / menus."""

from __future__ import annotations

import base64
import hmac
import ipaddress
import logging
import logging.handlers
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import __version__
from .config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    load,
    redacted_env,
    save,
    to_dict,
)
from .models import Child
from .pipeline import (
    default_menus_dir,
    discover_children,
    list_cached_children,
    load_child_menu_cache,
    run_extraction,
)
from .sohappy import SoHappyClient, SoHappyError

logger = logging.getLogger("ha_menu.web")


_PKG_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PKG_DIR / "web" / "templates"
_STATIC_DIR = _PKG_DIR / "web" / "static"


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8237
LOG_FILE = Path("/data") / "ha_menu.log"


# ------------------------------------------------------------------------
# État runtime
# ------------------------------------------------------------------------


class _RuntimeState:
    """État mutable partagé par les endpoints et le scheduler interne."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config: AppConfig = load(config_path)
        self._lock = threading.Lock()
        self._sync_thread: threading.Thread | None = None
        self.last_sync: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.scheduler: Any | None = None

    def persist(self) -> None:
        save(self.config, self.config_path)

    def is_syncing(self) -> bool:
        return self._sync_thread is not None and self._sync_thread.is_alive()

    def start_sync(
        self,
        *,
        target_child_ids: list[int] | None = None,
        publish: bool = True,
        delete_orphans: bool = False,
        trigger: str = "manual",
    ) -> bool:
        with self._lock:
            if self.is_syncing():
                return False
            self.last_error = None
            self._sync_thread = threading.Thread(
                target=self._run_sync,
                args=(target_child_ids, publish, delete_orphans, trigger),
                daemon=True,
                name=f"ha_menu-{trigger}",
            )
            self._sync_thread.start()
            return True

    def _run_sync(
        self,
        target_child_ids: list[int] | None,
        publish: bool,
        delete_orphans: bool,
        trigger: str,
    ) -> None:
        try:
            stats = run_extraction(
                self.config,
                publish=publish,
                delete_orphans=delete_orphans,
                target_child_ids=target_child_ids,
            )
            self.last_sync = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "ok": stats.get("ok", False),
                "trigger": trigger,
                "children_ok": stats.get("children_ok", 0),
                "children_ko": stats.get("children_ko", 0),
                "errors": stats.get("errors", 0),
                "details": stats.get("children", []),
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Sync %s échoué", trigger)
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.last_sync = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "ok": False,
                "trigger": trigger,
                "error": self.last_error,
            }

    def reload_config(self) -> None:
        self.config = load(self.config_path)


# ------------------------------------------------------------------------
# Modèles Pydantic
# ------------------------------------------------------------------------


class StatusResponse(BaseModel):
    syncing: bool
    last_sync: dict[str, Any] | None
    last_error: str | None
    children_count: int
    enabled_count: int
    sohappy_configured: bool
    cron: str
    auto_sync: bool
    version: str


# ------------------------------------------------------------------------
# Application
# ------------------------------------------------------------------------


def create_app(config_path: Path = DEFAULT_CONFIG_PATH) -> FastAPI:
    """Construit l'application FastAPI."""
    state = _RuntimeState(config_path)

    @staticmethod  # placeholder for scheduler lifespan
    def _no_op() -> None:
        return None

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        """Démarre/arrête le scheduler APScheduler si auto_sync=True."""
        if not state.config.auto_sync:
            logger.info("Auto-sync désactivé (config.auto_sync=False)")
            yield
            return
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger

            scheduler = BackgroundScheduler(timezone="Europe/Paris")

            def _auto_tick() -> None:
                if state.is_syncing():
                    return
                state.start_sync(trigger="auto")

            scheduler.add_job(
                _auto_tick,
                CronTrigger.from_crontab(state.config.sync_cron),
            )
            scheduler.start()
            state.scheduler = scheduler
            logger.info(
                "Auto-sync actif (cron=%s TZ=Europe/Paris)",
                state.config.sync_cron,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Auto-sync scheduler non démarré : %s", exc)
        try:
            yield
        finally:
            sched = state.scheduler
            if sched is not None:
                sched.shutdown()
                state.scheduler = None

    app = FastAPI(
        title="HA - Menu — WebUI",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    # ---- Auth Basic (LAN whitelist, comme guichet-unique) ----
    basic_user = os.getenv("CANTINE_BASIC_USER", "")
    basic_pass = os.getenv("CANTINE_BASIC_PASS", "")
    if not (basic_user and basic_pass):
        basic_user = "admin"
        basic_pass = secrets.token_urlsafe(18)
        logger.warning(
            "CANTINE_BASIC_USER/PASS non définis — couple généré :\n"
            "    CANTINE_BASIC_USER=%s\n"
            "    CANTINE_BASIC_PASS=%s",
            basic_user, basic_pass,
        )

    lan_nets = [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("::1/128"),
    ]
    lan_host_suffixes = (".local", ".lan", ".home", ".internal", ".nas")

    def _real_client_ip(req: Request) -> str:
        xff = req.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        return (req.client.host if req.client else "") or ""

    def _is_lan_ip(ip_str: str) -> bool:
        try:
            ip = ipaddress.ip_address(ip_str)
        except (ValueError, TypeError):
            return False
        return any(ip in net for net in lan_nets)

    def _is_lan_host(req: Request) -> bool:
        host = req.headers.get("host", "").split(":")[0].lower()
        if not host:
            return False
        if _is_lan_ip(host):
            return True
        return any(host.endswith(sfx) for sfx in lan_host_suffixes)

    def _check_basic_auth(
        header_value: str, expected_user: str, expected_pass: str,
    ) -> bool:
        if not header_value.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header_value[6:]).decode("utf-8", errors="strict")
        except Exception:  # noqa: BLE001
            return False
        if ":" not in decoded:
            return False
        user, _, pwd = decoded.partition(":")
        try:
            return (
                hmac.compare_digest(user, expected_user)
                and hmac.compare_digest(pwd, expected_pass)
            )
        except TypeError:
            # Python ≥3.10 lève TypeError si l'une des chaînes contient des
            # caractères non-ASCII. On retombe sur une comparaison simple.
            return user == expected_user and pwd == expected_pass

    # Routes publiques (pas d'auth Basic même via le proxy NPM).
    # - /menus       : page famille (lecture seule, partagée)
    # - /static/*    : assets CSS/JS
    PUBLIC_PATHS = ("/menus", "/static/")
    _public_prefixes = tuple(p + "/" for p in PUBLIC_PATHS[:-1]) + (PUBLIC_PATHS[-1],)  # noqa: E501

    @app.middleware("http")
    async def _basic_auth_middleware(request: Request, call_next):
        path = request.url.path
        # Ingress HA : déjà authentifié par HA ; le proxy Supervisor a toujours cette IP.
        if request.client and request.client.host == "172.30.32.2":
            return await call_next(request)
        if _is_lan_ip(_real_client_ip(request)) or _is_lan_host(request):
            return await call_next(request)
        # Bypass auth pour les routes publiques (page famille, assets).
        if path in PUBLIC_PATHS or any(
            path.startswith(prefix) for prefix in _public_prefixes
        ):
            return await call_next(request)
        if not _check_basic_auth(
            request.headers.get("Authorization", ""), basic_user, basic_pass,
        ):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="HA - Menu"'},
                content="Authentification requise.\n",
                media_type="text/plain",
            )
        return await call_next(request)

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ----------------------------------------------------------------
    # Vues HTML
    # ----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        children = state.config.enabled_children()
        ctx = {
            "request": request,
            "version": __version__,
            "children": [
                {
                    "id": c.id,
                    "full_name": c.full_name,
                    "school": c.school,
                    "ha_calendar": c.ha_calendar,
                }
                for c in children
            ],
            "is_syncing": state.is_syncing(),
            "last_sync": state.last_sync,
            "last_error": state.last_error,
            "cron": state.config.sync_cron,
            "auto_sync": state.config.auto_sync,
            "sohappy_configured": state.config.sohappy.is_set(),
        }
        return templates.TemplateResponse("index.html", ctx)

    @app.get("/menus", response_class=HTMLResponse)
    def menus_view(
        request: Request,
        family: int | None = None,
        days: int = 7,
    ) -> HTMLResponse:
        """Page famille — menus de la semaine (et suivants) par enfant.

        Paramètres query string :
            ``?family=<child_id>`` : affiche le menu de cet enfant. Sans
                paramètre : dropdown, défaut = premier enfant actif.
            ``?days=<N>``         : fenêtre d'affichage (défaut 7, max 30).

        Route **publique** (bypass auth Basic + LAN bypass) — la famille
        peut la bookmarker derrière le proxy NPM sans mot de passe.
        """
        from datetime import date as _date, timedelta
        menus_dir = default_menus_dir(state.config)
        cached_ids = list_cached_children(menus_dir)
        # Construit la liste des enfants avec menus cachés + métadonnées.
        entries: list[dict[str, Any]] = []
        for cid in cached_ids:
            data = load_child_menu_cache(cid, menus_dir)
            if data is None:
                continue
            entries.append({
                "child_id": data.get("child_id"),
                "child_name": data.get("child_name"),
                "extracted_at": data.get("extracted_at"),
                "days": data.get("days", []),
            })
        # Tri : enfants connus de la config d'abord, puis le reste.
        known_ids = {c.id for c in state.config.enabled_children()}
        entries.sort(key=lambda e: (
            0 if e["child_id"] in known_ids else 1,
            e["child_id"],
        ))

        # Si pas d'enfant sélectionné ou enfant invalide → prendre le 1er.
        selected: dict[str, Any] | None = None
        if family is not None:
            for e in entries:
                if e["child_id"] == family:
                    selected = e
                    break
        if selected is None and entries:
            selected = entries[0]

        # Fenêtre d'affichage.
        days = max(1, min(days, 30))
        today = _date.today()
        start = today
        end = today + timedelta(days=days - 1)

        if selected is not None:
            # Filtre les jours dans [start, end] + calcule weekday FR.
            jours_fr = [
                "lundi", "mardi", "mercredi", "jeudi",
                "vendredi", "samedi", "dimanche",
            ]
            mois_fr = [
                "", "janvier", "février", "mars", "avril", "mai", "juin",
                "juillet", "août", "septembre", "octobre",
                "novembre", "décembre",
            ]
            visible: list[dict[str, Any]] = []
            for d in selected["days"]:
                try:
                    jour = _date.fromisoformat(d["date"])
                except (KeyError, TypeError, ValueError):
                    continue
                if jour < start or jour > end:
                    continue
                weekday = jours_fr[jour.weekday()]
                visible.append({
                    "date": d["date"],
                    "weekday": weekday,
                    "pretty_date": f"{weekday} {jour.day} {mois_fr[jour.month]} {jour.year}",
                    "summary": d.get("summary") or "Menu indisponible",
                    "llm_source": d.get("llm_source"),
                    "is_today": jour == today,
                    "is_weekend": jour.weekday() >= 5,
                })
            visible.sort(key=lambda v: v["date"])
            selected = {**selected, "visible_days": visible}
        else:
            selected = {"child_name": "—", "visible_days": [], "child_id": None}

        ctx = {
            "request": request,
            "version": __version__,
            "children": entries,
            "selected": selected,
            "days_param": days,
            "days_options": [3, 7, 14, 21],
            "today": today.isoformat(),
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
        }
        return templates.TemplateResponse("menus.html", ctx)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_view(request: Request, tab: str = "credentials") -> HTMLResponse:
        if tab not in ("credentials", "children", "llm", "mqtt", "configuration"):
            tab = "credentials"
        ctx = {
            "request": request,
            "tab": tab,
            "version": __version__,
            "sohappy": {
                "email": state.config.sohappy.email,
                "password_set": bool(state.config.sohappy.password),
            },
            "children": [c.to_dict() for c in state.config.children],
            "llm": {
                "enabled": state.config.use_llm_for_menus,
                "api_key_set": bool(state.config.minimax_api_key),
                "base_url": state.config.minimax_base_url,
                "model": state.config.minimax_model,
                "endpoint": state.config.minimax_endpoint,
            },
            "mqtt": {
                "enabled": state.config.use_mqtt,
                "host": state.config.mqtt_host,
                "port": state.config.mqtt_port,
                "username": state.config.mqtt_username,
                "password_set": bool(state.config.mqtt_password),
                "base_topic": state.config.mqtt_base_topic,
                "qos": state.config.mqtt_qos,
                "retain": state.config.mqtt_retain,
            },
            "env_redacted": redacted_env(state.config),
            "is_syncing": state.is_syncing(),
            "last_sync": state.last_sync,
            "last_error": state.last_error,
        }
        return templates.TemplateResponse("settings.html", ctx)

    @app.get("/api/status", response_model=StatusResponse)
    def api_status() -> StatusResponse:
        return StatusResponse(
            syncing=state.is_syncing(),
            last_sync=state.last_sync,
            last_error=state.last_error,
            children_count=len(state.config.children),
            enabled_count=len(state.config.enabled_children()),
            sohappy_configured=state.config.sohappy.is_set(),
            cron=state.config.sync_cron,
            auto_sync=state.config.auto_sync,
            version=__version__,
        )

    @app.get("/api/config")
    def api_config() -> JSONResponse:
        return JSONResponse(to_dict(state.config, include_passwords=False))

    # ----------------------------------------------------------------
    # Identifiants So-Happy
    # ----------------------------------------------------------------

    async def _parse_credentials_body(request: Request) -> tuple[str, str]:
        """Lit un body JSON ou form-encoded ``{email, password}`` et le
        retourne. Tolérant : body vide / invalide → ("", "").
        """
        import json as _json
        raw = await request.body()
        if not raw:
            return "", ""
        ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        try:
            if ctype == "application/json":
                data = _json.loads(raw.decode("utf-8"))
            elif ctype == "application/x-www-form-urlencoded":
                from urllib.parse import parse_qs
                data = {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}
            else:
                return "", ""
        except Exception:  # noqa: BLE001
            return "", ""
        if not isinstance(data, dict):
            return "", ""
        return (data.get("email") or "").strip(), data.get("password") or ""

    @app.post("/api/sohappy")
    async def api_sohappy(request: Request) -> JSONResponse:
        email, password = await _parse_credentials_body(request)
        if not email:
            return JSONResponse({"error": "email requis"}, status_code=400)
        state.config.sohappy.email = email
        if password:
            state.config.sohappy.password = password
        state.persist()
        return JSONResponse({"ok": True})

    @app.post("/api/sohappy/test")
    async def api_sohappy_test(request: Request) -> JSONResponse:
        """Teste le login sans rien modifier.

        Accepte un body JSON ou form-encoded ``{email, password}``. Si l'un
        des champs est fourni, il surcharge la config sauvegardée pour le
        test uniquement (rien n'est persisté).
        """
        import copy

        body_email, body_password = await _parse_credentials_body(request)

        # Copie la config pour ne rien modifier
        test_cfg = copy.copy(state.config)
        override_email = False
        override_password = False
        if body_email:
            test_cfg.sohappy.email = body_email
            override_email = True
        if body_password:
            test_cfg.sohappy.password = body_password
            override_password = True

        # Validation : on doit disposer des credentials complets pour tester
        if not test_cfg.sohappy.is_set():
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "Saisissez l'email et le mot de passe avant de tester."
                        if not override_email and not override_password
                        else "Email ou mot de passe manquant."
                    ),
                },
                status_code=400,
            )
        try:
            with SoHappyClient(test_cfg) as client:
                token = client.login()
                return JSONResponse({
                    "ok": True,
                    "user": test_cfg.sohappy.email,
                    "expires_at": (
                        token.expires_at.isoformat() if token.expires_at else None
                    ),
                })
        except SoHappyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    # ----------------------------------------------------------------
    # Découverte / enfants
    # ----------------------------------------------------------------

    @app.post("/api/discover")
    def api_discover() -> JSONResponse:
        try:
            children = discover_children(state.config)
        except SoHappyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        state.persist()
        return JSONResponse({
            "ok": True,
            "children": [c.to_dict() for c in children],
        })

    @app.post("/api/children")
    async def api_children_upsert(request: Request) -> JSONResponse:
        import json as _json
        try:
            data = _json.loads((await request.body()).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "JSON invalide"}, status_code=400)
        try:
            child_id = int(data["id"])
        except (KeyError, ValueError, TypeError):
            return JSONResponse({"error": "id requis"}, status_code=400)

        existing = state.config.find_child(child_id)
        if existing is None:
            child = Child(id=child_id)
            state.config.children.append(child)
        else:
            child = existing
        child.first_name = (data.get("first_name") or child.first_name)
        child.last_name = (data.get("last_name") or child.last_name)
        child.school = (data.get("school") or child.school)
        child.enabled = bool(data.get("enabled", True))
        ha_calendar = (data.get("ha_calendar") or "").strip()
        if ha_calendar and not re.fullmatch(r"calendar\.[a-z0-9_]+", ha_calendar):
            return JSONResponse(
                {"error": "Agenda HA invalide (attendu : calendar.xxx)"}, status_code=400,
            )
        child.ha_calendar = ha_calendar
        state.persist()
        return JSONResponse({"ok": True, "child": child.to_dict()})

    @app.post("/api/children/{child_id}/ha/test")
    async def api_children_ha_test(child_id: int) -> JSONResponse:
        """Lit l'agenda HA d'un enfant et liste les événements HA - Menu présents."""
        child = state.config.find_child(child_id)
        if child is None:
            return JSONResponse({"error": "Enfant inconnu"}, status_code=404)
        if not child.ha_calendar:
            return JSONResponse(
                {"ok": False, "error": "Agenda HA non configuré."}, status_code=400,
            )
        from .ha_calendar import list_ha_events
        try:
            events = list_ha_events(child)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "error": f"Agenda HA : {exc}"}, status_code=400,
            )
        return JSONResponse({
            "ok": True,
            "calendar": child.ha_calendar,
            "events_count": len(events),
            "events": [
                {"uid": e.get("uid"), "summary": e.get("summary"), "date": e["start"].get("date")}
                for e in events[:20]
            ],
        })

    @app.post("/api/children/{child_id}/refresh")
    async def api_children_refresh(child_id: int) -> JSONResponse:
        """Re-interroge So-Happy verbose=true pour rafraîchir school + menus."""
        child = state.config.find_child(child_id)
        if child is None:
            return JSONResponse({"error": "Enfant inconnu"}, status_code=404)
        try:
            with SoHappyClient(state.config) as client:
                client.login()
                fresh = client.fetch_child_details(child_id)
        except SoHappyError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=400,
            )
        # Met à jour school + menus (préserve l'agenda HA / enabled)
        child.first_name = fresh.first_name or child.first_name
        child.last_name = fresh.last_name or child.last_name
        child.school = fresh.school or child.school
        child.birth_date = fresh.birth_date or child.birth_date
        child.clients = fresh.clients
        child.sohappy_menus = fresh.sohappy_menus
        state.persist()
        return JSONResponse({
            "ok": True,
            "child": child.to_dict(),
            "menus_found": len(fresh.sohappy_menus),
        })

    @app.delete("/api/children/{child_id}")
    def api_children_delete(child_id: int) -> JSONResponse:
        before = len(state.config.children)
        state.config.children = [
            c for c in state.config.children if c.id != child_id
        ]
        if len(state.config.children) == before:
            return JSONResponse({"error": "introuvable"}, status_code=404)
        state.persist()
        return JSONResponse({"ok": True})

    # ----------------------------------------------------------------
    # Sync
    # ----------------------------------------------------------------

    @app.post("/api/sync")
    async def api_sync(request: Request) -> JSONResponse:
        import json as _json
        try:
            data = _json.loads((await request.body()).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            data = {}
        publish = bool(data.get("publish", True))
        delete_orphans = bool(data.get("delete_orphans", False))
        child_ids = data.get("child_ids")
        if not isinstance(child_ids, list):
            child_ids = None
        else:
            try:
                child_ids = [int(x) for x in child_ids]
            except (TypeError, ValueError):
                child_ids = None
        started = state.start_sync(
            target_child_ids=child_ids,
            publish=publish,
            delete_orphans=delete_orphans,
            trigger="api",
        )
        if not started:
            return JSONResponse(
                {"error": "Un sync est déjà en cours"}, status_code=409,
            )
        return JSONResponse({"started": True})

    # ----------------------------------------------------------------
    # Menus (consultation cache local post-sync)
    # ----------------------------------------------------------------
    # Source : /data/menus/{child_id}.json écrit par run_extraction.
    # Format : voir pipeline.save_child_menu_cache.
    # - GET /api/menus                   → enfants cachés (id + nom + extracted_at)
    # - GET /api/menus/{child_id}        → menus d'un enfant (filtre from/to)
    # - GET /api/menus/{child_id}/today  → menu du jour (404 si pas de cache)

    def _menus_dir() -> Path:
        return default_menus_dir(state.config)

    @app.get("/api/menus")
    async def api_menus_index() -> JSONResponse:
        """Liste les enfants qui ont un cache de menus."""
        menus_dir = _menus_dir()
        cached = list_cached_children(menus_dir)
        out: list[dict[str, Any]] = []
        for cid in cached:
            data = load_child_menu_cache(cid, menus_dir)
            if data is None:
                continue
            out.append({
                "child_id": data.get("child_id"),
                "child_name": data.get("child_name"),
                "extracted_at": data.get("extracted_at"),
                "days_count": len(data.get("days", [])),
            })
        return JSONResponse({"menus_dir": str(menus_dir), "children": out})

    @app.get("/api/menus/{child_id}")
    async def api_menus_child(
        request: Request,
        child_id: int,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> JSONResponse:
        """Menus d'un enfant sur une fenêtre optionnelle ``?from``/``?to`` (YYYY-MM-DD).

        ``from`` est un mot-clé Python non utilisable comme nom de paramètre,
        on accepte donc ``?from=...`` via alias en relisant la query-string brute.
        """
        # Si l'appelant a utilisé ``?from=...`` (recommandé), FastAPI l'ignore car
        # ce n'est pas le nom du paramètre. On le récupère depuis l'URL brute.
        if date_from is None:
            raw_from = request.query_params.get("from")
            if raw_from is not None:
                date_from = raw_from

        data = load_child_menu_cache(child_id, _menus_dir())
        if data is None:
            return JSONResponse(
                {"error": f"Pas de cache pour l'enfant {child_id}. Lancez un sync."},
                status_code=404,
            )
        days = data.get("days", [])
        if date_from:
            days = [d for d in days if d.get("date", "") >= date_from]
        if date_to:
            days = [d for d in days if d.get("date", "") <= date_to]
        return JSONResponse({
            "child_id": data.get("child_id"),
            "child_name": data.get("child_name"),
            "extracted_at": data.get("extracted_at"),
            "source_url": data.get("source_url"),
            "filter": {"from": date_from, "to": date_to},
            "days": days,
        })

    @app.get("/api/menus/{child_id}/today")
    async def api_menus_today(child_id: int) -> JSONResponse:
        """Menu du jour pour un enfant (cache). 404 si absent."""
        from datetime import date as _date

        data = load_child_menu_cache(child_id, _menus_dir())
        if data is None:
            return JSONResponse(
                {"error": f"Pas de cache pour l'enfant {child_id}."},
                status_code=404,
            )
        today = _date.today().isoformat()
        for d in data.get("days", []):
            if d.get("date") == today:
                return JSONResponse({
                    "child_id": data.get("child_id"),
                    "child_name": data.get("child_name"),
                    "extracted_at": data.get("extracted_at"),
                    "day": d,
                })
        return JSONResponse(
            {"error": f"Pas de menu cached pour aujourd'hui ({today})."},
            status_code=404,
        )

    @app.get("/api/menus/{child_id}/{day}")
    async def api_menus_day(child_id: int, day: str) -> JSONResponse:
        """Menu d'un enfant pour une date précise (YYYY-MM-DD). 404 si absent."""
        data = load_child_menu_cache(child_id, _menus_dir())
        if data is None:
            return JSONResponse(
                {"error": f"Pas de cache pour l'enfant {child_id}."},
                status_code=404,
            )
        for d in data.get("days", []):
            if d.get("date") == day:
                return JSONResponse({
                    "child_id": data.get("child_id"),
                    "child_name": data.get("child_name"),
                    "extracted_at": data.get("extracted_at"),
                    "day": d,
                })
        return JSONResponse(
            {"error": f"Pas de menu cached pour {day}."},
            status_code=404,
        )

    # ----------------------------------------------------------------
    # LLM (reformatage des menus via MiniMax API compatible OpenAI)
    # ----------------------------------------------------------------

    async def _parse_llm_body(request: Request) -> dict[str, Any]:
        """Lit le body JSON ou form-encoded pour les endpoints LLM."""
        import json as _json
        raw = await request.body()
        if not raw:
            return {}
        ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        try:
            if ctype == "application/json":
                return _json.loads(raw.decode("utf-8"))
            if ctype == "application/x-www-form-urlencoded":
                from urllib.parse import parse_qs
                return {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}
        except Exception:  # noqa: BLE001
            return {}
        return {}

    @app.post("/api/llm")
    async def api_llm_update(request: Request) -> JSONResponse:
        """Met à jour la config LLM (clé API, modèle, base URL, activation).

        Champs acceptés (tous optionnels) :
          - use_llm_for_menus (bool)
          - minimax_api_key (str ; vide = ne change pas, '__CLEAR__' = efface)
          - minimax_base_url (str)
          - minimax_model (str)
        La clé n'est jamais renvoyée en clair dans la réponse.
        """
        body = await _parse_llm_body(request)
        if "use_llm_for_menus" in body:
            state.config.use_llm_for_menus = bool(body["use_llm_for_menus"])
        if "minimax_base_url" in body and body["minimax_base_url"]:
            state.config.minimax_base_url = str(body["minimax_base_url"]).strip()
        if "minimax_model" in body and body["minimax_model"]:
            state.config.minimax_model = str(body["minimax_model"]).strip()
        if "minimax_endpoint" in body and body["minimax_endpoint"]:
            ep = str(body["minimax_endpoint"]).strip().lower()
            if ep in ("anthropic", "openai"):
                state.config.minimax_endpoint = ep
        if "minimax_api_key" in body:
            key = str(body["minimax_api_key"] or "")
            if key == "__CLEAR__":
                state.config.minimax_api_key = ""
            elif key:  # champ vide = laisser intact
                state.config.minimax_api_key = key
        state.persist()
        return JSONResponse({
            "ok": True,
            "use_llm_for_menus": state.config.use_llm_for_menus,
            "minimax_api_key_set": bool(state.config.minimax_api_key),
            "minimax_base_url": state.config.minimax_base_url,
            "minimax_model": state.config.minimax_model,
            "minimax_endpoint": state.config.minimax_endpoint,
        })

    @app.post("/api/llm/test")
    async def api_llm_test(request: Request) -> JSONResponse:
        """Teste la connexion à l'API LLM (1 appel léger)."""
        from .llm import LLMClient

        # Accepte un override ponctuel via le body (utile depuis l'UI quand
        # l'utilisateur saisit une nouvelle clé sans avoir rechargé la page).
        body = await _parse_llm_body(request)
        test_cfg = state.config
        if body.get("minimax_api_key"):
            # Copie pour ne pas polluer l'état tant qu'on n'a pas enregistré.
            import copy as _copy
            test_cfg = _copy.copy(state.config)
            test_cfg.minimax_api_key = str(body["minimax_api_key"])
        if body.get("minimax_base_url"):
            import copy as _copy
            test_cfg = _copy.copy(state.config) if test_cfg is state.config else test_cfg
            test_cfg.minimax_base_url = str(body["minimax_base_url"])
        if body.get("minimax_model"):
            import copy as _copy
            test_cfg = _copy.copy(state.config) if test_cfg is state.config else test_cfg
            test_cfg.minimax_model = str(body["minimax_model"])
        if body.get("minimax_endpoint") in ("anthropic", "openai"):
            import copy as _copy
            test_cfg = _copy.copy(state.config) if test_cfg is state.config else test_cfg
            test_cfg.minimax_endpoint = str(body["minimax_endpoint"])

        if not test_cfg.minimax_api_key:
            return JSONResponse(
                {"ok": False, "error": "Clé API non configurée."},
                status_code=400,
            )
        with LLMClient(test_cfg) as client:
            result = client.test_connection()
        status = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=status)

    # ----------------------------------------------------------------
    # MQTT (Home Assistant via EMQX)
    # ----------------------------------------------------------------

    @app.post("/api/mqtt")
    async def api_mqtt_update(request: Request) -> JSONResponse:
        """Met à jour la config MQTT (host, port, user, pwd, activation)."""
        body = await _parse_llm_body(request)
        if "use_mqtt" in body:
            state.config.use_mqtt = bool(body["use_mqtt"])
        if "mqtt_host" in body:
            state.config.mqtt_host = str(body["mqtt_host"]).strip()
        if "mqtt_port" in body:
            try:
                state.config.mqtt_port = int(body["mqtt_port"])
            except (TypeError, ValueError):
                pass
        if "mqtt_username" in body:
            state.config.mqtt_username = str(body["mqtt_username"])
        if "mqtt_password" in body:
            pwd = str(body["mqtt_password"] or "")
            if pwd == "__CLEAR__":
                state.config.mqtt_password = ""
            elif pwd:
                state.config.mqtt_password = pwd
        if "mqtt_base_topic" in body and body["mqtt_base_topic"]:
            state.config.mqtt_base_topic = str(body["mqtt_base_topic"]).strip()
        if "mqtt_qos" in body:
            try:
                state.config.mqtt_qos = max(0, min(int(body["mqtt_qos"]), 2))
            except (TypeError, ValueError):
                pass
        if "mqtt_retain" in body:
            state.config.mqtt_retain = bool(body["mqtt_retain"])
        state.persist()
        return JSONResponse({
            "ok": True,
            "use_mqtt": state.config.use_mqtt,
            "mqtt_host": state.config.mqtt_host,
            "mqtt_port": state.config.mqtt_port,
            "mqtt_username": state.config.mqtt_username,
            "mqtt_base_topic": state.config.mqtt_base_topic,
            "mqtt_qos": state.config.mqtt_qos,
            "mqtt_retain": state.config.mqtt_retain,
            "mqtt_password_set": bool(state.config.mqtt_password),
        })

    @app.post("/api/mqtt/test")
    async def api_mqtt_test(request: Request) -> JSONResponse:
        """Teste la connexion MQTT (ping publish)."""
        import copy as _copy
        body = await _parse_llm_body(request)
        test_cfg = _copy.copy(state.config)
        if body.get("mqtt_host"):
            test_cfg.mqtt_host = str(body["mqtt_host"])
        if body.get("mqtt_port"):
            try:
                test_cfg.mqtt_port = int(body["mqtt_port"])
            except (TypeError, ValueError):
                pass
        if body.get("mqtt_username") is not None:
            test_cfg.mqtt_username = str(body["mqtt_username"])
        if body.get("mqtt_password"):
            test_cfg.mqtt_password = str(body["mqtt_password"])
        if not test_cfg.mqtt_host:
            return JSONResponse(
                {"ok": False, "error": "Host broker non configuré."},
                status_code=400,
            )
        from .mqtt_publisher import MQTTPublisher

        # Connexion seule, sans enfant : publier un faux enfant créerait un appareil
        # « Test » permanent dans HA (discovery retenue).
        try:
            pub = MQTTPublisher(test_cfg)
            stats = pub.publish([], {})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "error": f"Connexion broker : {exc}"},
                status_code=400,
            )
        return JSONResponse({"ok": True, **(stats or {})})

    # ----------------------------------------------------------------
    # Configuration (cron, lookback, lookahead, auto_sync)
    # ----------------------------------------------------------------

    @app.post("/api/config")
    async def api_config_update(request: Request) -> JSONResponse:
        import json as _json
        try:
            body = _json.loads((await request.body()).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "JSON invalide"}, status_code=400)
        if "sync_cron" in body:
            state.config.sync_cron = str(body["sync_cron"])
        if "auto_sync" in body:
            state.config.auto_sync = bool(body["auto_sync"])
        if "sync_lookback_days" in body:
            state.config.sync_lookback_days = int(body["sync_lookback_days"])
        if "sync_lookahead_days" in body:
            state.config.sync_lookahead_days = int(body["sync_lookahead_days"])
        if "api_base" in body:
            state.config.api_base = str(body["api_base"])
        if "api_subscription_key" in body:
            state.config.api_subscription_key = str(body["api_subscription_key"])
        state.persist()
        # Recharger scheduler si actif (le lifespan ne se relance pas à chaud,
        # il faudra un restart container pour prendre effet).
        if state.scheduler is not None:
            state.scheduler.shutdown()
            state.scheduler = None
        return JSONResponse({"ok": True})

    @app.get("/partials/status", response_class=HTMLResponse)
    def partials_status(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            "_status.html",
            {
                "request": request,
                "is_syncing": state.is_syncing(),
                "last_sync": state.last_sync,
                "last_error": state.last_error,
            },
        )

    # ----------------------------------------------------------------
    # Logs (debug LLM / sync)
    # ----------------------------------------------------------------

    def _read_logs(lines: int) -> list[str]:
        """Lit les N dernières lignes du log applicatif (file handler).

        Plus de lecture via ``/proc/1/fd/*`` (bloque — stdout Docker
        redirigé). On utilise exclusivement ``LOG_FILE`` écrit via le
        ``RotatingFileHandler`` installé dans ``run_web``.
        """
        if not LOG_FILE.exists():
            return ["(logs non encore écrits — premiers démarrages en cours)"]
        try:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as fh:
                return fh.read().splitlines()[-lines:]
        except OSError as exc:  # noqa: BLE001
            return [f"(logs indisponibles : {exc})"]

    @app.get("/api/logs")
    def api_logs(lines: int = 200) -> JSONResponse:
        """Renvoie les N dernières lignes de logs (défaut 200, max 2000)."""
        lines = max(1, min(lines, 2000))
        return JSONResponse({"lines": _read_logs(lines), "version": __version__})

    @app.get("/logs", response_class=HTMLResponse)
    def logs_view(request: Request, lines: int = 200) -> HTMLResponse:
        lines = max(20, min(lines, 2000))
        return templates.TemplateResponse(
            "logs.html",
            {
                "request": request,
                "version": __version__,
                "lines": _read_logs(lines),
                "lines_requested": lines,
            },
        )

    @app.get("/partials/logs", response_class=HTMLResponse)
    def partials_logs(request: Request, lines: int = 200) -> HTMLResponse:
        lines = max(20, min(lines, 2000))
        return templates.TemplateResponse(
            "_logs.html",
            {"request": request, "lines": _read_logs(lines)},
        )

    return app


# ------------------------------------------------------------------------
# Entrée CLI
# ------------------------------------------------------------------------


def run_web(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    config_path: Path | None = None,
) -> None:
    """Lance uvicorn. Bloquant."""
    import uvicorn

    cp = config_path or DEFAULT_CONFIG_PATH
    app = create_app(cp)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # FileHandler pour la page /logs et l'endpoint /api/logs.
    # On n'utilise PAS basicConfig ici : uvicorn configure déjà le
    # root logger. On attache plutôt notre handler au root logger (et à
    # uvicorn.access) SANS toucher aux autres handlers existants.
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=2 * 1024 * 1024,  # 2 MiB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Évite les doublons si on redémarre run_web dans le même process.
    if not any(
        isinstance(h, logging.handlers.RotatingFileHandler)
        and getattr(h, "baseFilename", None) == str(LOG_FILE)
        for h in root.handlers
    ):
        root.addHandler(file_handler)
    # uvicorn.access a son propre logger enfant → on lui attache aussi.
    logging.getLogger("uvicorn.access").addHandler(file_handler)
    logging.getLogger("uvicorn.error").addHandler(file_handler)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=True)
