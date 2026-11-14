"""Noms d'entités : l'appareil s'appelle « Cantine <enfant> », pas de répétition."""

import json
from pathlib import Path

_DIR = Path(__file__).parents[2] / "custom_components" / "ha_menu"


def test_entity_names_do_not_repeat_device_name():
    for f in ("strings.json", "translations/fr.json", "translations/en.json"):
        entities = json.loads((_DIR / f).read_text())["entity"]
        names = [v["name"] for plat in entities.values() for v in plat.values()]
        assert not any(n.lower().startswith("cantine") for n in names), (f, names)
