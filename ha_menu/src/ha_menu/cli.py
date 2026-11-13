"""CLI pour ha_menu."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from . import __version__
from .config import AppConfig, ConfigError, load, save
from .ha_calendar import list_ha_events
from .pipeline import discover_children, run_extraction
from .sohappy import SoHappyError


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )


def _load_config_or_exit(path: Path | None) -> AppConfig:
    from .config import DEFAULT_CONFIG_PATH
    p = path or DEFAULT_CONFIG_PATH
    try:
        return load(p)
    except ConfigError as exc:
        Console().print(f"[red]Configuration invalide :[/red] {exc}")
        raise SystemExit(2) from exc


# ------------------------------------------------------------------------
# Sous-commandes
# ------------------------------------------------------------------------


def cmd_sync(args: argparse.Namespace) -> int:
    """Sync ponctuelle : login → menus → agenda HA."""
    _setup_logging(args.verbose)
    console = Console()
    cfg = _load_config_or_exit(args.config)

    try:
        stats = run_extraction(
            cfg,
            publish=not args.no_publish,
            delete_orphans=not args.no_delete,
            target_child_ids=args.child,
        )
    except SoHappyError as exc:
        console.print(f"[red]Échec :[/red] {exc}")
        return 1

    console.rule("[bold green]Sync terminée")
    console.print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if stats.get("ok") else 1


def cmd_discover(args: argparse.Namespace) -> int:
    """Login + récupération des enfants du compte parent."""
    _setup_logging(args.verbose)
    console = Console()
    cfg = _load_config_or_exit(args.config)

    try:
        children = discover_children(cfg)
    except SoHappyError as exc:
        console.print(f"[red]Échec :[/red] {exc}")
        return 1
    save(cfg, args.config or cfg_config_path(args))
    console.rule("[bold green]Découverte OK")
    t = Table(title="Enfants trouvés")
    t.add_column("ID", style="cyan")
    t.add_column("Prénom", style="bold")
    t.add_column("Nom")
    t.add_column("École")
    for c in children:
        t.add_row(str(c.id), c.first_name, c.last_name, c.school)
    console.print(t)
    console.print(
        f"\n[green]{len(children)} enfant(s) — config sauvegardée.[/green]",
    )
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    """Re-fetch d'un enfant (So-Happy verbose=true) — rafraîchit
    school + sohappy_menus. Équivalent du bouton UI « 🔄 Rafraîchir ».

    Si --child n'est pas fourni, rafraîchit tous les enfants actifs.
    """
    from .sohappy import SoHappyClient

    _setup_logging(args.verbose)
    console = Console()
    cfg = _load_config_or_exit(args.config)
    if not cfg.sohappy.is_set():
        console.print("[red]Identifiants So-Happy manquants.[/red]")
        return 1
    targets = cfg.enabled_children()
    if args.child:
        wanted = set(args.child)
        targets = [c for c in targets if c.id in wanted]
        if not targets:
            console.print(f"[red]Aucun enfant actif avec id dans {args.child}.[/red]")
            return 1
    try:
        with SoHappyClient(cfg) as client:
            client.login()
            for child in targets:
                console.print(f"[cyan]Refresh {child.full_name} (id={child.id})…[/cyan]")
                fresh = client.fetch_child_details(child.id)
                child.first_name = fresh.first_name or child.first_name
                child.last_name = fresh.last_name or child.last_name
                child.school = fresh.school or child.school
                child.birth_date = fresh.birth_date or child.birth_date
                child.clients = fresh.clients
                child.sohappy_menus = fresh.sohappy_menus
                console.print(
                    f"  → school={child.school!r}, "
                    f"{len(child.sohappy_menus)} menu(s) So-Happy"
                )
                if child.sohappy_menus:
                    for m in child.sohappy_menus[:5]:
                        console.print(
                            f"    • id={m.get('id')} name={m.get('name')!r} "
                            f"client_id={(m.get('client') or {}).get('id')}"
                        )
    except SoHappyError as exc:
        console.print(f"[red]Échec :[/red] {exc}")
        return 1
    save(cfg, args.config or cfg_config_path(args))
    console.print("[green]Config sauvegardée.[/green]")
    return 0


def cfg_config_path(args: argparse.Namespace) -> Path:
    from .config import DEFAULT_CONFIG_PATH
    return args.config or DEFAULT_CONFIG_PATH


def cmd_check(args: argparse.Namespace) -> int:
    """Vérifie la configuration sans appel réseau."""
    _setup_logging(args.verbose)
    console = Console()
    try:
        cfg = _load_config_or_exit(args.config)
    except SystemExit:
        return 2

    console.print("[green]Configuration valide :[/green]")
    console.print(f"  So-Happy email : {cfg.sohappy.email or '(non configuré)'}")
    console.print(f"  So-Happy password set : {bool(cfg.sohappy.password)}")
    console.print(f"  Enfants total : {len(cfg.children)}")
    console.print(f"  Enfants actifs : {len(cfg.enabled_children())}")
    console.print(f"  Cron : {cfg.sync_cron!r}")
    console.print(f"  Auto-sync : {cfg.auto_sync}")
    console.print(f"  Lookahead : {cfg.sync_lookahead_days} jours")
    console.print(f"  API base : {cfg.api_base}")
    console.print("[green]LLM :[/green]")
    console.print(f"  Activé : {cfg.use_llm_for_menus}")
    console.print(f"  Modèle : {cfg.minimax_model}")
    console.print(f"  Base URL : {cfg.minimax_base_url}")
    console.print(f"  Clé API : {'oui' if cfg.minimax_api_key else 'NON (LLM désactivé)'}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Liste les événements HA - Menu présents dans l'agenda HA (par enfant)."""
    _setup_logging(args.verbose)
    console = Console()
    cfg = _load_config_or_exit(args.config)
    for child in cfg.enabled_children():
        if not child.ha_calendar:
            continue
        try:
            events = list_ha_events(child)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]{child.full_name} :[/red] {exc}")
            continue
        t = Table(title=f"{child.ha_calendar} — {child.full_name}")
        t.add_column("Date")
        t.add_column("Titre")
        t.add_column("UID")
        for ev in sorted(events, key=lambda e: e["start"].get("date", "")):
            t.add_row(ev["start"].get("date", ""), ev.get("summary", ""), ev.get("uid", ""))
        console.print(t)
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Lance l'UI web FastAPI."""
    _setup_logging(args.verbose)
    console = Console()
    try:
        from .web import run_web
    except ImportError as exc:
        console.print(
            f"[red]Dépendances WebUI manquantes :[/red] {exc}\n"
            "[yellow]Installez : uv sync --extra web[/yellow]",
        )
        return 2
    console.print(f"[green]WebUI :[/green] http://{args.host}:{args.port}")
    run_web(host=args.host, port=args.port, config_path=args.config)
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    """Lance le scheduler APScheduler en foreground."""
    _setup_logging(args.verbose)
    console = Console()
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        console.print(
            "[red]APScheduler non installé.[/red] "
            "[yellow]uv sync --extra scheduler[/yellow]",
        )
        return 2

    def job() -> None:
        cfg = _load_config_or_exit(args.config)
        try:
            run_extraction(cfg, publish=True, delete_orphans=False)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Job failed :[/red] {exc}")

    scheduler = BlockingScheduler(timezone="Europe/Paris")
    scheduler.add_job(job, CronTrigger.from_crontab(args.cron))
    console.print(
        f"[green]Démarrage du daemon[/green] — cron : {args.cron!r} "
        f"(fuseau Europe/Paris)",
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        console.print("[yellow]Daemon arrêté.[/yellow]")
    return 0


# ------------------------------------------------------------------------
# Parser
# ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ha-menu",
        description=(
            "Extraction du menu cantine So-Happy vers un agenda Home Assistant"
        ),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Active les logs détaillés (DEBUG)",
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Chemin du fichier de config JSON (défaut : /data/config.json)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="Sync ponctuelle (login + menus + agenda HA)")
    sync.add_argument("--no-publish", action="store_true", help="Ne pas publier dans l'agenda HA")
    sync.add_argument("--no-delete", action="store_true", help="Ne pas supprimer les orphelins")
    sync.add_argument(
        "--child", type=int, action="append", default=None,
        help="ID d'enfant à sync (peut être répété). Défaut : tous les actifs.",
    )
    sync.set_defaults(func=cmd_sync)

    discover = sub.add_parser(
        "discover", help="Login So-Happy + récupération des enfants du compte parent",
    )
    discover.set_defaults(func=cmd_discover)

    refresh = sub.add_parser(
        "refresh",
        help="Re-fetch des détails enfant (So-Happy verbose=true) — rafraîchit sohappy_menus",
    )
    refresh.add_argument(
        "--child", type=int, action="append", default=None,
        help="ID(s) d'enfant à rafraîchir. Défaut : tous les actifs.",
    )
    refresh.set_defaults(func=cmd_refresh)

    check = sub.add_parser("check", help="Vérifier la configuration")
    check.set_defaults(func=cmd_check)

    list_cmd = sub.add_parser(
        "list", help="Lister les événements HA - Menu présents dans l'agenda HA",
    )
    list_cmd.set_defaults(func=cmd_list)

    web = sub.add_parser(
        "web", help="Lance la WebUI FastAPI (status / sync / config / enfants)",
    )
    web.add_argument("--host", default="0.0.0.0")
    web.add_argument("--port", type=int, default=8237)
    web.set_defaults(func=cmd_web)

    daemon = sub.add_parser(
        "daemon", help="Lance le scheduler APScheduler interne",
    )
    daemon.add_argument(
        "--cron", default="0 2 * * *",
        help="Expression cron (défaut : '0 2 * * *')",
    )
    daemon.set_defaults(func=cmd_daemon)

    parser.add_argument(
        "--version", action="version", version=f"ha-menu {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
