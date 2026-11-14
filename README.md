# HA - Menu

Dépôt d'apps Home Assistant contenant **HA - Menu** : menus de cantine
So-Happy vers un agenda Home Assistant par enfant.

Voir [ha_menu/README.md](ha_menu/README.md).

## Intégration HA - Menu (entités et carte)

L'intégration `custom_components/ha_menu/` complète l'app :

- un appareil par enfant : « Menu affiché » (menu de midi jusqu'à 21 h, puis
  celui du lendemain), « Cantine aujourd'hui », « Cantine demain » ;
- un bouton « Synchroniser » ;
- la carte `custom:ha-menu-card` (« Menu du jour »).

Installation : HACS → Dépôts personnalisés → ce dépôt (catégorie Intégration),
puis redémarrer HA. L'app s'annonce : accepter l'intégration proposée dans
Paramètres → Intégrations.

Réglages par enfant (Configurer) : où vérifier que l'enfant mange à la cantine.

- **Guichet Unique** : inscrit à la cantine ou à l'accueil de loisirs ce jour-là.
- **Pronote** : au moins un cours le matin et un l'après-midi, non annulés.
- **Aucune** : un menu présent suffit.

Si la source ne répond pas, la carte affiche le menu avec « inscription non vérifiée ».

