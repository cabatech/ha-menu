"""Module LLM pour le reformatage des menus So-Happy.

Supporte deux formats d'endpoint MiniMax :
- ``anthropic`` (par défaut) → ``POST /v1/messages``, header ``x-api-key``,
  format Anthropic natif, avec ``thinking: {"type": "disabled"}`` pour
  éviter le thinking involontaire qui peut produire des réponses vides
  sur M2.7.
- ``openai`` (fallback) → ``POST /v1/chat/completions``, header Bearer,
  compatible OpenAI Chat Completions.

Le reformatage vise à produire un résumé **court et lisible** pour un
calendrier CalDAV, format exactement 5 lignes :

    Entrée : choix 1 / choix 2
    Plat : choix 1 / choix 2
    Garnitures : choix 1 / choix 2
    Laitage : choix 1 / choix 2
    Dessert : choix 1

Aucun allergène, aucun flag végé/bio/local/fait-maison, aucun emoji. Les
catégories absentes restent affichées avec un libellé seul (« Dessert : »).

Si le LLM échoue ou n'est pas configuré, ``format_menu_short`` produit le
même rendu déterministe en Python (cf. ``models.format_menu_short``).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import (
    ANTHROPIC_API_VERSION,
    DEFAULT_MINIMAX_BASE_URL,
    DEFAULT_MINIMAX_ENDPOINT,
    DEFAULT_MINIMAX_MODEL,
    DEFAULT_MINIMAX_TIMEOUT,
    AppConfig,
)
from .models import MenuDay

logger = logging.getLogger(__name__)


# Ré-export pour les imports externes (compat ascendante).
DEFAULT_BASE_URL = DEFAULT_MINIMAX_BASE_URL
DEFAULT_MODEL = DEFAULT_MINIMAX_MODEL
DEFAULT_TIMEOUT = DEFAULT_MINIMAX_TIMEOUT
DEFAULT_ENDPOINT = DEFAULT_MINIMAX_ENDPOINT

# Nombre de tentatives supplémentaires quand la réponse LLM est valide HTTP
# (200 OK) mais ne respecte PAS le format attendu (5 libellés manquants,
# réponse tronquée, refus, etc.). 1 par défaut = 2 essais au total.
FORMAT_RETRIES = 1


# Prompt système — reformulation des menus de cantine pour CalDAV (v2).
# On vise un rendu FAMILIAL et NATUREL en français, pas une juxtaposition
# mécanique "A / B / C". On laisse le LLM faire la cohérence sémantique
# (c'est précisément pour ça qu'on l'utilise et pas un simple parseur).
SYSTEM_PROMPT = (
    "Tu reformates des menus de cantine scolaire en français pour un "
    "calendrier familial. La sortie est destinée à des parents qui veulent "
    "savoir en un coup d'œil ce que mange leur enfant aujourd'hui.\n"
    "\n"
    "RÈGLE DE SORTIE (non négociable) : EXACTEMENT 5 lignes, dans cet ordre, "
    "avec ces libellés suivis de deux-points :\n"
    "  Entrée : ...\n"
    "  Plat : ...\n"
    "  Garnitures : ...\n"
    "  Laitage : ...\n"
    "  Dessert : ...\n"
    "\n"
    "PHRASÉ NATUREL EN FRANÇAIS (c'est le point clé) :\n"
    "- Préfère des phrases françaises cohérentes à des juxtapositions "
    "mécaniques séparées par « / ».\n"
    "- Utilise « et », « à la », « avec », « ou » selon le sens.\n"
    "- Pour deux plats liés (ex. salade + sa sauce, yaourt + son sucre), "
    "Forme une seule phrase : « Salade de tomates à la vinaigrette moutarde » "
    "PAS « Salade de tomates / vinaigrette moutarde ».\n"
    "- Pour deux choix alternatifs (viande ou poisson), utilise « ou » : "
    "« Poulet rôti ou colin sauce citron » PAS « Poulet rôti / colin sauce "
    "citron ».\n"
    "- Pour lister, utilise des virgules et un « et » final si besoin.\n"
    "- Reste COURT. Une ligne par catégorie, pas une phrase littéraire.\n"
    "\n"
    "NETTOYAGE DES DONNÉES BRUTES :\n"
    "- Retire les mentions parasites entre parenthèses qui n'ont rien à "
    "faire dans un menu familial : « (Elémentaire) », « (Maternelle) », "
    "« (de porc) » quand redondant, suffixes techniques (« PMD », « LR », "
    "« VF ») quand ils n'apportent rien au parent.\n"
    "- NE GARDE PAS : allergènes, flags végé/bio/local/fait-maison, "
    "préfixes « •/-/* », en-têtes, mentions marketing (« façon grand-mère », "
    "« et sa sauce maison »).\n"
    "- NE mets PAS de préfixe « Régime : ... », « Menu : ... » ou autre en-tête.\n"
    "\n"
    "CATÉGORIES VIDES : écris juste « Catégorie : » (rien après). Exemple : "
    "« Dessert : » si pas de dessert.\n"
    "\n"
    "MENU VIDE : si la liste de plats est totalement vide, renvoie exactement : "
    "MENU_VIDE\n"
    "\n"
    "EXEMPLES ATTENDUS (input → output) :\n"
    "\n"
    "Input :\n"
    '[{"category":"entree","label":"Salade de tomates (Elémentaire)"},\n'
    ' {"category":"entree","label":"et vinaigrette moutarde (Elémentaire)"}]\n'
    "Output :\n"
    "Entrée : Salade de tomates à la vinaigrette moutarde\n"
    "\n"
    "Input :\n"
    '[{"category":"plat","label":"Aiguillette de poulet VF sauce moutarde (Elémentaire)"},\n'
    ' {"category":"plat","label":"Merlu PMD sauce aux épices douces (Elémentaire)"}]\n'
    "Output :\n"
    "Plat : Aiguillette de poulet sauce moutarde ou Merlu sauce aux épices douces\n"
    "\n"
    "Input :\n"
    '[{"category":"dessert","label":"Cake nature (Elémentaire)"}]\n'
    "Output :\n"
    "Dessert : Cake nature\n"
    "\n"
    "AUTRES RÈGLES :\n"
    "1. N'invente JAMAIS de plat. Si un champ est vide ou absent, la "
    "catégorie reste vide.\n"
    "2. NE mets AUCUN emoji.\n"
    "3. NE réponds RIEN d'autre que les 5 lignes (ou MENU_VIDE). Pas de "
    "phrase introductive, pas de note de fin, pas de « Voici le menu : ».\n"
    "4. Tout en français. Si un libellé est dans une autre langue, "
    "traduis-le en français.\n"
    "5. Conserve les accents et la cédille français (é, è, ê, à, ç, ù…). "
    "Ne JAMAIS les remplacer par leur équivalent ASCII (fraises ≠ fraiches).\n"
)


# ------------------------------------------------------------------------
# Exceptions
# ------------------------------------------------------------------------


class LLMError(RuntimeError):
    """Erreur d'appel à l'API LLM."""


# ------------------------------------------------------------------------
# Client LLM
# ------------------------------------------------------------------------


@dataclass(slots=True)
class LLMClient:
    """Client HTTP pour l'API MiniMax (Anthropic- ou OpenAI-compatible).

    Lit sa configuration depuis ``AppConfig`` :
        - ``config.minimax_api_key``      (clé API)
        - ``config.minimax_base_url``     (default https://api.minimax.io)
        - ``config.minimax_model``        (default MiniMax-M2.7)
        - ``config.minimax_timeout``      (default 30s)
        - ``config.minimax_endpoint``     ("anthropic" par défaut, "openai")
    """

    config: AppConfig
    http_client: httpx.Client | None = field(default=None, repr=False)
    """Client HTTP à réutiliser (utile pour les tests/mock).

    Si ``None`` (cas normal), un client est créé à la construction.
    """

    def __post_init__(self) -> None:
        if self.http_client is None:
            base_url = self.config.minimax_base_url.rstrip("/")
            # Tolère les URLs qui finissent par /v1 (config legacy) — on
            # l'enlève car on concatène explicitement /v1/{messages,
            # chat/completions} selon l'endpoint.
            if base_url.endswith("/v1"):
                base_url = base_url[: -len("/v1")]
            headers = self._auth_headers()
            headers["Content-Type"] = "application/json"
            self.http_client = httpx.Client(
                base_url=base_url,
                headers=headers,
                timeout=self.config.minimax_timeout,
            )

    def _auth_headers(self) -> dict[str, str]:
        """Headers d'authentification selon le type d'endpoint."""
        if self.config.minimax_endpoint == "openai":
            return {
                "Authorization": f"Bearer {self.config.minimax_api_key}",
            }
        # anthropic (par défaut) — compatible Anthropic Messages API.
        return {
            "x-api-key": self.config.minimax_api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        }

    def _endpoint_path(self) -> str:
        """Path de l'endpoint POST selon le type configuré.

        Note : l'API anthropic-compatible de MiniMax est exposée à
        ``/anthropic/v1/messages`` (et NON ``/v1/messages`` qui n'existe pas).
        """
        if self.config.minimax_endpoint == "openai":
            return "/v1/chat/completions"
        return "/anthropic/v1/messages"

    def close(self) -> None:
        if self.http_client is not None:
            self.http_client.close()
            self.http_client = None

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.config.minimax_api_key)

    def test_connection(self) -> dict[str, Any]:
        """Vérifie que la clé API fonctionne (1 appel léger).

        Renvoie ``{"ok": True, "model": "...", "endpoint": "...",
        "latency_ms": 42}`` ou ``{"ok": False, "error": "..."}``.
        """
        if not self.is_configured():
            return {"ok": False, "error": "Clé API non configurée"}
        assert self.http_client is not None
        try:
            if self.config.minimax_endpoint == "openai":
                payload = {
                    "model": self.config.minimax_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                }
            else:
                payload = {
                    "model": self.config.minimax_model,
                    "max_tokens": 5,
                    "system": "You are a helpful assistant.",
                    "messages": [{"role": "user", "content": "ping"}],
                    "thinking": {"type": "disabled"},
                }
            t0 = time.monotonic()
            resp = self.http_client.post(self._endpoint_path(), json=payload)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code != 200:
                return {
                    "ok": False,
                    "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                }
            return {
                "ok": True,
                "model": self.config.minimax_model,
                "endpoint": self.config.minimax_endpoint,
                "latency_ms": latency_ms,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def chat(self, messages: list[dict[str, str]], *, max_tokens: int = 1200) -> str:
        """Appel à l'endpoint configuré (anthropic ou openai), renvoie texte."""
        if not self.is_configured():
            raise LLMError("Clé API MiniMax non configurée.")
        assert self.http_client is not None
        if self.config.minimax_endpoint == "openai":
            return self._chat_openai(messages, max_tokens=max_tokens)
        return self._chat_anthropic(messages, max_tokens=max_tokens)

    # ------------------------------------------------------------------
    # OpenAI-compatible
    # ------------------------------------------------------------------

    def _chat_openai(
        self, messages: list[dict[str, str]], *, max_tokens: int,
    ) -> str:
        payload = {
            "model": self.config.minimax_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        try:
            resp = self.http_client.post(self._endpoint_path(), json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"Erreur réseau : {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code} : {resp.text[:200]}")
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Réponse JSON invalide : {exc}") from exc
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"Réponse sans choices : {data}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not content:
            raise LLMError(f"Réponse sans contenu : {data}")
        # Strip ``<think>…</think>`` pour les modèles M2.x (cf. doc MiniMax).
        return _strip_think_tags(content.strip())

    # ------------------------------------------------------------------
    # Anthropic-compatible (par défaut — recommandé)
    # ------------------------------------------------------------------

    def _chat_anthropic(
        self, messages: list[dict[str, str]], *, max_tokens: int,
    ) -> str:
        # Anthropic Messages API : "system" est top-level (pas un message).
        system_text = ""
        conv: list[dict[str, Any]] = []
        for m in messages:
            if m.get("role") == "system":
                system_text = m.get("content", "")
            else:
                conv.append(m)
        if not system_text:
            # L'API Anthropic n'accepte pas un system vide.
            system_text = "You are a helpful assistant."
        payload: dict[str, Any] = {
            "model": self.config.minimax_model,
            "system": system_text,
            "messages": conv,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            # Désactive explicitement le thinking de M2.7 qui peut sinon
            # produire des réponses vides (juste un <thinking>...</thinking>
            # sans contenu textuel).
            "thinking": {"type": "disabled"},
        }
        try:
            resp = self.http_client.post(self._endpoint_path(), json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"Erreur réseau : {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code} : {resp.text[:200]}")
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Réponse JSON invalide : {exc}") from exc
        # Format Anthropic : {content: [{type: "text", text: "..."}, ...]}
        content_blocks = data.get("content") or []
        text_parts: list[str] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(block.get("text", "") or "")
        text = "\n".join(text_parts).strip()
        if not text:
            raise LLMError(
                f"Réponse sans contenu (blocks={content_blocks!r}, "
                f"stop_reason={data.get('stop_reason')!r})",
            )
        return text


# ------------------------------------------------------------------------
# Reformatage des menus
# ------------------------------------------------------------------------


def _menu_items_to_payload(day: MenuDay) -> list[dict[str, Any]]:
    """Convertit un MenuDay en payload JSON pour le LLM.

    On omet volontairement les flags (is_vegetarian, is_organic, is_local,
    is_homemade) — le prompt demande de ne PAS les afficher. On garde les
    allergènes dans le payload pour information du LLM, mais le prompt lui
    interdit de les reproduire.
    """
    items: list[dict[str, Any]] = []
    for it in day.items:
        items.append({
            "category": it.category.value,
            "category_label": it.category.label,
            "label": it.label,
            "allergens": list(it.allergens) if it.allergens else [],
        })
    return items


def enhance_menu(day: MenuDay, *, config: AppConfig) -> MenuDay:
    """Reformate un ``MenuDay`` via le LLM et stocke le résultat dans
    ``day.raw['llm_summary']`` (qui sera ensuite lu par ``MenuDay.summary``).

    Renvoie le **même** MenuDay (muté) pour rester compatible avec
    ``pipeline.run_extraction``. Si le LLM est désactivé, non configuré ou
    en échec, on bascule sur le formatage déterministe Python (cf.
    ``models.format_menu_short``) qui produit exactement la même structure.

    Idempotent : appelé plusieurs fois sur le même jour, le LLM est
    ré-interrogé (la fonction ne cache rien volontairement).
    """
    from .models import format_menu_short

    if not day.items:
        return day

    # Si le LLM est désactivé ou la clé absente, on bascule direct sur
    # le fallback (utile pour debug / mode « sans réseau »).
    if not config.use_llm_for_menus or not config.minimax_api_key:
        text = format_menu_short(day)
        new_raw = {**day.raw, "llm_summary": text, "llm_source": "fallback"}
        return MenuDay(
            jour=day.jour,
            service=day.service,
            items=day.items,
            label=day.label,
            raw=new_raw,
        )

    client = LLMClient(config)
    try:
        items_payload = _menu_items_to_payload(day)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Date : {day.jour.isoformat()}\n\n"
                    "Plats (JSON) :\n"
                    f"{json.dumps(items_payload, ensure_ascii=False, indent=2)}"
                ),
            },
        ]
        text, source = _call_with_format_retry(
            client,
            messages,
            max_retries=config.llm_max_retries,
            retry_delay=config.llm_retry_delay,
            context=str(day.jour),
        )
        if text is None:
            text = format_menu_short(day)
            return MenuDay(
                jour=day.jour,
                service=day.service,
                items=day.items,
                label=day.label,
                raw={**day.raw, "llm_summary": text, "llm_source": "fallback"},
            )

        if text.strip().upper() == "MENU_VIDE":
            text = "Menu non communiqué pour cette date."

        if source == "llm":
            llm_source = "llm"
            summary = text
        else:
            # format-fallback : le LLM a répondu mais le format est invalide
            # même après le retry. On garde son texte pour debug + fallback
            # local propre.
            logger.warning(
                "LLM : format invalide pour %s après retry — fallback local\nRéponse : %r",
                day.jour, text[:200],
            )
            llm_source = "fallback"
            summary = format_menu_short(day)
        new_raw = {
            **day.raw,
            "llm_summary": summary,
            "llm_source": llm_source,
            "llm_raw": text,  # texte LLM brut (pour debug, toujours présent
                              # si le LLM a répondu)
        }
        return MenuDay(
            jour=day.jour,
            service=day.service,
            items=day.items,
            label=day.label,
            raw=new_raw,
        )
    finally:
        client.close()


