"""The scraperx Claude Code skill is part of the product: shipped in the package, installable by the CLI, loaded by the
plugin manifest, proposed when missing, and in step with the CLI it describes. All against a tmp home."""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest

import scraperx
from scraperx import skill_install as si

REPO = Path(__file__).resolve().parent.parent
SKILL = si.packaged_skill_path()


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md must open with a --- frontmatter block"
    return dict(line.split(": ", 1) for line in m.group(1).splitlines() if ": " in line)


def test_packaged_skill_is_well_formed():
    text = SKILL.read_text(encoding="utf-8")
    fm = _frontmatter(text)
    assert fm["name"] == "scraperx"
    assert 100 < len(fm["description"]) <= 1024
    assert "/home/" not in text and "~/ai/" not in text and "global-graph" not in text  # no machine-local paths


def test_skill_ships_as_package_data():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert '"skills/*/SKILL.md"' in pyproject
    assert SKILL == REPO / "scraperx" / "skills" / "scraperx" / "SKILL.md"


def test_plugin_and_marketplace_point_at_the_packaged_skill():
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert plugin["name"] == "scraperx" and plugin["skills"] == ["./scraperx/skills/"]
    assert (REPO / plugin["skills"][0] / "scraperx" / "SKILL.md").is_file()
    assert market["plugins"][0]["name"] == "scraperx" and market["plugins"][0]["source"] == "./"
    assert "version" not in market["plugins"][0]  # one version, in plugin.json


def test_versions_agree():
    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert plugin["version"] == scraperx.__version__
    assert f'version = "{scraperx.__version__}"' in pyproject


def test_install_then_status(tmp_path):
    home = tmp_path
    (home / ".claude").mkdir()
    assert si.skill_status(home=home).needs_action
    target = si.install_skill(home=home)
    assert target == home / ".claude" / "skills" / "scraperx" / "SKILL.md"
    assert target.read_bytes() == SKILL.read_bytes()
    st = si.skill_status(home=home)
    assert (st.installed, st.up_to_date, st.needs_action) == ("personal", True, False)
    target.write_text("old skill", encoding="utf-8")
    st = si.skill_status(home=home)
    assert (st.installed, st.up_to_date, st.needs_action) == ("personal", False, True)


def test_plugin_copy_and_project_install_are_found(tmp_path):
    home = tmp_path / "home"
    plugin_skill = home / ".claude" / "plugins" / "cache" / "scraperx" / "scraperx" / "1.13.0" / "scraperx" / "skills"
    (plugin_skill / "scraperx").mkdir(parents=True)
    (plugin_skill / "scraperx" / "SKILL.md").write_bytes(SKILL.read_bytes())
    assert si.skill_status(home=home).installed == "plugin"
    proj = tmp_path / "proj"
    si.install_skill(project=proj)
    assert si.skill_status(home=home, project=proj).installed == "project"


def test_claude_config_dir_is_honoured(tmp_path, monkeypatch):
    cfg = tmp_path / "cc"
    cached = cfg / "plugins" / "cache" / "scraperx" / "scraperx" / "1.13.0" / "scraperx" / "skills" / "scraperx"
    cached.mkdir(parents=True)
    (cached / "SKILL.md").write_bytes(SKILL.read_bytes())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    st = si.skill_status()
    assert (st.claude_code, st.installed, st.up_to_date) == (True, "plugin", True)
    assert si.install_skill() == cfg / "skills" / "scraperx" / "SKILL.md"


def test_hint_once_a_day_and_silenceable(tmp_path, monkeypatch):
    monkeypatch.delenv("SCRAPERX_NO_SKILL_HINT", raising=False)
    home = tmp_path
    out = io.StringIO()
    assert not si.maybe_hint(home=home, stream=out)  # no Claude Code: nothing to propose
    (home / ".claude").mkdir()
    assert si.maybe_hint(home=home, stream=out, now=1_000_000.0)
    assert "scraperx skill install" in out.getvalue() and "not installed" in out.getvalue()
    assert not si.maybe_hint(home=home, stream=out, now=1_000_000.0 + 3600)  # same day
    assert si.maybe_hint(home=home, stream=out, now=1_000_000.0 + 90000)  # next day
    si.install_skill(home=home)
    assert not si.maybe_hint(home=home, stream=out, now=1_000_000.0 + 200000)  # current: quiet
    monkeypatch.setenv("SCRAPERX_NO_SKILL_HINT", "1")
    (home / ".claude" / "skills" / "scraperx" / "SKILL.md").write_text("old", encoding="utf-8")
    assert not si.maybe_hint(home=home, stream=out, now=1_000_000.0 + 400000)


def _cli_help(argv: list[str], capsys) -> str:
    from scraperx import __main__ as cli

    old = sys.argv
    sys.argv = ["scraperx", *argv, "--help"]
    try:
        with pytest.raises(SystemExit) as ei:
            cli.main()
        assert ei.value.code == 0
    finally:
        sys.argv = old
    return capsys.readouterr().out


def test_every_command_the_skill_names_exists(capsys):
    """The skill must not promise a command the CLI does not have."""
    text = SKILL.read_text(encoding="utf-8")
    pairs = set(re.findall(r"`scraperx (x|reddit|telegram|bluesky|skill) ([a-z]+)", text))
    pairs |= {(a, b) for a, b in re.findall(r"`(telegram|reddit|bluesky|x) ([a-z]+) ", text)}
    assert ("x", "user") in pairs and ("reddit", "comments") in pairs and ("telegram", "posts") in pairs
    assert ("bluesky", "search") in pairs and ("bluesky", "thread") in pairs
    for group, action in sorted(pairs):
        assert action in _cli_help([group], capsys), f"skill names `scraperx {group} {action}`, the CLI lacks it"
    for top in ("search", "doctor", "github"):
        assert re.search(rf"`scraperx {top}\b", text)


def test_cli_skill_install_and_status(tmp_path, monkeypatch, capsys):
    from scraperx import __main__ as cli

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(sys, "argv", ["scraperx", "skill", "install"])
    cli.main()
    assert (tmp_path / ".claude" / "skills" / "scraperx" / "SKILL.md").is_file()
    monkeypatch.setattr(sys, "argv", ["scraperx", "skill", "status"])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 0 and "installed (personal), current" in capsys.readouterr().out
