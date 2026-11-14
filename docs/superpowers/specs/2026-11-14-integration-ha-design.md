# Intégration Home Assistant « HA - Menu » — conception

Date : 2026-11-14. Statut : à valider par Christophe.

## Intention

L'app HA - Menu (add-on `afb7d0ea_ha_menu`) récupère les menus de cantine sur So-Happy et remplit un agenda HA par enfant. Christophe veut en plus une **vraie intégration HA**, sur le modèle de TaskMate déjà utilisé dans les tableaux de bord des enfants : visible dans Paramètres → Intégrations, avec un appareil par enfant, des entités propres et une **carte « Menu du jour »** à poser sur les tableaux de bord `chambre-de-noah` et `chambre-de-lisa`.

L'intégration **dialogue avec l'app sans la remplacer** : l'app garde So-Happy, le LLM, le cache et les agendas.

## Objectifs

1. Un appareil HA par enfant (« Cantine Noah », « Cantine Lisa »).
2. Entités par enfant :
   - `sensor.<enfant>_menu_affiche` : le menu du jour jusqu'à 21 h, puis celui du lendemain. État = libellé court (ex. « Lundi 16 nov. »), attributs : `date`, `jour` (`aujourd_hui` / `demain`), `entree`, `plat`, `garnitures`, `laitage`, `dessert`, `cantine` (bool, cf. 3).
   - `binary_sensor.<enfant>_cantine_aujourdhui` et `binary_sensor.<enfant>_cantine_demain`.
   - `button.<enfant>_synchroniser` (synchro de l'app ; un seul bouton global suffit s'il est plus simple, cf. « Laissé au builder »).
3. Règle « cantine » d'un jour J pour un enfant :
   - il faut **un menu So-Happy pour J**, **et**
   - selon la source d'inscription choisie pour l'enfant :
     - **Guichet Unique** (Noah) : inscrit à la cantine **ou** à l'accueil de loisirs ce jour-là (le repas est compris dans l'accueil de loisirs : hypothèse de Christophe, à garder modifiable) ;
     - **Pronote** (Lisa, collège) : au moins un cours non annulé le matin **et** au moins un cours non annulé l'après-midi (`is_morning`, `is_afternoon`, `canceled` des `lessons`) ;
     - **aucune** : menu présent = cantine.
   - Des menus existent aussi le mercredi et pendant les vacances (accueil de loisirs) : la présence d'un menu ne suffit donc jamais quand une source est configurée.
4. Carte Lovelace `custom:ha-menu-card` livrée par l'intégration (pas de dépôt HACS séparé pour la carte), configurable par `entity: sensor.<enfant>_menu_affiche`. Affiche le titre (« Menu de midi » ou « Menu de demain »), la date et les 5 lignes, ou un état vide lisible (« Pas de cantine aujourd'hui / demain »). Style cohérent avec les tableaux de bord existants (sections, thèmes Frosted Glass / Mushroom), sans dépendre de mushroom.
5. Bascule de 21 h et changement de jour à minuit recalculés par l'intégration elle-même (pas seulement à la synchro de l'app).

## Non-objectifs

