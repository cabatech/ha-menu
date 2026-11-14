"""Options : entrée non chargée ou sans enfant → abandon clair, pas d'erreur 500.

Nécessite Home Assistant (venv .superpowers/havenv) ; ignoré sinon.
"""

from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")
from custom_components.ha_menu.config_flow import options_children


def test_entry_not_loaded():
    assert options_children(SimpleNamespace()) == {}


def test_entry_loaded_without_data():
    assert options_children(SimpleNamespace(runtime_data=SimpleNamespace(data=None))) == {}


def test_entry_loaded():
    entry = SimpleNamespace(runtime_data=SimpleNamespace(data={1: {"name": "Paul"}}))
    assert options_children(entry) == {1: {"name": "Paul"}}
