# Intégration HA « HA - Menu » Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ajouter au dépôt `cabatech/ha-menu` une intégration HA (HACS) qui lit les menus de l'app, expose un appareil par enfant (menu affiché, cantine aujourd'hui/demain, bouton de synchro) et livre une carte `custom:ha-menu-card`, puis retirer le MQTT de l'app.

**Architecture:** `custom_components/ha_menu/` à la racine du dépôt (à côté de `repository.yaml` et de l'add-on `ha_menu/`). Un `DataUpdateCoordinator` interroge l'API de l'app (`/api/menus`, `/api/status`, `/api/sync`) sur le réseau interne ; la logique « cantine » est un module pur (`cantine.py`, sans import HA) testé seul ; les entités recalculent localement à 21:00, 00:00 et à chaque changement d'état des entités d'inscription liées. La carte est un web component JS sans build servi par l'intégration.

**Tech Stack:** Python 3.12+, API Home Assistant (config entries, coordinator, `frontend.add_extra_js_url`), `aiohttp` (session HA), JS vanilla (custom element), pytest.

**Spec:** `docs/superpowers/specs/2026-11-14-integration-ha-design.md`

## Global Constraints

- Domaine d'intégration : `ha_menu`. Intégration et app partagent le numéro de version : **3.0.0** (rupture : MQTT retiré).
- L'intégration ne remplace pas l'app : aucune logique So-Happy / LLM dans `custom_components/`.
- Pas de jeton ni de secret entre intégration et app (réseau interne, middleware de l'app laisse passer les IP privées).
- Règle cantine du jour J : menu So-Happy pour J **et** inscription selon la source de l'enfant (`aucune` / `guichet_unique` / `pronote`).
- Bascule du menu affiché à **21:00** (avant : aujourd'hui ; à partir de 21:00 : demain), fuseau de HA (Europe/Paris).
- Inscription non établie (entité `unavailable`/`unknown`) → `cantine = None` : binary_sensor `unknown`, carte = menu + « inscription non vérifiée ».
- Entité accueil de loisirs absente (n'existe pas dans HA) → traitée comme « non inscrit à ce service ».
- Textes visibles en français. Pas de prénom en dur dans le code ni les tests (exemples : Paul, Dupont).
- Carte sans dépendance (pas de mushroom, pas de Lit importé d'un CDN).

## Review Focus

- Pronote après minuit : `today_s_timetable` encore sur la veille quelques minutes → le recalcul sur changement d'état doit corriger sans attendre le coordinateur (vérifié en recette, Task 6 step 5 : forcer l'état de l'entité liée avec `ha_set_entity`/état temporaire et constater la mise à jour).
- Guichet Unique au redémarrage : capteurs `unknown` 3 s → `cantine = None`, jamais `False` (test dans Task 1).
- Jour sans menu mais inscrit (mercredi sans accueil) → `False`, pas `None` (test dans Task 1).
- App injoignable pendant une mise à jour de l'add-on → les entités gardent les dernières données (coordinateur : `UpdateFailed` seulement après 3 échecs consécutifs, Task 2).
- Enfant ajouté dans l'app après la configuration → apparaît au prochain rechargement de l'entrée, sans options (source `aucune` par défaut) ; vérifié en recette.

---

### Task 1: Logique pure « cantine »

**Files:**
- Create: `custom_components/ha_menu/cantine.py`
- Test: `tests/integration/test_cantine.py`

**Interfaces:**
- Produces (module sans import `homeassistant`) :
  - `parse_summary(summary: str) -> list[dict[str, str]]` : lignes `"Libellé : valeur"` → `[{"label": "Entrée", "valeur": "..."}]`, lignes sans « : » ignorées.
  - `displayed_day(now: datetime) -> tuple[date, str]` : `(now.date(), "aujourd_hui")` si `now.hour < 21`, sinon `(now.date() + 1 j, "demain")`.
  - `inscription_guichet(cantine: str | None, loisirs: str | None, repas_loisirs: bool) -> bool | None` : états HA bruts (`"on"`, `"off"`, `"unknown"`, `"unavailable"`, `None` = entité absente).
  - `inscription_pronote(state: str | None, lessons: list[dict] | None) -> bool | None`.
  - `cantine(has_menu: bool, inscription: bool | None) -> bool | None`.
  - `UNSURE = ("unknown", "unavailable")`.

- [ ] **Step 1: Write the failing tests** (le test charge le module par chemin avec `importlib.util.spec_from_file_location`, pour ne pas importer `custom_components/ha_menu/__init__.py` qui dépend de HA)

```python
def test_parse_summary():
    assert cantine.parse_summary("Entrée : Carottes\nPlat : Poulet\nbruit") == [
        {"label": "Entrée", "valeur": "Carottes"}, {"label": "Plat", "valeur": "Poulet"}]

def test_displayed_day_switches_at_21h():
    assert cantine.displayed_day(datetime(2026, 11, 16, 20, 59)) == (date(2026, 11, 16), "aujourd_hui")
    assert cantine.displayed_day(datetime(2026, 11, 16, 21, 0)) == (date(2026, 11, 17), "demain")

@pytest.mark.parametrize("c,l,repas,exp", [
    ("on", None, True, True), ("off", "on", True, True), ("off", "on", False, False),
    ("off", None, True, False), ("off", "off", True, False),
    ("unknown", None, True, None), ("unavailable", "off", True, None),
    ("off", "unknown", True, None), ("on", "unknown", True, True),
])
def test_inscription_guichet(c, l, repas, exp):
    assert cantine.inscription_guichet(c, l, repas) is exp

def _lesson(morning, canceled=False):
    return {"is_morning": morning, "is_afternoon": not morning, "canceled": canceled}

def test_inscription_pronote():
    assert cantine.inscription_pronote("5", [_lesson(True), _lesson(False)]) is True
    assert cantine.inscription_pronote("3", [_lesson(True)]) is False            # mercredi matin
    assert cantine.inscription_pronote("2", [_lesson(True), _lesson(False, canceled=True)]) is False
    assert cantine.inscription_pronote("0", []) is False
    assert cantine.inscription_pronote("unavailable", None) is None
    assert cantine.inscription_pronote(None, None) is None

def test_cantine_rule():
    assert cantine.cantine(False, True) is False   # inscrit mais pas de menu
    assert cantine.cantine(True, None) is None
    assert cantine.cantine(True, True) is True
```

- [ ] **Step 2: Run tests to verify they fail**
Run: `cd ~/projets/ha-menu && uv run --no-project --with pytest pytest tests/integration -q`
Expected: FAIL (module absent).

- [ ] **Step 3: Implement the five functions in `cantine.py`** (stdlib uniquement).

- [ ] **Step 4: Run tests** — same command, Expected: all PASS.

- [ ] **Step 5: Commit** `feat(integration): logique cantine`

### Task 2: Squelette d'intégration, client API, config flow, coordinateur

**Files:**
- Create: `hacs.json` (`{"name": "HA - Menu", "render_readme": true}`), `custom_components/ha_menu/{__init__.py, manifest.json, const.py, api.py, coordinator.py, config_flow.py, strings.json, translations/fr.json}`

**Interfaces:**
- `const.py` : `DOMAIN = "ha_menu"`, `DEFAULT_PORT = 8237`, `SCAN_INTERVAL = timedelta(minutes=30)`, clés d'options `CONF_CHILDREN = "children"`, `CONF_SOURCE`, `CONF_CANTINE_TODAY`, `CONF_CANTINE_TOMORROW`, `CONF_LOISIRS_TODAY`, `CONF_LOISIRS_TOMORROW`, `CONF_TIMETABLE_TODAY`, `CONF_TIMETABLE_TOMORROW`, `CONF_REPAS_LOISIRS`, `SOURCES = ("aucune", "guichet_unique", "pronote")`.
- `api.py` : `class HaMenuApi(session: aiohttp.ClientSession, base_url: str)` avec `async status() -> dict`, `async children() -> list[dict]` (`GET /api/menus` → `children`), `async menus(child_id: int) -> dict` (`GET /api/menus/{id}`), `async sync() -> bool` (`POST /api/sync` `{}` ; `False` sur 409). Exception `HaMenuApiError`.
- `coordinator.py` : `HaMenuCoordinator(DataUpdateCoordinator[dict[int, dict]])` ; `data = {child_id: {"name": str, "days": {"YYYY-MM-DD": summary}}}`. Échec réseau : garder `self.data` et ne lever `UpdateFailed` qu'au 3ᵉ échec consécutif.
- `__init__.py` : `async_setup_entry` crée api + coordinateur dans `entry.runtime_data`, `async_config_entry_first_refresh`, forward vers `sensor`, `binary_sensor`, `button` ; listener de mise à jour des options → reload.
- `config_flow.py` : `async_step_user` (champ `url`, défaut `http://afb7d0ea-ha-menu:8237`), `async_step_hassio(discovery_info: HassioServiceInfo)` (URL depuis `config["host"]`/`config["port"]`, confirmation), validation par `status()`, `unique_id = DOMAIN` (une seule instance). Options flow : Task 3.
- `manifest.json` : `"config_flow": true`, `"iot_class": "local_polling"`, `"dependencies": ["frontend", "http"]`, `"version": "3.0.0"`, `"codeowners": ["@cabatech"]`.

- [ ] **Step 1: Write the files above.**
- [ ] **Step 2: Verify imports against the HA version running at home** (`ha_get_overview` donne la version) :
Run: `uv run --no-project --python 3.13 --with homeassistant==<version> python -c "import custom_components.ha_menu.config_flow, custom_components.ha_menu.coordinator"` depuis la racine du dépôt.
Expected: aucune erreur.
- [ ] **Step 3: Commit** `feat(integration): squelette, API et config flow`

### Task 3: Entités, options par enfant, recalcul local

**Files:**
- Create: `custom_components/ha_menu/{entity.py, sensor.py, binary_sensor.py, button.py}`
- Modify: `config_flow.py` (options flow), `strings.json`, `translations/fr.json`

**Interfaces:**
- Consumes: Task 1 (fonctions pures), Task 2 (`HaMenuCoordinator`, constantes).
- `entity.py` : `HaMenuChildEntity(CoordinatorEntity)` avec `child_id`, `device_info` = appareil « Cantine <nom> » (identifiers `(DOMAIN, f"child_{id}")`), `_attr_has_entity_name = True`, et `compute(day: date) -> tuple[list[dict], bool | None]` (lignes du menu, cantine) qui lit l'état des entités d'inscription via `hass.states.get`. Dans `async_added_to_hass` : `async_track_time_change(hour=21|0, minute=0, second=0)` et `async_track_state_change_event` sur les entités liées → `async_write_ha_state`.
- `sensor.py` : `menu_affiche` (translation_key `menu_affiche`, icône `mdi:silverware-fork-knife`). État : date en français court (« lun. 16 nov. »), attributs `date`, `jour` (`aujourd_hui`/`demain`), `lignes` (liste de Task 1), `cantine` (bool/None), `inscription_verifiee` (`cantine is not None`).
- `binary_sensor.py` : `cantine_aujourdhui`, `cantine_demain` ; `is_on` = `cantine(...)` (None → `unknown`).
- `button.py` : `synchroniser`, un seul, sur l'appareil « HA - Menu » (identifiers `(DOMAIN, "app")`) ; `api.sync()` puis `coordinator.async_request_refresh()` après fin de synchro (poll `status()["syncing"]` toutes les 5 s, 5 min max).
- Options flow : étape `init` = choix de l'enfant (liste du coordinateur) ; étape `child` = `source` (select), puis selon la source les sélecteurs d'entités (`binary_sensor` pour Guichet Unique, `sensor` pour Pronote) et `repas_loisirs` (bool, défaut `True`). Préremplissage : `binary_sensor.guichet_cantine_aujourdhui` / `_demain` / `binary_sensor.guichet_accueil_loisirs_aujourdhui` / `_demain` s'ils existent ; pour Pronote, premier `sensor.pronote_*_today_s_timetable` / `*_tomorrow_s_timetable`. Stockage : `options["children"][str(child_id)] = {...}`. Enfant sans options = source `aucune`.

- [ ] **Step 1: Write the files.**
- [ ] **Step 2: Re-run the Task 2 import check** (ajouter `sensor`, `binary_sensor`, `button`) and Task 1 tests. Expected: PASS.
- [ ] **Step 3: Commit** `feat(integration): entités et options par enfant`

### Task 4: Carte `custom:ha-menu-card`

**Files:**
- Create: `custom_components/ha_menu/frontend/ha-menu-card.js`
- Modify: `custom_components/ha_menu/__init__.py` (enregistrement de la carte dans `async_setup`)

**Interfaces:**
- Enregistrement : `hass.http.async_register_static_paths([StaticPathConfig("/ha_menu/ha-menu-card.js", <chemin>, True)])` puis `frontend.add_extra_js_url(hass, "/ha_menu/ha-menu-card.js?v=3.0.0")`, une seule fois par démarrage.
- Carte : `customElements.define("ha-menu-card", …)`, `window.customCards.push({type: "ha-menu-card", name: "Menu du jour", description: "Menu de cantine HA - Menu"})`. Config : `entity` (obligatoire), `title` (facultatif). `static getConfigForm()` → schéma avec `entity` (selector `entity`, `integration: ha_menu`, `domain: sensor`) et `title`. `getStubConfig(hass)` → premier `sensor.*` de l'intégration. `getCardSize()` → 4. Rendu dans un `ha-card`, couleurs par variables CSS du thème (`--primary-text-color`, `--secondary-text-color`, `--primary-color`).
- Textes exacts : titre « Menu de midi » (jour = `aujourd_hui`) ou « Menu de demain » ; état vide « Pas de cantine aujourd'hui » / « Pas de cantine demain » ; mention « inscription non vérifiée » quand `cantine` est `null` ; entité absente : « Entité introuvable ».

- [ ] **Step 1: Write the card and the registration.**
- [ ] **Step 2: Syntax check** : `node --check custom_components/ha_menu/frontend/ha-menu-card.js` → aucune sortie.
- [ ] **Step 3: Commit** `feat(integration): carte Menu du jour`

### Task 5: App 3.0.0 — retrait du MQTT, découverte Supervisor

**Files:**
- Delete: `ha_menu/src/ha_menu/mqtt_publisher.py`, `ha_menu/tests/test_mqtt_publisher.py`
- Modify: `ha_menu/src/ha_menu/{config.py, pipeline.py, web.py}` (champs, appels et routes `/api/mqtt*`), `ha_menu/src/ha_menu/web/templates/settings.html` (onglet MQTT), `ha_menu/pyproject.toml` (extra `mqtt`), `ha_menu/Dockerfile` (`--extra mqtt`), `ha_menu/config.yaml` (`discovery: [ha_menu]`, `version: "3.0.0"`), `pyproject.toml`/`__init__.py` (3.0.0), `uv.lock`, `README.md`, `AGENTS.md`, `ha_menu/README.md`
- Create: annonce au démarrage dans `web.py` (lifespan) : si `SUPERVISOR_TOKEN`, `POST http://supervisor/discovery` avec `{"service": "ha_menu", "config": {"host": os.environ["HOSTNAME"], "port": 8237}}` ; échec = log d'avertissement, jamais bloquant.

**Interfaces:**
- Les anciennes clés `mqtt_*` d'un `config.json` existant sont ignorées au chargement (pas d'erreur).

- [ ] **Step 1: Write a failing test** dans `tests/test_config.py` : `test_old_mqtt_keys_ignored` charge un `config.json` contenant `mqtt_host` et vérifie que le chargement réussit.
- [ ] **Step 2: Remove MQTT, add discovery, bump versions, `uv lock`.**
- [ ] **Step 3: Run** `cd ha_menu && uv run pytest -q` (sans pipe vers `tail`) → tous PASS ; `uv run ruff check src/ha_menu/config.py src/ha_menu/web.py src/ha_menu/pipeline.py` → pas de nouvelle erreur ; `grep -rni mqtt src tests` → plus aucune référence utile.
- [ ] **Step 4: `docker build ha_menu`** → succès.
- [ ] **Step 5: Commit** `feat!: v3.0.0, MQTT retiré, découverte par le Supervisor`

### Task 6: Recette sur le vrai HA

- [ ] **Step 1:** Push `main` + tag `v3.0.0`. HA : `ha_manage_app action=check_updates` puis `update` sur `afb7d0ea_ha_menu`. Logs : l'annonce discovery est acceptée.
- [ ] **Step 2:** HACS : ajouter le dépôt personnalisé `cabatech/ha-menu` (catégorie Intégration), installer, redémarrer HA (accord de Christophe pour le redémarrage).
- [ ] **Step 3:** Paramètres → Intégrations : l'intégration est découverte ; confirmer. À défaut, saisie manuelle de l'URL.
- [ ] **Step 4:** Options : Noah → `guichet_unique` ; Lisa → `pronote` (`sensor.pronote_abadie_lisa_today_s_timetable` / `_tomorrow_s_timetable`).
- [ ] **Step 5:** Vérifier les entités (`ha_search ha_menu`), les attributs de `menu_affiche`, l'état des binary_sensors au regard de l'emploi du temps et des inscriptions du jour, et le bouton (une synchro démarre dans les logs).
- [ ] **Step 6:** Avec l'accord de Christophe, ajouter `custom:ha-menu-card` dans la première section des tableaux de bord `chambre-de-noah` et `chambre-de-lisa` ; capture d'écran.

### Task 7: Livraison

- [ ] Skill `livrer-version-vibecoding` : fiches `HA - Menu` (version 3.0.0, ligne de version, limites connues mises à jour : bouton et recalcul à minuit réglés) et `HA - Menu sur HAOS`.
- [ ] Passation de la migration et skill `migrer-app-vers-addon-ha` : ajouter le cas « intégration compagnon + carte ».
- [ ] BACKLOG : passer la tâche Operon `y95axam` (v2.1) à terminée via Operon.
- [ ] Proposer à part la petite version de Guichet Unique (toujours publier les capteurs accueil de loisirs).