- Remplacer l'app ou déplacer la logique So-Happy / LLM dans l'intégration.
- Rendre Guichet Unique multi-enfants (il ne couvre que Noah).
- Notifications (pourront s'appuyer plus tard sur les entités).
- Publication dans HACS « par défaut » : dépôt personnalisé suffit.

## Anti-objectifs

- Doublons d'entités : les capteurs MQTT de l'app sont **retirés** (code `mqtt_publisher.py`, réglages MQTT de la WebUI) dans la même version. Aucun appareil MQTT `ha_menu` n'existe aujourd'hui dans HA.
- Une carte qui affiche un menu alors que l'enfant ne mange pas à la cantine.
- Un secret à saisir ou à stocker pour relier l'intégration à l'app.

## Contraintes et existant vérifié

- **App** : FastAPI, port 8237, pas de port exposé sur l'hôte. Le middleware laisse passer sans authentification les IP privées (dont le réseau interne des add-ons `172.30.32.0/23`). API existante : `GET /api/menus/{child_id}` (cache JSON des jours), `GET /api/menus`, `POST /api/sync`, `GET /api/status`. Nom d'hôte interne de l'add-on : `afb7d0ea-ha-menu` (à confirmer à l'implémentation).
- **Guichet Unique** (add-on `cabatech/ha-guichet-unique`, Noah seul) : entités MQTT globales `binary_sensor.guichet_cantine_aujourdhui` / `_demain`, etc. Un capteur n'existe que pour les services présents dans les inscriptions : **pas d'entité accueil de loisirs aujourd'hui**. Changement requis côté Guichet Unique : toujours publier `binary_sensor.guichet_accueil_loisirs_aujourdhui` / `_demain` (dépôt distinct, petite version).
- **Pronote** (intégration HACS « Pronote for Home Assistant ») : `sensor.pronote_abadie_lisa_today_s_timetable` et `sensor.pronote_abadie_lisa_tomorrow_s_timetable`, attribut `lessons` (liste avec `start_at`, `canceled`, `is_morning`, `is_afternoon`). Attention : `next_day_s_timetable` = prochain jour de cours, pas demain.
- Dépôt `cabatech/ha-menu` public, contient déjà `repository.yaml` (dépôt d'add-ons) ; HACS accepte `custom_components/` + `hacs.json` à la racine du même dépôt.
- Fuseau Europe/Paris (celui de HA).

## Approches

| Approche | Pour | Contre |
|---|---|---|
| **A. Intégration HACS dans le même dépôt (retenue)** : `custom_components/ha_menu/`, coordinateur qui interroge l'API de l'app, config flow, carte JS servie par l'intégration | Visible dans Intégrations, appareils propres, carte installée avec, aucune config dans la WebUI pour HA | Deux composants à versionner ensemble |
| B. Améliorer les entités MQTT de l'app + carte en YAML | Moins de code | Pas d'intégration, logique « cantine » côté app avec lecture des états HA, ne répond pas à la demande |
| C. Intégration qui remplace l'app | Un seul composant | Écartée par Christophe |

Christophe a choisi une intégration qui dialogue avec l'app ; il a délégué le reste (« go pour le reste »).

## Décisions

- **Découverte** : l'add-on s'annonce au Supervisor (`discovery: [ha_menu]` dans `config.yaml`, POST `/discovery` au démarrage avec `host`/`port`) ; le config flow accepte `async_step_hassio` et propose aussi une saisie manuelle de l'URL. Pas de jeton (réseau interne).
- **Enfants** : liste lue depuis l'app (`/api/menus` + config). Options flow : pour chaque enfant, source d'inscription (`aucune` / `guichet_unique` / `pronote`) et les entités correspondantes (sélecteurs d'entités, préremplis quand l'entité attendue existe).
- **Rafraîchissement** : coordinateur toutes les 30 min sur l'API de l'app ; recalcul local (sans appel réseau) à 21:00, 00:00 et à chaque changement d'état des entités d'inscription liées.
- **Bouton Synchroniser** : `POST /api/sync` puis rafraîchissement du coordinateur une fois la synchro terminée (`/api/status`).
- **Carte** : JS sans build (Lit chargé depuis l'élément `ha-panel-lovelace` ou web component nu), enregistrée via `frontend.add_extra_js_url` ; éditeur visuel minimal (choix de l'entité).
- **MQTT** retiré de l'app (v3.0.0, rupture).
- Versions : app et intégration partagent le numéro de version.

## Comportement observé des sources (historique HA, 11 au 14 nov.)

- Pronote : `today_s_timetable` et `tomorrow_s_timetable` basculent seules entre 00:00 et 00:08. Passages ponctuels en `unavailable` (ex. 12 nov. 23:00, 15 min).
- Guichet Unique : `cantine_aujourdhui` recalculé à 00:01 et à chaque synchro (≈ 07:00) ; les capteurs passent quelques secondes en `unknown` au redémarrage de l'add-on. `_demain` est calculé par date calendaire : à 21:00, il vise déjà le bon jour.
- Le recalcul sur changement d'état des entités liées couvre donc le léger retard de Pronote après minuit.
- Aucune entité MQTT `ha_menu` n'existe dans HA (recherche du 2026-11-14) : retrait du MQTT sans migration.

## Source indisponible (décision de Christophe)

Si l'état d'inscription ne peut pas être établi (entité Pronote ou Guichet Unique `unavailable`/`unknown`), la carte affiche **le menu avec la mention « inscription non vérifiée »**. Les `binary_sensor` cantine valent alors `unknown`. Une panne passagère ne vide pas la carte. Si l'app est injoignable, l'intégration garde les dernières données du coordinateur (entités marquées indisponibles seulement après plusieurs échecs, au choix du builder).

## Appels du rédacteur (à vérifier par Christophe)

- Repas inclus dans l'accueil de loisirs : option par enfant dans l'options flow (cochée par défaut), visible seulement pour la source Guichet Unique.
- Entité accueil de loisirs de Guichet Unique absente (avant la petite version de Guichet Unique) : traitée comme « pas inscrit à ce service » ; la cantine seule reste prise en compte. L'intégration peut donc sortir en premier.
- Joignabilité de l'add-on depuis HA Core par son nom d'hôte : déjà prouvée par EMQX (`a0d7b954-emqx`) ; à confirmer en recette. Droits Supervisor pour `/discovery` vérifiés à l'implémentation.

## Laissé au builder

- Format exact de l'état de `menu_affiche` et des identifiants d'entités (stables, en français, sans prénom en dur dans le code).
- Un bouton par enfant ou un seul global (l'app synchronise tous les enfants d'un coup : un bouton global attaché à un appareil « HA - Menu » est acceptable).
- Mise en page de la carte, dans le respect du point 4.
- Tests : logique « cantine » et bascule 21 h en tests unitaires purs (sans HA) ; config flow avec `pytest-homeassistant-custom-component` si le coût reste raisonnable.
