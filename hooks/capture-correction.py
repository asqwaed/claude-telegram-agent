#!/usr/bin/env python3
"""UserPromptSubmit hook — capture correction episodes for self-learning.

Reads the Claude Code hook payload from stdin (JSON with `prompt`). If the user
message looks like a correction ("не надо", "неправильно", "wrong", ...), appends
one episode to memory/episodes.jsonl. This is the *capture* half of the
self-learning loop; a later scoring/promotion pass turns repeated episodes into
durable rules.

NEVER blocks and NEVER writes to stdout (stdout here would be injected into the
model's context). Pure side-effect + exit 0. Fail-silent on any error.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

EPISODES = Path(__file__).resolve().parent.parent / "memory" / "episodes.jsonl"

# Correction signals (lowercase substring match). Moderate set to limit noise.
SIGNALS = [
    "не надо", "неправильно", "не так", "не то что", "я же просил",
    "я же говорил", "опять ты", "хватит", "перестань", "зачем ты",
    "wrong", "that's not", "not what i", "stop doing", "again you",
    "don't do that",
]


def main() -> int:
    try:
        data = json.load(sys.stdin)
        prompt = (data.get("prompt") or "").strip()
    except Exception:
        return 0
    if not prompt:
        return 0
    low = prompt.lower()
    matched = next((s for s in SIGNALS if s in low), None)
    if not matched:
        return 0
    episode = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": "correction",
        "signal": matched,
        "prompt": prompt[:500],
        "session_id": data.get("session_id", ""),
        "tags": [],
        "status": "new",
        "freq": 1,
    }
    try:
        EPISODES.parent.mkdir(parents=True, exist_ok=True)
        with EPISODES.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(episode, ensure_ascii=False) + "\n")
    except Exception:
        pass  # fail-silent — capture must never disrupt the turn
    return 0


if __name__ == "__main__":
    sys.exit(main())
