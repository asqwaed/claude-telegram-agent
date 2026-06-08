"""One-time Telegram MTProto (user account) login helper — multi-account.

Each account alias is configured in ``.env`` (see ``TELEGRAM_ACCOUNTS`` and the
``TELEGRAM_<ALIAS>_API_ID/_API_HASH/_PHONE`` entries). This script performs the
interactive login for one account: Telegram sends a login code to that account's
Telegram app, you enter it here (and the two-factor password if set). On success
a per-account session file is written and reused by the MTProto tools in
``mcp/server.py`` — you only run this once per account.

Run (default account)::

    python credentials/telegram_auth.py

Run for a specific account::

    python credentials/telegram_auth.py work

The login code and 2FA password are read from stdin, so the bot can drive this
non-interactively by piping them in.
"""

import sys
from pathlib import Path

# Make the project root importable so we can reuse config.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

from telethon import TelegramClient  # noqa: E402


def main() -> None:
    """Run the interactive MTProto login for the chosen account."""
    alias = (
        sys.argv[1].strip().lower()
        if len(sys.argv) > 1
        else config.DEFAULT_TELEGRAM_ACCOUNT
    )
    acct = config.TELEGRAM_ACCOUNTS.get(alias)
    if acct is None:
        print(
            f"error: unknown telegram account '{alias}'. "
            f"valid: {', '.join(config.TELEGRAM_ACCOUNTS) or '(none configured)'}"
        )
        return
    if not (acct["api_id"] and acct["api_hash"] and acct["phone"]):
        print(
            f"error: account '{alias}' is missing api_id/api_hash/phone in .env "
            f"(TELEGRAM_{alias.upper()}_API_ID / _API_HASH / _PHONE)."
        )
        return

    session = str(config.telegram_session_path(alias)).removesuffix(".session")
    print(f"authorizing telegram account '{alias}' ({acct['phone']})")

    client = TelegramClient(session, int(acct["api_id"]), acct["api_hash"])
    client.start(
        phone=lambda: acct["phone"],
        code_callback=lambda: input("enter the login code Telegram sent you: ").strip(),
        password=lambda: input("enter your 2FA password (blank if none): ").strip(),
    )

    me = client.get_me()
    print(
        f"login complete: '{alias}' -> "
        f"{getattr(me, 'first_name', '')} (@{getattr(me, 'username', None)}), "
        f"session saved to {config.telegram_session_path(alias).name}"
    )
    client.disconnect()


if __name__ == "__main__":
    main()