def _looks_like_short_format(text: str) -> bool:
    """Vérifie grossièrement que le LLM a bien produit le format attendu."""
    expected = ("Entrée :", "Plat :", "Garnitures :", "Laitage :", "Dessert :")
    return all(label in text for label in expected)


def _strip_think_tags(text: str) -> str:
    """Retire les blocs ``…`` émis par les modèles M2.x.

    Référence : https://platform.minimax.io/docs/api-reference/text-openai-api
    (« the content field will contain `` tag content »).
    On retire tout ce qui est entre `` et `` (multiligne, greedy).
    """
    import re

    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _chat_with_retry(
    client: LLMClient,
    messages: list[dict[str, str]],
    *,
    max_retries: int,
    retry_delay: float,
    context: str = "",
) -> str | None:
    """Appelle ``client.chat`` avec retry exponentiel sur ``LLMError``.

    Renvoie le texte brut si succès, ``None`` si toutes les tentatives ont
    échoué (l'appelant tombe alors sur le fallback local). Les erreurs de
    format ne sont PAS retentées ici — c'est ``enhance_menu`` qui décide de
    re-tenter ou pas via cette même fonction.
    """
    import time

    last_exc: LLMError | None = None
    for attempt in range(max_retries + 1):
        try:
            return client.chat(messages)
        except LLMError as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = retry_delay * (2 ** attempt)
                logger.warning(
                    "LLM %s: tentative %d/%d échouée : %s — retry dans %.1fs",
                    context or "?", attempt + 1, max_retries + 1, exc, wait,
                )
                time.sleep(wait)
    logger.warning(
        "LLM %s: %d tentatives échouées — fallback : %s",
        context or "?", max_retries + 1, last_exc,
    )
    return None


