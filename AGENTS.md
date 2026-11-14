# AGENTS.md — ha_menu

> Document pour les agents IA (Open Code, Claude, Gemini, MCP…) qui
> travaillent sur ce projet. **Source de vérité** : en cas de doute,
> suivre ce fichier avant de toucher au code.

## Vue d'ensemble

`ha_menu` extrait le menu cantine depuis le portail So-Happy
(API REST `apim-production.so-happy.fr/api-app/`) et le publie dans un
agenda Home Assistant par enfant (Local Calendar conseillé). Depuis la
v2.0.0, c'est une **app Home Assistant** (ex-add-on) construite par le
Supervisor depuis ce dépôt (`repository.yaml` + dossier `ha_menu/`).

- Config persistante JSON `/data/config.json` (`chmod 600`), éditée dans la WebUI.
- WebUI FastAPI servie par **Ingress** ; scheduler APScheduler interne.
- Accès HA : `SUPERVISOR_TOKEN` → `http://supervisor/core` ; en dev `HA_URL` + `HA_TOKEN`.
- Modèle suivi : `~/projets/guichet-unique` (`cabatech/ha-guichet-unique`).

## Conventions du projet

| Élément | Convention |
|---|---|
| Langage | Python ≥ 3.11 (uv-managed) |
| Style | `ruff` (config dans `pyproject.toml`, règles E/F/W/I/N/UP/B/A/C4/SIM) |
| Tests | `pytest`, zéro réseau (mocks `httpx` via `respx` ou `unittest.mock`) |
| Format | `pyproject.toml` (hatchling backend) — pas de `setup.py` ni `setup.cfg` |
| Commits | Français, préfixe optionnel `[ha_menu]` |
| Logs | En français, emoji-friendly (`✓`, `✗`, `⚠`) |

## Commandes utiles (dev local)

```bash
cd ha_menu
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev,scheduler,web]"
.venv/bin/pytest -ra
.venv/bin/ruff check src tests

# CLI (config : /data/config.json par défaut, sinon --config)
.venv/bin/ha-menu --config ./data/config.json sync --no-publish
HA_URL=http://homeassistant.local:8123 HA_TOKEN=… .venv/bin/ha-menu --config ./data/config.json list

# Image (le Supervisor fait pareil)
uv lock && docker build -t ha-menu:test .
```

## Variables d'environnement

Les credentials sont dans `/data/config.json`, pas dans l'environnement.

| Variable | Description |
|---|---|
| `SUPERVISOR_TOKEN` | Fourni par le Supervisor (app HA) |
| `HA_URL` / `HA_TOKEN` | Accès HA hors app (dev) |
| `CANTINE_BASIC_USER` / `CANTINE_BASIC_PASS` | Auth Basic si le port est exposé hors Ingress |

## Structure du code

```
ha_menu/src/ha_menu/
├── __init__.py          # version
├── cli.py               # argparse : sync / discover / refresh / check / list / web / daemon
├── config.py            # AppConfig + SoHappyCredentials + load/save (chmod 600)
│                        # + champs LLM (minimax_*)
├── models.py            # Child, MenuDay, MenuItem, MealCategory
│                        # MenuDay.summary (court 5 lignes) + detailed_description (verbose)
│                        # format_menu_short() = fallback déterministe sans LLM
├── sohappy.py           # Client API So-Happy (login + list_children + menus)
│                        # _clean_typography() corrige "Menu dePaul" → "Menu de Paul"
├── llm.py               # Module LLM : system prompt strict, format 5 lignes, retry
│                        # endpoint="anthropic" (défaut) ou "openai"
│                        # _strip_think_tags() (M2.x) + _chat_with_retry() + _call_with_format_retry()
│                        # (5 today_<cat> + 5 tomorrow_<cat> + calendar)
│                        # via HA Discovery (homeassistant/* topics en retain)
├── pipeline.py          # Orchestrateur : login → menus → agenda HA
│                        # enhance_days() retourne (days, stats)
├── ha_calendar.py       # Agenda HA : REST (lecture, create_event) + WebSocket (delete)
│                        # marqueur « ha-menu-id: <enfant>-<date> » dans la description
├── web.py               # FastAPI : status / config / sync / enfants / logs / menus
│                        # Routes publiques bypass auth : /menus*, /static/*
│                        # Endpoints LLM : POST /api/llm, /api/llm/test
└── web/
    ├── templates/       # Jinja2 (base, index, settings, logs, menus, _status, _logs)
    └── static/          # pico.min.css + htmx.min.js + app.css
```

## API So-Happy (reverse-engineered)

Voir [[ha_menu/README]] § "API So-Happy". En résumé :

| Endpoint | Méthode |
|---|---|
| `/tokens` | POST (login) |
| `/users/childrens` | GET (liste enfants) |
| `/clients/{cid}/menus/{mid}/restaurant-menus` | GET (menus) |

Auth : `Authorization: Bearer <token>`.

Le payload `/restaurant-menus` est **instable** selon les déploiements —
`sohappy._parse_menus_payload` accepte 3 formats (liste directe, dict avec
`items`/`data`/`menus`, libellé ou `category_code`). Toute évolution de
l'API doit être tracée ici et testée via les fixtures `tests/fixtures/`.

## Format de la description — 5 lignes (LLM ou fallback)

Chaque événement d'agenda expose exactement 5 lignes, suivies de la ligne marqueur :

