"""Tests pour ha_menu.cli — construction du parser."""

from __future__ import annotations

import pytest

from ha_menu.cli import build_parser


def test_parser_help_exits():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])


def test_parser_required_command():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_sync_command():
    parser = build_parser()
    args = parser.parse_args(["sync", "--no-publish", "--no-delete", "--child", "42"])
    assert args.command == "sync"
    assert args.no_publish is True
    assert args.no_delete is True
    assert args.child == [42]


def test_parser_web_command():
    parser = build_parser()
    args = parser.parse_args(["web", "--host", "127.0.0.1", "--port", "9000"])
    assert args.command == "web"
    assert args.host == "127.0.0.1"
    assert args.port == 9000


def test_parser_version():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--version"])


def test_parser_daemon_default_cron():
    parser = build_parser()
    args = parser.parse_args(["daemon"])
    assert args.command == "daemon"
    assert args.cron == "0 2 * * *"
