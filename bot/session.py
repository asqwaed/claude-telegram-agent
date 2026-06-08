"""Conversation session and long-term memory management.

Each Telegram chat has a session stored as a JSON file under
``SESSIONS_DIR/{chat_id}.json`` holding the recent message history, and an
optional long-term notes file under ``NOTES_DIR/{chat_id}.md`` written by the
agent itself. The :class:`SessionManager` is the single point of access for
both, and is responsible for enforcing the per-session message limit and for
building the context string that primes each Claude Code invocation.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class SessionManager:
    """Loads, persists, and trims per-chat conversation sessions.

    All file I/O is defensive: missing files yield empty sessions, and
    corrupted or unreadable files are logged and treated as empty rather than
    crashing the bot.
    """

    def __init__(self) -> None:
        """Initialize the manager, ensuring storage directories exist."""
        self.sessions_dir: Path = config.SESSIONS_DIR
        self.notes_dir: Path = config.NOTES_DIR
        self.limit: int = config.SESSION_LIMIT
        # Best-effort directory creation; main.py also validates these.
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            self.notes_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Failed to create memory directories: %s", exc)

    # --- Internal helpers --------------------------------------------------
    def _session_path(self, chat_id: int) -> Path:
        """Return the JSON session file path for a chat."""
        return self.sessions_dir / f"{chat_id}.json"

    def _empty_session(self, chat_id: int) -> dict:
        """Build a fresh, empty session dict for a chat."""
        now = _utc_now_iso()
        return {
            "chat_id": chat_id,
            "messages": [],
            "created_at": now,
            "updated_at": now,
        }

    def _trim(self, session: dict) -> dict:
        """Enforce SESSION_LIMIT by keeping only the most recent messages."""
        messages = session.get("messages", [])
        if len(messages) > self.limit:
            session["messages"] = messages[-self.limit:]
        return session

    # --- Public API --------------------------------------------------------
    def load(self, chat_id: int) -> dict:
        """Load a session from disk, returning an empty session if absent.

        The per-session message limit is always enforced after loading so that
        callers never see more than ``SESSION_LIMIT`` messages.
        """
        path = self._session_path(chat_id)
        if not path.exists():
            return self._empty_session(chat_id)
        try:
            with path.open("r", encoding="utf-8") as fh:
                session = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(
                "Failed to load session %s (%s); starting fresh.", chat_id, exc
            )
            return self._empty_session(chat_id)

        # Guard against malformed structures.
        if not isinstance(session, dict) or "messages" not in session:
            logger.warning("Malformed session %s; starting fresh.", chat_id)
            return self._empty_session(chat_id)
        session.setdefault("chat_id", chat_id)
        session.setdefault("created_at", _utc_now_iso())
        session.setdefault("updated_at", session["created_at"])
        return self._trim(session)

    def save(self, chat_id: int, session: dict) -> None:
        """Persist a session to disk as indented JSON.

        Updates the ``updated_at`` timestamp before writing.
        """
        session["updated_at"] = _utc_now_iso()
        path = self._session_path(chat_id)
        try:
            with path.open("w", encoding="utf-8") as fh:
                json.dump(session, fh, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.error("Failed to save session %s: %s", chat_id, exc)

    def add_message(self, chat_id: int, role: str, content: str) -> None:
        """Append a message to a session, enforce the limit, and persist."""
        session = self.load(chat_id)
        session["messages"].append(
            {
                "role": role,
                "content": content,
                "timestamp": _utc_now_iso(),
            }
        )
        self._trim(session)
        self.save(chat_id, session)

    def get_notes_path(self, chat_id: int) -> Path:
        """Return the long-term notes markdown path for a chat."""
        return self.notes_dir / f"{chat_id}.md"

    def load_notes(self, chat_id: int) -> str:
        """Return the long-term notes for a chat, or "" if absent/unreadable.

        Permission and other OS errors are logged and treated as empty notes so
        the bot keeps working even if a notes file becomes unreadable.
        """
        path = self.get_notes_path(chat_id)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read notes %s: %s", chat_id, exc)
            return ""

    def save_notes(self, chat_id: int, content: str) -> None:
        """Write (creating if needed) the long-term notes file for a chat."""
        path = self.get_notes_path(chat_id)
        try:
            path.write_text(content, encoding="utf-8")
            logger.info("Notes updated for chat_id=%s", chat_id)
        except OSError as exc:
            logger.error("Failed to save notes %s: %s", chat_id, exc)

    def append_notes(self, chat_id: int, new_entry: str) -> None:
        """Append an entry to the notes file, separated by a horizontal rule.

        The agent normally writes notes itself via its tools; this method is a
        programmatic fallback for appending an entry from Python code.
        """
        existing = self.load_notes(chat_id)
        if existing.strip():
            combined = f"{existing.rstrip()}\n---\n{new_entry}"
        else:
            combined = new_entry
        self.save_notes(chat_id, combined)

    def load_profile(self) -> str:
        """Return the always-loaded profile note (Obsidian _meta/Profile.md)."""
        path = config.PROFILE_PATH
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read profile %s: %s", path, exc)
            return ""

    def get_context_string(self, chat_id: int) -> str:
        """Build the priming context: profile (long-term) plus recent history.

        Format::

            [ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ]
            {Profile.md, or "пусто"}

            [ИСТОРИЯ ДИАЛОГА]
            user: ...
            assistant: ...

        The profile is the small always-loaded memory layer; deeper knowledge
        lives in the vault and is fetched on demand via the obs_* tools. The
        history section is omitted entirely when there are no messages.
        """
        profile = self.load_profile().strip()
        profile_block = profile if profile else "пусто"

        parts = ["[ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ]", profile_block]

        session = self.load(chat_id)
        messages = session.get("messages", [])
        if messages:
            parts.append("")
            parts.append("[ИСТОРИЯ ДИАЛОГА]")
            for msg in messages:
                role = msg.get("role", "user")
                label = "assistant" if role == "assistant" else "user"
                parts.append(f"{label}: {msg.get('content', '')}")

        return "\n".join(parts)

    # --- Context size & compression ---------------------------------------
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough token estimate (~4 characters per token)."""
        return len(text) // 4

    def context_stats(self, chat_id: int) -> dict:
        """Return size stats for the assembled context of a chat."""
        ctx = self.get_context_string(chat_id)
        profile = self.load_profile()
        messages = self.load(chat_id).get("messages", [])
        tokens = self.estimate_tokens(ctx)
        limit = config.CONTEXT_TOKEN_LIMIT
        return {
            "messages": len(messages),
            "tokens": tokens,
            "profile_tokens": self.estimate_tokens(profile),
            "limit": limit,
            "percent": round(100 * tokens / limit) if limit else 0,
        }

    def messages_to_compress(self, chat_id: int, keep_recent: int) -> list:
        """Return the older messages that should be summarized (or [])."""
        messages = self.load(chat_id).get("messages", [])
        if len(messages) <= keep_recent:
            return []
        return messages[:-keep_recent]

    def apply_compression(
        self, chat_id: int, summary_text: str, keep_recent: int
    ) -> None:
        """Replace older history with a single summary entry, keep recent ones."""
        session = self.load(chat_id)
        messages = session.get("messages", [])
        recent = messages[-keep_recent:] if len(messages) > keep_recent else messages
        summary_msg = {
            "role": "assistant",
            "content": f"[СЖАТАЯ ИСТОРИЯ ПРЕДЫДУЩЕГО ДИАЛОГА]\n{summary_text}",
            "timestamp": _utc_now_iso(),
        }
        session["messages"] = [summary_msg] + recent
        self.save(chat_id, session)

    def clear(self, chat_id: int) -> None:
        """Delete the session file for a chat. Notes are preserved."""
        path = self._session_path(chat_id)
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                logger.error("Failed to clear session %s: %s", chat_id, exc)
