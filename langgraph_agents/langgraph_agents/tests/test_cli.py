# Copyright (C) 2026 DeMoD LLC
# SPDX-License-Identifier: LGPL-3.0-only
"""Tests for the CLI."""
import sys
from unittest.mock import patch, MagicMock

from langgraph_agents.cli import build_parser, main, _load_config


def test_parser_has_all_subcommands():
    parser = build_parser()
    actions = [a for a in parser._actions if hasattr(a, 'choices') and a.choices]
    cmds = set()
    for a in actions:
        cmds.update(a.choices.keys())
    for expected in ("agents", "backends", "chat", "run", "status", "tui"):
        assert expected in cmds, f"missing subcommand: {expected}"


def test_load_config_parses_jsonc():
    """Config loader should strip comments and parse JSONC."""
    import tempfile, os
    jsonc = '''{
      // a comment
      "agents": [
        {"name": "test", "llm_backend": "echo", "channel": "test"}
      ],
    }'''
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonc', delete=False) as f:
        f.write(jsonc)
        f.flush()
        config = _load_config(f.name)
    os.unlink(f.name)
    assert config["agents"][0]["name"] == "test"


def test_cmd_backends_lists_glm5p2(capsys):
    """backends subcommand should list glm5p2."""
    main(["backends"])
    captured = capsys.readouterr()
    assert "glm5p2" in captured.out


def test_cmd_chat_echo_backend(capsys):
    """chat with echo backend should return the message."""
    main(["chat", "--backend", "echo", "hello from test"])
    captured = capsys.readouterr()
    assert "hello from test" in captured.out


def test_cmd_chat_coordinator_graph(capsys):
    """chat with coordinator graph should route echo: prefix."""
    main(["chat", "--graph", "coordinator", "--backend", "echo", "echo: coordinator test"])
    captured = capsys.readouterr()
    assert "coordinator test" in captured.out


def test_cmd_agents_lists_all(capsys, tmp_path):
    """agents subcommand should list agents from config."""
    config = tmp_path / "agents.jsonc"
    config.write_text('''{
      "agents": [
        {"name": "echo-agent", "graph": "base", "llm_backend": "echo", "channel": "echo"},
        {"name": "glm5p2-agent", "graph": "coordinator", "llm_backend": "glm5p2", "channel": "glm"}
      ]
    }''')
    main(["agents", "--config", str(config)])
    captured = capsys.readouterr()
    assert "echo-agent" in captured.out
    assert "glm5p2-agent" in captured.out


def test_cmd_status_connection_failure(capsys):
    """status with unreachable mesh should exit non-zero."""
    rc = main(["status", "--mesh-url", "http://127.0.0.1:9999"])
    assert rc == 1


def test_sierpinski_depth_1():
    """depth=1 should produce a single triangle."""
    from langgraph_agents.cli import _sierpinski
    result = _sierpinski(depth=1)
    assert "▲" in result


def test_sierpinski_grows_with_depth():
    """Deeper recursion should produce more lines."""
    from langgraph_agents.cli import _sierpinski
    shallow = len(_sierpinski(depth=1).split("\n"))
    deep = len(_sierpinski(depth=3).split("\n"))
    assert deep > shallow


def test_banner_shown_on_every_command(capsys):
    """The Sierpinski banner should appear before command output."""
    main(["backends"])
    captured = capsys.readouterr()
    assert "▲" in captured.out
    assert "Punctim" in captured.out