def _stats_key(source: str) -> str:
    """Helper : mappe llm_source → clé de stats."""
    return source if source in ("llm", "fallback") else "fallback"


def _call_with_format_retry(
    client: LLMClient,
    messages: list[dict[str, str]],
    *,
    max_retries: int,
    retry_delay: float,
    format_retries: int = FORMAT_RETRIES,
    context: str = "",
) -> tuple[str | None, str]:
    """Composé : retry HTTP (transient) puis retry format si réponse non conforme.

    Sémantique du retour :
        - ``(None, "llm-unavailable")`` : tous les HTTP retries ont échoué,
          l'appelant bascule en fallback local.
        - ``(text, "llm")`` : la réponse respecte le format attendu
          (peut avoir nécessité 1 retry format intermédiaire).
        - ``(text, "format-fallback")`` : la dernière réponse LLM n'a pas
          passé le test de format malgré les retries — l'appelant applique
          alors son fallback local mais conserve le texte pour debug.

    Les retries HTTP et format sont indépendants : si le 1er HTTP retry
    échoue (LLMError), on s'arrête et on signale ``llm-unavailable``.
    """
    raw = _chat_with_retry(
        client,
        messages,
        max_retries=max_retries,
        retry_delay=retry_delay,
        context=context,
    )
    if raw is None:
        return None, "llm-unavailable"
    text = _strip_think_tags(raw)
    for attempt in range(format_retries):
        if _looks_like_short_format(text):
            return text, "llm"
        logger.warning(
            "LLM %s : format invalide (essai %d/%d) — retry format",
            context or "?", attempt + 1, format_retries + 1,
        )
        raw = _chat_with_retry(
            client,
            messages,
            max_retries=max_retries,
            retry_delay=retry_delay,
            context=f"{context or '?'} (format retry {attempt + 1})",
        )
        if raw is None:
            return None, "llm-unavailable"
        text = _strip_think_tags(raw)
    if _looks_like_short_format(text):
        return text, "llm"
    return text, "format-fallback"


