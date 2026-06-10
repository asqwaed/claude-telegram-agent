#!/usr/bin/env python3
"""PreToolUse hook (matcher: Read|Edit|Write|Bash) — block access to secrets.

Reads the Claude Code hook payload from stdin. For file tools it inspects
tool_input.file_path; for Bash it scans tool_input.command. If the target
touches a secret/credential file, prints a reason to stderr and exits 2 (block).

Protects: .env (but not .env.example/.sample), *.session, *.key, *.token, and
the credentials/ dir (except the auth helper code + README). Reading these into
the model's context risks leaking them into replies, so reads are blocked too.

Fail-open on parse errors (exit 0).
"""
import json
import re
import sys

# Patterns that identify a secret target (case-insensitive).
SECRET = [
    r"\.env(?![.\w])",          # .env, not .env.example / .env.sample
    r"\.session\b",
    r"\.key\b",
    r"\.token\b",
    r"\btoken\.json\b",
    r"(^|/)credentials/",       # the credentials dir
]
# Allowed exceptions inside credentials/ (tracked, non-secret code).
ALLOW = [
    r"credentials/[A-Za-z_]*auth\.py\b",
    r"credentials/README",
]


def is_secret(target: str) -> bool:
    low = target.lower()
    if any(re.search(a, low) for a in ALLOW):
        # Only exempt when the match is purely an allowed file (no other secret
        # token elsewhere in the string).
        stripped = low
        for a in ALLOW:
            stripped = re.sub(a, "", stripped)
        return any(re.search(s, stripped) for s in SECRET)
    return any(re.search(s, low) for s in SECRET)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    ti = data.get("tool_input") or {}
    target = ti.get("file_path") or ti.get("command") or ""
    if target and is_secret(target):
        sys.stderr.write(
            "BLOCKED by protect-secrets: this touches a secret/credential file "
            "(.env, *.session, *.key, *.token, credentials/). Never read or edit "
            "secrets; never echo them into a reply.\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
