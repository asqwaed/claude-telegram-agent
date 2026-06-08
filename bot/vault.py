"""File-based helpers for the Obsidian vault.

The agent manipulates the vault via the ``obs_*`` MCP tools; these helpers back
the deterministic slash-commands (/note, /today, /find, /profile) so they run
instantly without an LLM round-trip.
"""

from datetime import datetime
from pathlib import Path

import config


def capture(text: str) -> Path:
    """Quick-capture text into the Inbox as a timestamped note. Returns rel path."""
    now = datetime.now()
    path = config.INBOX_DIR / (now.strftime("%Y-%m-%d_%H%M%S") + ".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ncreated: {now.isoformat(timespec='seconds')}\n---\n{text}\n",
        encoding="utf-8",
    )
    return path.relative_to(config.VAULT_DIR)


def read_today() -> str:
    """Return today's daily note content, or '' if it doesn't exist."""
    path = config.JOURNAL_DIR / f"{datetime.now():%Y-%m-%d}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def append_today(text: str) -> Path:
    """Append a timestamped line to today's daily note. Returns rel path."""
    today = f"{datetime.now():%Y-%m-%d}"
    path = config.JOURNAL_DIR / f"{today}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# {today}\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"- {datetime.now():%H:%M} {text}\n")
    return path.relative_to(config.VAULT_DIR)


def search(query: str, limit: int = 20) -> list[str]:
    """Full-text search across the vault. Returns 'path:line: snippet' strings."""
    q = query.lower()
    hits: list[str] = []
    for p in sorted(config.VAULT_DIR.rglob("*.md")):
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if q in line.lower():
                rel = p.relative_to(config.VAULT_DIR)
                hits.append(f"{rel}:{i}: {line.strip()[:120]}")
                if len(hits) >= limit:
                    return hits
    return hits


def read_profile() -> str:
    """Return the always-loaded profile note, or '' if missing."""
    return (
        config.PROFILE_PATH.read_text(encoding="utf-8")
        if config.PROFILE_PATH.exists()
        else ""
    )
