"""One-time Google OAuth helper for the claude-agent project.

Authorize the default ("personal") account::

    python credentials/google_auth.py

Authorize an additional account (e.g. a work one)::

    python credentials/google_auth.py work

It loads ``credentials/google_credentials.json`` (an OAuth *Desktop app* client
downloaded from Google Cloud Console), opens a browser for you to log in and
approve, and saves the resulting token to the per-account file configured in
``config.GOOGLE_ACCOUNTS`` (``google_token.json`` for "personal").

On subsequent runs it reuses the saved token, silently refreshing it with the
stored ``refresh_token`` when it has expired. The same token files are consumed
by the Gmail, Calendar and Drive tools in ``mcp/server.py``.
"""

import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Make the project root importable so we can reuse the account registry.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

# Scopes requested. Changing these requires deleting the token file(s) and
# re-running this script so the user re-consents to the new scope set.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]

CREDS_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = CREDS_DIR / "google_credentials.json"


def main() -> None:
    """Obtain (or refresh) Google OAuth credentials for the chosen account."""
    account = sys.argv[1].strip().lower() if len(sys.argv) > 1 else config.DEFAULT_GOOGLE_ACCOUNT
    account = config.GOOGLE_EMAIL_TO_ALIAS.get(account, account)
    token_path = config.GOOGLE_ACCOUNTS.get(account)
    if token_path is None:
        print(
            f"error: unknown account '{account}'. "
            f"valid accounts: {', '.join(config.GOOGLE_ACCOUNTS)}"
        )
        return
    TOKEN_FILE = token_path
    print(f"authorizing account '{account}' -> {TOKEN_FILE.name}")

    if not CREDENTIALS_FILE.exists():
        print(
            f"error: {CREDENTIALS_FILE} not found.\n"
            "download an OAuth 'Desktop app' client JSON from "
            "https://console.cloud.google.com and save it there first."
        )
        return

    creds: Credentials | None = None

    # Reuse an existing token if present.
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(TOKEN_FILE), SCOPES
            )
        except (ValueError, KeyError):
            creds = None

    # Valid token already on disk — nothing to do.
    if creds and creds.valid:
        print("token already valid")
        return

    # Expired but refreshable — refresh silently, no browser needed.
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            print("token already valid")
            return
        except Exception as exc:  # noqa: BLE001 — fall back to full flow.
            print(f"refresh failed ({exc}); starting browser login...")
            creds = None

    # No usable token — run the interactive browser flow.
    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_FILE), SCOPES
    )
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    print(f"google auth complete. token saved to {TOKEN_FILE}")


if __name__ == "__main__":
    main()
