"""Central configuration for the Claude Code Wrapper Agent.

Loads environment variables from the project's ``.env`` file and exposes every
path and tunable constant the rest of the application depends on. All paths are
derived from the location of this file so the project can be moved or cloned
anywhere without breaking.
"""

from pathlib import Path

from dotenv import load_dotenv
import os

# Resolve the project root as the directory that contains this config file.
BASE_DIR: Path = Path(__file__).resolve().parent

# Load the .env file that lives next to this config module.
load_dotenv(BASE_DIR / ".env")


def _parse_allowed_users(raw: str) -> list[int]:
    """Parse a comma-separated list of Telegram user IDs into ints.

    Whitespace and empty entries are ignored so values like ``"1, 2,"`` parse
    cleanly. Non-integer entries are skipped rather than crashing startup.
    """
    users: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            users.append(int(chunk))
        except ValueError:
            # Ignore malformed entries; main.py validates the final list.
            continue
    return users


# --- Credentials / access control -----------------------------------------
TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "").strip()
ALLOWED_USERS: list[int] = _parse_allowed_users(os.getenv("ALLOWED_USERS", ""))

# --- Phase 6: MCP service credentials --------------------------------------
# Personal chat the agent sends proactive notifications to.
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# Web search.
BRAVE_API_KEY: str = os.getenv("BRAVE_API_KEY", "").strip()
# GitHub personal access token.
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "").strip()
# Spotify OAuth app credentials.
SPOTIFY_CLIENT_ID: str = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET: str = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
SPOTIFY_REDIRECT_URI: str = os.getenv(
    "SPOTIFY_REDIRECT_URI", "http://localhost:8888/callback"
).strip()

# --- Phase 7: Telegram MTProto (user accounts) -----------------------------
# Multiple user accounts, each with its own api_id/api_hash (from
# https://my.telegram.org), login phone, and Telethon session file. Configured
# via .env: TELEGRAM_ACCOUNTS lists the aliases (first = default), and each
# alias has TELEGRAM_<ALIAS>_API_ID / _API_HASH / _PHONE entries.
def _parse_telegram_accounts() -> dict[str, dict[str, str]]:
    """Build the {alias: {api_id, api_hash, phone}} registry from env vars."""
    raw = os.getenv("TELEGRAM_ACCOUNTS", "")
    accounts: dict[str, dict[str, str]] = {}
    for alias in (a.strip().lower() for a in raw.split(",") if a.strip()):
        prefix = f"TELEGRAM_{alias.upper()}_"
        accounts[alias] = {
            "api_id": os.getenv(prefix + "API_ID", "").strip(),
            "api_hash": os.getenv(prefix + "API_HASH", "").strip(),
            "phone": os.getenv(prefix + "PHONE", "").strip(),
        }
    return accounts


TELEGRAM_ACCOUNTS: dict[str, dict[str, str]] = _parse_telegram_accounts()
DEFAULT_TELEGRAM_ACCOUNT: str = next(iter(TELEGRAM_ACCOUNTS), "")
# faster-whisper model size for voice-message transcription.
WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "small").strip()

# --- Paths -----------------------------------------------------------------
MEMORY_DIR: Path = BASE_DIR / "memory"
SESSIONS_DIR: Path = MEMORY_DIR / "sessions"
NOTES_DIR: Path = MEMORY_DIR / "notes"
# Downloaded incoming media (photos/files) the agent may inspect or save.
MEDIA_DIR: Path = MEMORY_DIR / "media"
CLAUDE_MD_PATH: Path = BASE_DIR / "CLAUDE.md"

# Curated MCP server definitions (brave-search, github, youtube, spotify,
# playwright, fetch, knowledge-graph memory, local-tools). Claude Code does NOT
# auto-load this path, so handler.py passes it explicitly via --mcp-config.
# Overridable via the MCP_CONFIG_PATH env var.
MCP_CONFIG_PATH: Path = Path(
    os.getenv("MCP_CONFIG_PATH", str(Path.home() / ".claude" / "mcp.json"))
)

