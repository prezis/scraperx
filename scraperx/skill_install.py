"""skill_install — ship the scraperx Agent Skill with the package and offer it to Claude Code.

The skill (``scraperx/skills/scraperx/SKILL.md``) tells an agent which scraperx command answers which question. It
reaches Claude Code in one of two ways, and scraperx detects both:

* ``scraperx skill install``: copies the packaged SKILL.md to ``~/.claude/skills/scraperx/`` (personal) or
  ``<project>/.claude/skills/scraperx/`` (``--project``). Run it again after upgrading scraperx.
* the Claude Code plugin: this repository is also a plugin marketplace
  (``claude plugin marketplace add prezis/scraperx`` then ``claude plugin install scraperx@scraperx``); the plugin
  loads the same SKILL.md from the package directory.

When Claude Code is present (``~/.claude`` exists) and the skill is missing or older than the installed scraperx, the
CLI prints one hint line on stderr, at most once a day (``SCRAPERX_NO_SKILL_HINT=1`` silences it).
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["SkillStatus", "install_skill", "maybe_hint", "packaged_skill_path", "skill_status"]

SKILL_NAME = "scraperx"
_HINT_ENV = "SCRAPERX_NO_SKILL_HINT"
_HINT_EVERY_S = 86400.0


def packaged_skill_path() -> Path:
    """The SKILL.md shipped inside the installed scraperx package."""
    return Path(__file__).resolve().parent / "skills" / SKILL_NAME / "SKILL.md"


def _claude_home(home: Path | None = None) -> Path:
    """Claude Code's config directory: CLAUDE_CONFIG_DIR when set (and no explicit home), else ~/.claude."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env and home is None:
        return Path(env).expanduser()
    return (home or Path.home()) / ".claude"


@dataclass
class SkillStatus:
    """Where Claude Code would load the scraperx skill from, and whether it matches this scraperx.

    Attributes:
        claude_code:  ~/.claude exists (Claude Code has run on this machine).
        installed:    "personal", "plugin", "project" or "" (not found).
        path:         The SKILL.md found, "" when none.
        up_to_date:   The found SKILL.md is byte-identical to the packaged one.
    """

    claude_code: bool
    installed: str
    path: str
    up_to_date: bool

    @property
    def needs_action(self) -> bool:
        return self.claude_code and (not self.installed or not self.up_to_date)


def _plugin_copies(claude: Path) -> list[Path]:
    plugins = claude / "plugins"
    if not plugins.is_dir():
        return []
    return sorted(plugins.glob(f"**/scraperx/skills/{SKILL_NAME}/SKILL.md"))


def skill_status(*, home: Path | None = None, project: Path | None = None) -> SkillStatus:
    """Find the scraperx skill Claude Code would load (project, then personal, then plugin) and compare it."""
    claude = _claude_home(home)
    packaged = packaged_skill_path().read_bytes()
    candidates: list[tuple[str, Path]] = []
    if project is not None:
        candidates.append(("project", Path(project) / ".claude" / "skills" / SKILL_NAME / "SKILL.md"))
    candidates.append(("personal", claude / "skills" / SKILL_NAME / "SKILL.md"))
    candidates.extend(("plugin", p) for p in _plugin_copies(claude))
    found = [(kind, p) for kind, p in candidates if p.is_file()]
    for kind, p in found:
        if p.read_bytes() == packaged:
            return SkillStatus(claude.is_dir(), kind, str(p), True)
    if found:
        kind, p = found[0]
        return SkillStatus(claude.is_dir(), kind, str(p), False)
    return SkillStatus(claude.is_dir(), "", "", False)


def install_skill(*, home: Path | None = None, project: Path | None = None) -> Path:
    """Copy the packaged SKILL.md to ~/.claude/skills/scraperx/ (or <project>/.claude/skills/scraperx/).
    Overwrites an older copy; returns the written path."""
    base = Path(project) / ".claude" if project is not None else _claude_home(home)
    target = base / "skills" / SKILL_NAME / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_bytes(packaged_skill_path().read_bytes())
    os.replace(tmp, target)
    return target


def maybe_hint(*, home: Path | None = None, stream=None, now: float | None = None) -> bool:
    """Print one stderr line proposing the install when Claude Code has no current scraperx skill. At most once per
    day (stamp in ~/.scraperx/skill-hint), never with SCRAPERX_NO_SKILL_HINT=1. Never raises. True if printed."""
    try:
        if os.environ.get(_HINT_ENV):
            return False
        status = skill_status(home=home)
        if not status.needs_action:
            return False
        stamp = (home or Path.home()) / ".scraperx" / "skill-hint"
        t = time.time() if now is None else now
        if stamp.is_file() and t - stamp.stat().st_mtime < _HINT_EVERY_S:
            return False
        what = "is out of date" if status.installed else "is not installed"
        print(
            f"scraperx: Claude Code is on this machine but the scraperx skill {what}; "
            "`scraperx skill install` adds it (or install the plugin: "
            "`claude plugin marketplace add prezis/scraperx && claude plugin install scraperx@scraperx`). "
            f"Silence: {_HINT_ENV}=1",
            file=stream or sys.stderr,
        )
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp, (t, t))
        return True
    except Exception:  # a hint must never break a scrape
        return False
