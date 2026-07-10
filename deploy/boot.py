#!/usr/bin/env python3
"""Crash-safe boot wrapper for the Telegram bot.

launchd runs *this* (not ``main.py`` directly). It runs a tiny, stdlib-only
rollback guard and then hands off to the real app. Keeping it dependency-free
matters: if a bad self-edit breaks an import anywhere in the app, this wrapper
still runs and can recover.

Guard logic:
  * every boot increments ``.selfupdate/boot_attempts``;
  * once the app is actually up it resets that counter to 0 (see
    ``bot.selfupdate.mark_healthy``);
  * so a counter that keeps climbing means the app can't start — after
    ``MAX_ATTEMPTS`` consecutive failed boots we ``git reset --hard`` to the
    last known-good commit and leave a marker the recovered app reports.

With launchd ``KeepAlive`` + ``ThrottleInterval`` 10s, a broken edit self-heals
in ~30s instead of bricking the bot.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / ".selfupdate"
ATTEMPTS = STATE / "boot_attempts"
LAST_GOOD = STATE / "last_good"
ROLLED_BACK = STATE / "rolled_back.json"
MAX_ATTEMPTS = 3


def _git(*args):
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, timeout=30,
    )


def _read_int(path):
    try:
        return int(path.read_text().strip() or "0")
    except (OSError, ValueError):
        return 0


def guard():
    """Increment the boot counter; roll back if we're stuck crash-looping."""
    STATE.mkdir(exist_ok=True)
    attempts = _read_int(ATTEMPTS) + 1
    ATTEMPTS.write_text(str(attempts))

    if attempts <= MAX_ATTEMPTS or not LAST_GOOD.exists():
        return

    good = LAST_GOOD.read_text().strip()
    head = (_git("rev-parse", "HEAD").stdout or "").strip()
    if not good or not head or good == head:
        return

    # The current revision can't boot — revert to the last known-good commit.
    _git("reset", "--hard", good)
    ROLLED_BACK.write_text(json.dumps({
        "from": head, "to": good, "attempts": attempts, "ts": time.time(),
    }))
    ATTEMPTS.write_text("0")
    print(f"[boot] rolled back {head[:7]} -> {good[:7]} after "
          f"{attempts} failed boots", file=sys.stderr)


def main():
    try:
        guard()
    except Exception as exc:  # noqa: BLE001 — the guard must never block boot.
        print(f"[boot] guard error (ignored): {exc}", file=sys.stderr)

    os.chdir(str(REPO))
    sys.path.insert(0, str(REPO))
    import main as app
    app.main()


if __name__ == "__main__":
    main()
