"""Client HTTP pour l'API So-Happy (`apim-production.so-happy.fr/api-app/`).

L'app web So-Happy (Angular) consomme directement cette API :

- Login :  POST /tokens                      body : {mail, password}
- Children : GET  /users/childrens?only_with_clients=true&add_validity_key=false
- Child detail (verbose) : GET /users/childrens/{cid}?verbose=true (clients + menus)
- Menus :   GET  /users/childrens/{cid}/menus/{mid}/restaurant-menus
                  ?category_code=&begin_date=YYYY-MM-DD&end_date=YYYY-MM-DD
                  (le GraphQL transforme en :
        clients/{cid}/zones-restauration/{zrId}/restaurant-menus/)

Authentification : ``Authorization: Bearer <token>`` + ``Ocp-Apim-Subscription-Key``.

⚠️ Le WAF / load-balancer de So-Happy semble rejeter les requêtes « anonymes »
(curl, serveur-à-serveur) avec un bug SQL trompeur (HTTP 500 « Invalid column name
WD_ORDER_ »). Les requêtes venant d'un navigateur authentifié passent (HTTP 200).
Pour contourner, on privilégie la capture depuis la session navigateur de
l'utilisateur (voir `playwright_capture.py` plus tard si nécessaire).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx

from .config import AppConfig
from .models import Child, MenuDay, MenuItem, classify_category, detect_label_flags

logger = logging.getLogger(__name__)


class SoHappyError(RuntimeError):
    """Erreur de l'API So-Happy."""


@dataclass(slots=True)
class TokenInfo:
    """Token d'authentification + date d'expiration approximative."""

    value: str
    expires_at: date | None = None