# Credentials & OAuth tokens (gitignored, never committed).
CREDENTIALS_DIR: Path = BASE_DIR / "credentials"
GOOGLE_CREDENTIALS_PATH: Path = CREDENTIALS_DIR / "google_credentials.json"
GOOGLE_TOKEN_PATH: Path = CREDENTIALS_DIR / "google_token.json"

# Per-account Telethon session files (each is an MTProto login key — treat like
# a password). e.g. credentials/telegram_personal.session
def telegram_session_path(alias: str) -> Path:
    """Return the Telethon session file path for a Telegram account alias."""
    return CREDENTIALS_DIR / f"telegram_{alias}.session"

# --- Google multi-account ---------------------------------------------------
# Aliases come from GOOGLE_ACCOUNTS (comma-separated, first = default). Each is
# authorized with: python credentials/google_auth.py <alias>. The "personal"
# alias keeps the default token filename; others get google_token_<alias>.json.
def _parse_google_accounts() -> dict[str, Path]:
    raw = os.getenv("GOOGLE_ACCOUNTS", "personal")
    accounts: dict[str, Path] = {}
    for alias in (a.strip().lower() for a in raw.split(",") if a.strip()):
        accounts[alias] = (
            GOOGLE_TOKEN_PATH
            if alias == "personal"
            else CREDENTIALS_DIR / f"google_token_{alias}.json"
        )
    return accounts or {"personal": GOOGLE_TOKEN_PATH}


GOOGLE_ACCOUNTS: dict[str, Path] = _parse_google_accounts()
DEFAULT_GOOGLE_ACCOUNT: str = next(iter(GOOGLE_ACCOUNTS), "personal")


# Optional email->alias map: GOOGLE_EMAIL_MAP="me@gmail.com:personal,w@x.com:work"
def _parse_email_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for pair in os.getenv("GOOGLE_EMAIL_MAP", "").split(","):
        if ":" in pair:
            email, alias = pair.split(":", 1)
            mapping[email.strip().lower()] = alias.strip().lower()
    return mapping


GOOGLE_EMAIL_TO_ALIAS: dict[str, str] = _parse_email_map()

# --- Tunables --------------------------------------------------------------
# Maximum number of messages retained per conversation session.
SESSION_LIMIT: int = 30

# Context budget (rough token estimate of the assembled context = notes +
# conversation history). When a turn's context exceeds this, the older history
# is auto-compressed into a summary. ~4 chars per token is assumed.
CONTEXT_TOKEN_LIMIT: int = 12000
# When compressing, keep this many most-recent messages verbatim; everything
# older is collapsed into a single summary entry.
COMPRESS_KEEP_RECENT: int = 8

# Seconds to wait for the Claude Code subprocess before killing it.
CLAUDE_TIMEOUT: int = 120

# Seconds between "thinking" status updates sent to the user.
THINKING_UPDATE_INTERVAL: int = 5

# The Claude Code CLI command. Overridable via the CLAUDE_COMMAND env var.
CLAUDE_COMMAND: str = os.getenv("CLAUDE_COMMAND", "claude")

# --- Obsidian vault (knowledge base + always-loaded profile) ---------------
# File-based: the agent reads/writes plain .md files in this vault. The Profile
# note is the small "always in context" layer injected into every prompt.
VAULT_DIR: Path = Path(
    os.getenv("VAULT_DIR", str(Path.home() / "Documents" / "Vault"))
)
PROFILE_PATH: Path = VAULT_DIR / "_meta" / "Profile.md"
INBOX_DIR: Path = VAULT_DIR / "00 Inbox"
JOURNAL_DIR: Path = VAULT_DIR / "Journal"

# --- Logging ---------------------------------------------------------------
# Rotating file log so agent.log never grows unbounded. Default: 5 MB per file,
# 3 old files kept (≈20 MB max total).
# Snapshot of official Claude subscription rate limits (5h + weekly), written by
# the Claude Code statusline script. The bot reads it for /usage.
RATELIMITS_FILE: Path = Path.home() / ".claude" / "ratelimits.json"

LOG_FILE: Path = BASE_DIR / "agent.log"
LOG_MAX_BYTES: int = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUP_COUNT: int = int(os.getenv("LOG_BACKUP_COUNT", "3"))
