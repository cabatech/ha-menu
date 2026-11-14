"""Constantes de l'intégration HA - Menu."""

from datetime import timedelta

DOMAIN = "ha_menu"
VERSION = "3.0.0"
DEFAULT_URL = "http://afb7d0ea-ha-menu:8237"
DEFAULT_PORT = 8237
SCAN_INTERVAL = timedelta(minutes=30)
MAX_FAILURES = 3

CONF_URL = "url"
CONF_CHILDREN = "children"
CONF_CHILD = "child"
CONF_SOURCE = "source"
CONF_CANTINE_TODAY = "cantine_today"
CONF_CANTINE_TOMORROW = "cantine_tomorrow"
CONF_LOISIRS_TODAY = "loisirs_today"
CONF_LOISIRS_TOMORROW = "loisirs_tomorrow"
CONF_TIMETABLE_TODAY = "timetable_today"
CONF_TIMETABLE_TOMORROW = "timetable_tomorrow"
CONF_REPAS_LOISIRS = "repas_loisirs"

SOURCE_NONE = "aucune"
SOURCE_GUICHET = "guichet_unique"
SOURCE_PRONOTE = "pronote"
SOURCES = (SOURCE_NONE, SOURCE_GUICHET, SOURCE_PRONOTE)
