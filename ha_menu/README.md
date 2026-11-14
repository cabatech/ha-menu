# HA - Menu

App Home Assistant qui récupère les menus de cantine du portail
[So-Happy](https://www.so-happy.fr) et les publie dans un agenda Home
Assistant par enfant : un événement « journée entière » par jour de menu,
avec une description courte en 5 lignes (Entrée, Plat, Garnitures, Laitage,
Dessert), reformulée par un LLM (MiniMax) ou par un format local de secours.

## Installation

1. Ajouter ce dépôt dans **Paramètres → Apps → Boutique → Dépôts**.
2. Installer **HA - Menu**, puis le démarrer.
3. Créer un agenda par enfant (intégration **Local Calendar**, par exemple
   « Cantine Prénom » → `calendar.cantine_prenom`).
4. Ouvrir la WebUI (barre latérale **Cantine**) :
   - **Identifiants So-Happy** : e-mail et mot de passe du compte parent, puis
     « Rafraîchir la liste depuis So-Happy » ;
   - **Enfants & agendas** : saisir l'entité agenda de chaque enfant, tester ;
   - **Configuration** : cron de synchro (défaut `0 2 * * *`), LLM.

## Fonctionnement

- Synchro automatique selon le cron (fuseau Europe/Paris), ou manuelle
  depuis le tableau de bord.
- Chaque événement porte une ligne `ha-menu-id: <enfant>-<date>` : seuls
  ces événements sont modifiés. Les autres événements de l'agenda ne sont
  jamais touchés.
- Une modification de menu = suppression puis recréation de l'événement.
- Les jours disparus de So-Happy ne sont supprimés que lors d'un ménage
  explicite (CLI `ha-menu sync`, sans `--no-delete`).
- Entités et carte « Menu du jour » : intégration HA - Menu (voir le README du dépôt).

La configuration est stockée dans `/data/config.json` (sauvegardée avec
l'app).

## Développement

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev,scheduler,web]"
.venv/bin/pytest -ra
```

Hors de Home Assistant, définir `HA_URL` et `HA_TOKEN` (jeton longue durée).