class SoHappyClient:
    """Client fin pour l'API So-Happy. Une instance par sync."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.base_url = config.api_base.rstrip("/")
        self._token: TokenInfo | None = None
        # Clé Azure API Management (extraite du bundle JS public so-happy.fr).
        self._apim_key = config.api_subscription_key
        # Headers requis pour passer le WAF/APIM :
        # - app_version : version du SPA embarqué dans le bundle JS (sinon SQL 500
        #   trompeur sur certains endpoints, dont /restaurant-menus).
        # - locale : fr_FR par défaut.
        # - verbose : true → demande la forme complète des réponses.
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "User-Agent": config.user_agent,
                "Ocp-Apim-Subscription-Key": self._apim_key,
                "app_version": "6.58.5",
                "locale": "fr_FR",
                "verbose": "true",
                "Accept": "application/json",
            },
            timeout=config.request_timeout,
        )

    def __enter__(self) -> SoHappyClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self) -> TokenInfo:
        """POST /tokens avec {mail, password} → renvoie TokenInfo."""
        email = self.config.sohappy.email
        password = self.config.sohappy.password
        if not email or not password:
            raise SoHappyError(
                "Identifiants So-Happy manquants. Configurez-les dans "
                "l'interface web (Settings → Identifiants So-Happy).",
            )

        logger.info("Login So-Happy pour %s…", email)
        try:
            resp = self._client.post(
                "/tokens",
                json={"mail": email, "password": password},
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise SoHappyError(f"Erreur réseau au login : {exc}") from exc

        if resp.status_code != 200:
            raise SoHappyError(
                f"Login échoué ({resp.status_code}) : {resp.text[:200]}",
            )
        payload = resp.json()
        token = payload.get("token") or {}
        value = token.get("value") if isinstance(token, dict) else None
        if not value:
            raise SoHappyError("Login OK mais aucun token reçu.")
        expires_at = None
        exp = token.get("expiration_date") if isinstance(token, dict) else None
        if exp:
            try:
                expires_at = date.fromisoformat(exp[:10])
            except (ValueError, TypeError):
                expires_at = None
        self._token = TokenInfo(value=value, expires_at=expires_at)
        logger.info("Login OK (expiration=%s).", expires_at or "?")
        return self._token

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise SoHappyError("Appel non authentifié — appeler login() d'abord.")
        return {"Authorization": f"Bearer {self._token.value}"}

    # ------------------------------------------------------------------
    # Enfants liés au compte parent
    # ------------------------------------------------------------------

    def list_children(self) -> list[Child]:
        """GET /users/childrens?only_with_clients=true&add_validity_key=false

        Renvoie une liste plate ``[{id, first_name, ...}, ...]`` ; pour
        obtenir les `clients` et `menus` attachés, on requête chaque enfant
        individuellement via ``/users/childrens/{cid}?verbose=true``. C'est
        la même structure utilisée par le SPA Angular.
        """
        logger.info("Récupération de la liste des enfants…")
        try:
            resp = self._client.get(
                "/users/childrens",
                params={"only_with_clients": "true", "add_validity_key": "false"},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise SoHappyError(f"Erreur réseau listage enfants : {exc}") from exc
        if resp.status_code == 401:
            raise SoHappyError("Session expirée — retentez le login.")
        if resp.status_code != 200:
            raise SoHappyError(
                f"Liste enfants indisponible ({resp.status_code}) : {resp.text[:200]}",
            )

        data = resp.json()
        items: list[Any]
        if isinstance(data, dict):
            items = (
                data.get("items")
                or data.get("data")
                or data.get("users")
                or data.get("childrens")
                or []
            )
        elif isinstance(data, list):
            items = data
        else:
            items = []

        children: list[Child] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            cid = raw.get("id") or raw.get("children_id")
            if cid is None:
                continue
            children.append(self._fetch_child_details(int(cid)))

        logger.info("→ %d enfant(s) trouvé(s) sur le compte.", len(children))
        return children

    def fetch_child_details(self, child_id: int) -> Child:
        """Détail d'un enfant (verbose=true) : school, clients, menus."""
        return self._fetch_child_details(child_id)

    def _fetch_child_details(self, child_id: int) -> Child:
        """GET /users/childrens/{cid}?verbose=true — récupère school, clients, menus."""
        try:
            resp = self._client.get(
                f"/users/childrens/{child_id}",
                params={"verbose": "true"},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise SoHappyError(
                f"Erreur réseau détail enfant {child_id} : {exc}",
            ) from exc
        if resp.status_code != 200:
            raise SoHappyError(
                f"Détail enfant {child_id} indisponible ({resp.status_code})",
            )
        raw = resp.json()
        if not isinstance(raw, dict):
            raise SoHappyError(f"Format inattendu pour enfant {child_id}")
        cid = int(raw.get("id") or child_id)
        bd_raw = raw.get("birth_date") or raw.get("date_of_birth")
        try:
            bd = date.fromisoformat(bd_raw[:10]) if bd_raw else None
        except (ValueError, TypeError):
            bd = None
        clients = raw.get("clients") or []
        menus = raw.get("menus") or []
        school = ""
        if isinstance(clients, list) and clients:
            first = clients[0]
            if isinstance(first, dict):
                school_obj = first.get("name") or ""
                if isinstance(school_obj, str):
                    school = school_obj
        return Child(
            id=cid,
            first_name=raw.get("first_name", "") or raw.get("firstname", ""),
            last_name=raw.get("last_name", "") or raw.get("lastname", ""),
            school=school,
            birth_date=bd,
            clients=clients if isinstance(clients, list) else [],
            sohappy_menus=menus if isinstance(menus, list) else [],
        )

    # ------------------------------------------------------------------
    # Menus
    # ------------------------------------------------------------------

    def fetch_menus(
        self,
        child: Child,
        *,
        begin: date | None = None,
        end: date | None = None,
    ) -> list[MenuDay]:
        """Récupère les menus d'un enfant pour une fenêtre de dates.

        Pour chaque menu So-Happy attaché à l'enfant (découvert via
        ``/users/childrens/{cid}?verbose=true``), on appelle
        ``/clients/{client_id}/menus/{menu_id}/restaurant-menus``.
        """
        if begin is None:
            begin = date.today() - timedelta(days=self.config.sync_lookback_days)
        if end is None:
            end = date.today() + timedelta(days=self.config.sync_lookahead_days)

        menus = child.sohappy_menus or []
        if not menus:
            # Fallback : extraire des `clients` (ancien format)
            for client in child.clients or []:
                if isinstance(client, dict):
                    client_menus = client.get("menus") or []
                    if isinstance(client_menus, list):
                        menus.extend(client_menus)
        if not menus:
            logger.warning(
                "Enfant %s : aucun menu So-Happy associé (refresh depuis l'UI ?).",
                child.full_name,
            )
            return []

        all_days: list[MenuDay] = []
        for menu in menus:
            if not isinstance(menu, dict):
                continue
            menu_id = menu.get("id") or menu.get("menu_id")
            if not menu_id:
                continue
            # Le client_id peut être dans menu.client.id ou menu.client_id
            client_id = (
                (menu.get("client") or {}).get("id")
                if isinstance(menu.get("client"), dict)
                else None
            ) or menu.get("client_id")
            if not client_id:
                logger.warning(
                    "Menu %s : pas de client_id connu.", menu_id,
                )
                continue
            label = menu.get("name") or menu.get("label") or ""
            try:
                days = self._fetch_menus_for(
                    client_id=int(client_id),
                    menu_id=int(menu_id),
                    begin=begin,
                    end=end,
                    menu_label=label,
                )
                all_days.extend(days)
            except SoHappyError as exc:
                logger.warning(
                    "Menu %s (%s) indisponible : %s",
                    menu_id, label or "?", exc,
                )
        return all_days

    def _fetch_menus_for(
        self,
        *,
        client_id: int,
        menu_id: int,
        begin: date,
        end: date,
        menu_label: str,
    ) -> list[MenuDay]:
        logger.info(
            "Menus client=%s menu=%s fenêtre %s → %s…",
            client_id, menu_id, begin, end,
        )
        # L'endpoint So-Happy (vue GraphQL utilisée par l'app Angular) :
        # `clients/{cid}/menus/{mid}/restaurant-menus`
        url_candidates = [
            f"/clients/{client_id}/menus/{menu_id}/restaurant-menus",
            f"/users/childrens/{client_id}/menus/{menu_id}/restaurant-menus",
        ]
        last_err: SoHappyError | None = None
        for url in url_candidates:
            try:
                resp = self._client.get(
                    url,
                    params={
                        "begin_date": begin.isoformat(),
                        "end_date": end.isoformat(),
                    },
                    headers=self._headers(),
                )
            except httpx.HTTPError as exc:
                last_err = SoHappyError(
                    f"Erreur réseau récupération menus : {exc}",
                )
                continue
            if resp.status_code == 200:
                payload = resp.json()
                return _parse_menus_payload(payload, menu_label=menu_label)
            last_err = SoHappyError(
                f"Menus indisponibles ({resp.status_code}) : {resp.text[:200]}",
            )
        if last_err:
            raise last_err
        raise SoHappyError("Aucun endpoint menus n'a répondu")


# ------------------------------------------------------------------------
# Helpers de parsing
# ------------------------------------------------------------------------


def _parse_menus_payload(
    payload: Any, *, menu_label: str,
) -> list[MenuDay]:
    """Convertit la réponse JSON So-Happy en ``list[MenuDay]``.

    Format réel observé (juin 2025+) :

    .. code-block:: json

        [
          {
            "date": "2026-09-14T00:00:00",
            "menus": [
              {
                "name": "Elémentaire",
                "categories": [
                  # Each category has a code + label + products[].
                  {"code": "ENTREE", "products": [...]},
                  {"code": "PLAT", "products": [...]},
                  {"code": "GARNITURE", "products": [...]},
                  {"code": "LAITAGE", "products": [...]},
                  {"code": "DESSERT", "products": [...]},
                  {"code": "GOUTER", "products": [...]},
                ],
              }
            ]
          },
          ...
        ]

    On tolère aussi un format dict ``{items: [...]}`` ou une liste plate d'items
    (legacy) avec ``category_code`` au niveau item.
    """
    # Cas liste plate legacy
    if isinstance(payload, list) and payload and "menus" not in payload[0] if payload else False:
        return _parse_legacy_flat_list(payload, menu_label=menu_label)

    # Cas standard (vraie API) : liste de {date, menus}
    if isinstance(payload, dict):
        items = payload.get("items") or payload.get("data") or payload.get("menus") or []
        if isinstance(items, list):
            return _parse_standard_list(items, menu_label=menu_label)
        return []

    if isinstance(payload, list):
        return _parse_standard_list(payload, menu_label=menu_label)

    return []


def _parse_standard_list(
    days: list[Any], *, menu_label: str,
) -> list[MenuDay]:
    """Parse le format réel de l'API : ``[{date, menus: [...]}, ...]``."""
    result: list[MenuDay] = []
    for day_entry in days:
        if not isinstance(day_entry, dict):
            continue
        day = _extract_date(day_entry)
        if day is None:
            continue
        # name peut venir du day_entry (legacy) ou d'un menu interne
        day_name = day_entry.get("name") or menu_label
        # menus est une liste de "menus journaliers" (peut y en avoir plusieurs
        # par jour, ex : "Elémentaire" + "Goûter")
        day_items: list[MenuItem] = []
        menus = day_entry.get("menus") or []
        for m in menus:
            if not isinstance(m, dict):
                continue
            inner_name = m.get("name") or day_name
            categories = m.get("categories") or []
            for cat in categories:
                if not isinstance(cat, dict):
                    continue
                cat_code = cat.get("code") or ""
                category = classify_category(cat_code)
                products = cat.get("products") or []
                for p in products:
                    if not isinstance(p, dict):
                        continue
                    label = (
                        p.get("commercial_label")
                        or p.get("commercial_label_html")
                        or p.get("name")
                        or ""
                    )
                    # strip basic HTML tags from label_html
                    if label and "<" in label:
                        import re
                        label = re.sub(r"<[^>]+>", "", label).strip()
                    if not label:
                        continue
                    allergens_raw = p.get("allergens") or []
                    if isinstance(allergens_raw, list):
                        allergens = [
                            str(a.get("label") or a.get("code") or "")
                            for a in allergens_raw
                            if isinstance(a, dict)
                        ]
                        allergens = [a for a in allergens if a]
                    else:
                        allergens = []
                    flags = detect_label_flags(p)
                    # append name as label prefix for context
                    # (e.g., "Goûter" → "Compote (Goûter)")
                    item_label = _clean_typography(label)
                    if inner_name and inner_name.lower() not in item_label.lower():
                        item_label = f"{item_label} ({inner_name})"
                    day_items.append(
                        MenuItem(
                            label=item_label,
                            category=category,
                            allergens=allergens,
                            raw=p,
                            **flags,
                        )
                    )
        if day_items:
            result.append(
                MenuDay(
                    jour=day,
                    items=day_items,
                    label=_clean_typography(day_name),
                    raw={"date": day.isoformat(), "menu_count": len(menus)},
                )
            )
    return result


def _parse_legacy_flat_list(
    items: list[Any], *, menu_label: str,
) -> list[MenuDay]:
    """Fallback legacy : liste plate ``[{date, label, category_code}, ...]``."""
    by_day: dict[date, list[MenuItem]] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        day = _extract_date(raw)
        if day is None:
            continue
        label = (
            raw.get("label")
            or raw.get("name")
            or raw.get("title")
            or raw.get("recipe_name")
            or ""
        )
        if not label:
            continue
        cat_raw = (
            raw.get("category_code")
            or raw.get("category")
            or raw.get("meal_type")
            or ""
        )
        category = classify_category(str(cat_raw))
        flags = detect_label_flags(raw)
        allergens_raw = raw.get("allergens") or raw.get("allergen_list") or []
        if isinstance(allergens_raw, str):
            allergens = [a.strip() for a in allergens_raw.split(",") if a.strip()]
        elif isinstance(allergens_raw, list):
            allergens = [str(a) for a in allergens_raw]
        else:
            allergens = []
        item = MenuItem(
            label=_clean_typography(str(label).strip()),
            category=category,
            allergens=allergens,
            raw=raw,
            **flags,
        )
        by_day.setdefault(day, []).append(item)

    return [
        MenuDay(jour=d, items=items_list, label=_clean_typography(menu_label), raw={"day": d.isoformat()})
        for d, items_list in sorted(by_day.items())
    ]


def _clean_typography(s: str) -> str:
    """Réinjecte un espace entre une particule FR et le mot qui suit collé.

    So-Happy concatène souvent les libellés sans espace après les particules :
    ``Menu dePaul``, ``etVinaigrette``, ``duPain``, ``accompagnementPurée``.
    Le LLM corrige en contexte, mais pour le fallback déterministe et la
    lecture brute (CalDAV, WebUI) on normalise en amont.

    Liste volontairement保守iste (particules courantes) — étendue
    possible sans risque de casser les autres libellés.
    """
    if not s:
        return s
    import re
    # de|du|des|le|la|les|et|à|aux|en|ou + lettre capitale → espace.
    return re.sub(
        r"\b(de|du|des|le|la|les|et|à|aux|en|ou)\s*([A-ZÀ-Ü][a-zà-ÿ]+)",
        lambda m: f"{m.group(1)} {m.group(2)}",
        s,
    )


def _extract_date(raw: dict[str, Any]) -> date | None:
    """Trouve une date dans un item So-Happy (champ ``date``, ``service_date``…)."""
    for key in ("date", "service_date", "day", "menu_date"):
        val = raw.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            try:
                return date.fromisoformat(val[:10])
            except ValueError:
                continue
        if hasattr(val, "date"):
            try:
                return val.date()  # type: ignore[no-any-return]
            except Exception:  # noqa: BLE001
                continue
    return None