```
Entrée : choix 1 / choix 2
Plat : choix 1 / choix 2
Garnitures : choix 1 / choix 2
Laitage : choix 1 / choix 2
Dessert : choix 1
```

- **Aucun** allergène, flag végé/bio, emoji.
- **Source** : `MenuDay.summary` lit `raw['llm_summary']` (posé par le LLM)
  ou bascule sur `format_menu_short()` (déterministe Python, mêmes labels).
- Si le LLM est désactivé / KO après retries, le fallback local produit
  le même format (sans le nettoyage marketing).

## Module LLM (`llm.py`)

- **Endpoint** : `minimax_endpoint` config = `"anthropic"` (défaut) ou
  `"openai"`. Anthropic expose `POST {base}/anthropic/v1/messages`
  (MiniMax a confirmé cette URL en 2025). OpenAI : `/v1/chat/completions`.
- **Retry HTTP** : `llm_max_retries=2` (3 essais total) avec backoff
  exponentiel `llm_retry_delay=0.5 * 2^attempt`.
- **Retry format** : 1 essai supplémentaire si réponse valide HTTP
  mais 5 libellés manquants.
- **thinking disabled** (Anthropic) : évite que M2.x/M3 ne renvoie
  juste un bloc `<think>...</think>` vide.

## Intégration HA (`custom_components/ha_menu/`)

- Intégration compagnon, installée par HACS depuis ce dépôt. Elle lit l'API de
  l'app (`/api/menus`, `/api/status`, `/api/sync`) sur le réseau interne, sans jeton.
- L'app s'annonce au Supervisor au démarrage (`discovery: [ha_menu]`) :
  l'intégration est proposée dans Paramètres → Intégrations.
- Logique « cantine » pure dans `cantine.py`, testée par `tests/integration/`
  (`uv run --no-project --with pytest pytest tests/integration`).
- Carte `custom:ha-menu-card` servie par l'intégration (`frontend/`).
- App et intégration partagent le numéro de version.

## Garde-fous à respecter

1. **Pas de delete d'orphelins par défaut** : un sync auto ne supprime
   jamais d'événement d'agenda. Le ménage nécessite une commande manuelle
   (`sync` sans `--no-delete`).
2. **Pas de mots de passe dans les logs** ni dans `/api/config` (juste
   `*_password_set: bool`).
3. **`config.json` chmod 600** : seul l'UID applicatif peut le lire.
4. **Auth HTTP Basic** hors Ingress. Bypass : proxy Ingress (`172.30.32.2`) et LAN.
5. **Ne jamais toucher aux événements non marqués** d'un agenda HA.
6. **Pas de modification de l'agenda des enfants désactivés** : un toggle
   `enabled=false` doit stopper la publication.

## Tests

| Fichier | Couverture |
|---|---|
| `test_config.py` | round-trip JSON, masquage mots de passe, chmod 600 |
| `test_models.py` | classify_category, detect_label_flags, MenuDay.summary |
| `test_sohappy.py` | `_parse_menus_payload` (3 formats), `_extract_date` |
| `test_ha_calendar.py` | payload journée entière, marqueur, plan garder/supprimer/créer |
| `test_pipeline.py` | `run_extraction` (mocks), `discover_children` (merge) |
| `test_cli.py` | Parser argparse (toutes les sous-commandes) |

Aucun test ne tape le réseau : `httpx` est mocké via
`unittest.mock.patch("ha_menu.pipeline.SohappyClient")`.

## Dépendances

### Python (pip)
- `httpx` — client HTTP API So-Happy
- `websockets` — suppression d'événements HA (WebSocket)
- `python-dotenv` — `.env`
- `rich` — CLI
- `tenacity` — retries

### Python (optional)
- `scheduler` : `apscheduler>=3.10.0` (mode daemon + auto-sync WebUI)
- `web` : `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`
- `dev` : `pytest`, `pytest-asyncio`, `respx`, `ruff`

### Système (Dockerfile stage 2)
- `tzdata`
- `ca-certificates`

## Pièges connus

### 1. Le marqueur doit rester stable

`ha-menu-id: <child_id>-<date>` identifie nos événements (HA ne laisse
pas choisir l'UID). Le changer = tous les anciens événements deviennent
orphelins (supprimés au prochain ménage) et recréés.

### 2. `apim-production.so-happy.fr/api-app/` peut changer

L'API REST est reverse-engineered depuis le bundle JS Angular. Si
Sodexo/Witco change le schéma, `sohappy._parse_menus_payload` doit
accepter le nouveau format. Tester avec une fixture JSON réelle.

### 3. APScheduler + reload

Modifier `sync_cron` dans la WebUI **redémarre** le scheduler au prochain
boot mais pas à chaud. Pour appliquer immédiatement : redémarrer l'app dans HA.

### 4. LAN bypass auth : ne pas l'étendre

Le middleware bypass auth sur les IP LAN (192.168/16, 127/8) et hostnames
`.local/.lan/.nas/.home/.internal` est volontairement restrictif. Ne pas
ajouter 10.0.0.0/8 (souvent utilisé par le CGNAT/NPM upstream — l'IP vue
par FastAPI y ressemble mais ce n'est pas un LAN de confiance).

### 5. `/data` est le stockage de l'app

Il est inclus dans les sauvegardes HA de l'app. Désinstaller l'app l'efface.
