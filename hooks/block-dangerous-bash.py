#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash) — block clearly destructive shell commands.

Reads the Claude Code hook payload from stdin (JSON with tool_input.command).
If the command matches a narrow denylist of irreversible/destructive patterns,
prints a reason to stderr and exits 2 — which tells Claude Code to block the
tool call. Anything else exits 0 (allow).

Fail-open: on any parse error we allow (exit 0) rather than brick the agent —
this is defense-in-depth, not the only safeguard.
"""
import json
import re
import sys

# Boundary so a pattern only matches when the dangerous command is actually
# *invoked* (start of line, or after ; & | newline backtick `(` ) — not when it
# merely appears inside a quoted string (git commit message, echo, grep, docs).
B = r"(?:^|[\n;&|`(])\s*"

# Narrow, high-confidence destructive patterns (case-insensitive). Command-style
# ones are boundary-anchored; the rest are inherently inside-arg so stay loose.
DENY = [
    B + r"(?:sudo\s+)?rm\s+-[a-z]*r[a-z]*f[a-z]*\s+(?:/|~|/\*|\$home|--no-preserve-root)",
    B + r"(?:sudo\s+)?mkfs\b",        # format filesystem
    B + r"(?:sudo\s+)?dd\b[^\n;&|]*\bof=/dev/",  # dd onto a device
    r">\s*/dev/(?:sd|disk|hd|nvme)",  # overwrite a raw disk
    r"\bdrop\s+(?:table|database)\b",  # destructive SQL (lives inside a quoted arg)
]
FORK_BOMB = ":(){:|:&};:"


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # fail-open
    cmd = (data.get("tool_input") or {}).get("command", "") or ""
    low = cmd.lower()

    if FORK_BOMB in cmd.replace(" ", ""):
        sys.stderr.write("BLOCKED by block-dangerous-bash: fork bomb\n")
        return 2
    for pat in DENY:
        if re.search(pat, low):
            sys.stderr.write(
                f"BLOCKED by block-dangerous-bash: command matches a forbidden "
                f"destructive pattern (/{pat}/). If this is intentional, run it "
                f"yourself in a terminal.\n"
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
