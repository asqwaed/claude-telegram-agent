"""Self-update machinery: let the agent safely modify and restart itself.

The bot runs Claude Code with full file/shell access in this repo, so the agent
can already edit its own source. The missing piece — handled here — is applying
those edits safely:

  * ``request_restart`` commits a checkpoint and drops a flag the running bot
    picks up after it has finished replying, then exits so launchd respawns it.
  * ``mark_healthy`` is called once the new process has actually come up (token
    valid, Telegram reachable). It clears the boot-failure counter and records
    the current commit as the last known-good revision.
  * The crash-rollback guard lives in ``deploy/boot.py`` (stdlib-only, so a
    broken edit anywhere in the app can't stop it running): if a fresh boot
    fails repeatedly it ``git reset --hard`` back to ``last_good``.

Together these guarantee a bad self-edit can never permanently brick the bot —
worst case it crash-loops for ~30s, auto-rolls-back, and tells you it did.
"""

import json
import logging
import subprocess
import time
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# State dir (gitignored). Kept in sync with deploy/boot.py — if you rename these,
# update both files.
STATE_DIR: Path = config.BASE_DIR / ".selfupdate"
LAST_GOOD: Path = STATE_DIR / "last_good"
REQUEST: Path = STATE_DIR / "request.json"
ROLLED_BACK: Path = STATE_DIR / "rolled_back.json"
BOOT_ATTEMPTS: Path = STATE_DIR / "boot_attempts"


def _git(*args: str) -> subprocess.CompletedProcess:
    """Run a git command inside the repo, capturing output (never raises)."""
    return subprocess.run(
        ["git", "-C", str(config.BASE_DIR), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def git_head() -> str:
    """Return the current HEAD sha (full), or '' if git is unavailable."""
    try:
        out = _git("rev-parse", "HEAD").stdout.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("git_head failed: %s", exc)
        return ""
    return out


def _short(sha: str) -> str:
    return sha[:7] if sha else "?"


# --- Health: called by the freshly-booted process once it's actually up -------
def mark_healthy() -> None:
    """Record that this revision booted fine: reset the failure counter and
    pin ``last_good`` to the current commit.

    Called from ``main._post_init`` — by which point imports succeeded, config
    loaded, the Telegram app was built, and the command-menu API call (in
    post_init) round-tripped, so the token is valid and Telegram reachable.
    """
    try:
        STATE_DIR.mkdir(exist_ok=True)
        BOOT_ATTEMPTS.write_text("0")
        head = git_head()
        if head:
            LAST_GOOD.write_text(head)
        logger.info("Self-update: marked healthy at %s", _short(head))
    except OSError as exc:
        logger.warning("mark_healthy failed: %s", exc)


# --- Restart request: written by the agent / a freshly-edited turn ------------
def request_restart(reason: str = "self-update") -> dict:
    """Commit a checkpoint of the working tree and flag a pending restart.

    The running bot consumes the flag (``consume_request``) after it has
    delivered the current reply, then restarts. Returns a small status dict.
    """
    STATE_DIR.mkdir(exist_ok=True)
    prev_head = git_head()

    # Checkpoint: commit whatever changed so there's a clean restore point and
    # so the rollback guard has a concrete commit to compare against. A no-op
    # commit (nothing staged) is fine — we just proceed to restart.
    committed = False
    try:
        _git("add", "-A")
        res = _git("commit", "-m", f"self-update: {reason}")
        committed = res.returncode == 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpoint commit failed: %s", exc)

    new_head = git_head()
    payload = {
        "reason": reason,
        "prev_head": prev_head,
        "new_head": new_head,
        "committed": committed,
        "ts": time.time(),
    }
    REQUEST.write_text(json.dumps(payload))
    logger.info(
        "Self-update: restart requested (%s) %s -> %s committed=%s",
        reason, _short(prev_head), _short(new_head), committed,
    )
    return payload


def has_pending_restart() -> bool:
    """True if a restart has been requested but not yet acted on."""
    return REQUEST.exists()


def _consume(path: Path) -> dict | None:
    """Read-and-delete a JSON state file; return its dict or None."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text() or "{}")
    except (json.JSONDecodeError, OSError):
        data = {}
    try:
        path.unlink()
    except OSError:
        pass
    return data if isinstance(data, dict) else {}


def consume_request() -> dict | None:
    """Pop the pending-restart payload (after a successful restart)."""
    return _consume(REQUEST)


def consume_rolled_back() -> dict | None:
    """Pop the rollback marker the boot guard leaves after an auto-revert."""
    return _consume(ROLLED_BACK)


# --- Post-restart user notification ------------------------------------------
async def notify_after_boot(bot) -> None:
    """Tell the user how the last (re)start went, if it was a self-update.

    Sends to ``config.TELEGRAM_CHAT_ID``. Silent if that isn't configured or if
    this was just an ordinary boot (no request / rollback markers).
    """
    chat_id = config.TELEGRAM_CHAT_ID
    rolled = consume_rolled_back()
    req = consume_request()

    if not chat_id:
        return

    msg = None
    if rolled:
        msg = (
            "⚠️ правка не завелась — откатил сам\n"
            f"крашнулся {rolled.get('attempts', '?')} раз подряд, "
            f"вернул рабочую версию {_short(rolled.get('to', ''))}\n"
            f"(сломанная была {_short(rolled.get('from', ''))})"
        )
    elif req:
        head = _short(req.get("new_head", git_head()))
        msg = f"✅ обновился и перезапустился\nHEAD {head} · {req.get('reason', '')}"

    if msg:
        try:
            await bot.send_message(chat_id=chat_id, text=msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-boot notify failed: %s", exc)


# --- CLI: the agent calls this from bash after editing its own code -----------
def _main(argv: list[str]) -> int:
    """`python3 bot/selfupdate.py request --reason "..."` → checkpoint + flag."""
    import argparse

    parser = argparse.ArgumentParser(prog="selfupdate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_req = sub.add_parser("request", help="commit a checkpoint and flag a restart")
    p_req.add_argument("--reason", default="self-update", help="what changed")
    sub.add_parser("status", help="print current self-update state")

    args = parser.parse_args(argv)
    if args.cmd == "request":
        payload = request_restart(args.reason)
        print(
            f"checkpoint {_short(payload['prev_head'])} -> "
            f"{_short(payload['new_head'])} (committed={payload['committed']}); "
            "restart flag set — the bot will restart after this reply."
        )
        return 0
    if args.cmd == "status":
        print(json.dumps({
            "head": git_head(),
            "last_good": LAST_GOOD.read_text().strip() if LAST_GOOD.exists() else None,
            "boot_attempts": BOOT_ATTEMPTS.read_text().strip() if BOOT_ATTEMPTS.exists() else "0",
            "pending_restart": has_pending_restart(),
        }, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    # Allow running as a standalone script (path-insert so `import config` works).
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(_main(sys.argv[1:]))