def enhance_days(
    days: list[MenuDay], *, config: AppConfig,
) -> tuple[list[MenuDay], dict[str, int]]:
    """Variante batch de ``enhance_menu`` : reformate tous les jours d'une liste.

    Crée un seul ``LLMClient`` réutilisé pour tous les jours (un appel HTTP
    par jour avec retry exponentiel si activé, c'est lent mais résilient —
    un échec sur J+3 ne casse pas les autres).

    Renvoie ``(jours, stats)`` où ``stats`` est ::
        {"total": N, "llm": K, "fallback": M, "skipped": E}
    Permet à la WebUI / au CLI d'afficher combien de jours ont été
    traités par le LLM vs tombés en fallback local.
    """
    from .models import format_menu_short

    stats = {"total": 0, "llm": 0, "fallback": 0, "skipped": 0}
    if not days:
        return days, stats

    # Court-circuit : si LLM désactivé ou pas de clé, fallback immédiat
    # pour toute la liste (pas d'appel HTTP, pas de coût).
    if not config.use_llm_for_menus or not config.minimax_api_key:
        out: list[MenuDay] = []
        for d in days:
            if not d.items:
                stats["skipped"] += 1
                out.append(d)
                continue
            stats["total"] += 1
            stats["fallback"] += 1
            out.append(MenuDay(
                jour=d.jour,
                service=d.service,
                items=d.items,
                label=d.label,
                raw={**d.raw, "llm_summary": format_menu_short(d), "llm_source": "fallback"},
            ))
        return out, stats

    client = LLMClient(config)
    try:
        out = []
        for d in days:
            if not d.items:
                stats["skipped"] += 1
                out.append(d)
                continue
            stats["total"] += 1
            items_payload = _menu_items_to_payload(d)
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Date : {d.jour.isoformat()}\n"
                        f"Régime : {d.label or 'standard'}\n\n"
                        "Plats (JSON) :\n"
                        f"{json.dumps(items_payload, ensure_ascii=False, indent=2)}"
                    ),
                },
            ]
            text, source = _call_with_format_retry(
                client,
                messages,
                max_retries=config.llm_max_retries,
                retry_delay=config.llm_retry_delay,
                context=str(d.jour),
            )
            # On capture la réponse LLM brute AVANT de l'écraser dans
            # les branches ci-dessous — utile pour le debug quand
            # source == "format-fallback".
            llm_response = text
            if text is None:
                text = format_menu_short(d)
                source = "fallback"
            elif text.strip().upper() == "MENU_VIDE":
                # Cas particulier : "MENU_VIDE" → texte court "Menu non
                # communiqué pour cette date." qu'on substitue avant
                # validation du format.
                text = "Menu non communiqué pour cette date."
                source = (
                    "llm" if _looks_like_short_format(text) else "fallback"
                )
            elif source == "format-fallback":
                logger.warning(
                    "LLM : format invalide pour %s après retry — fallback local\nRéponse : %r",
                    d.jour, llm_response[:200] if llm_response else "",
                )
                text = format_menu_short(d)
                source = "fallback"
            if source == "llm":
                stats["llm"] += 1
            else:
                stats["fallback"] += 1
            raw_out = {
                **d.raw,
                "llm_summary": text,
                "llm_source": source,
            }
            # Toujours stocker le texte LLM brut s'il existe (succès OU
            # format-fallback). None si HTTP KO (llm-unavailable).
            if llm_response is not None and source != "llm-unavailable":
                raw_out["llm_raw"] = llm_response
            out.append(MenuDay(
                jour=d.jour,
                service=d.service,
                items=d.items,
                label=d.label,
                raw=raw_out,
            ))
        return out, stats
    finally:
        client.close()
