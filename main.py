"""Entry point for the Claude Code Wrapper Agent Telegram bot.

Sets up logging, validates the runtime environment (token, allow-list, storage
directories, and a working Claude Code CLI), wires together the session
manager, formatter, and handler, registers the Telegram handlers, and starts
long-polling.
"""

import logging
import subprocess
import sys
from logging.handlers import RotatingFileHandler

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

import config
from bot.commands import CommandHandlers
from bot.formatter import MessageFormatter
from bot.handler import AgentHandler
from bot.session import SessionManager

logger = logging.getLogger("claude_agent")


def setup_logging() -> None:
    """Configure root logging with a rotating file handler (+ console on a TTY).

    The rotating file handler keeps ``agent.log`` bounded. A console handler is
    added only when stdout is an interactive terminal — when the bot runs as a
    background daemon (stdout redirected to agent.log), adding it would write
    every line twice, so it is skipped.
    """
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    file_handler = RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)

    if sys.stdout.isatty():
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    # Quiet down the very chatty HTTP library used by python-telegram-bot.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def validate_environment() -> None:
    """Fail fast if the environment is not ready to run the bot."""
    if not config.TELEGRAM_TOKEN:
        print(
            "ERROR: TELEGRAM_TOKEN is not set. Add it to your .env file.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not config.ALLOWED_USERS:
        print(
            "ERROR: ALLOWED_USERS is empty. Add at least one Telegram user ID "
            "to your .env file.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Ensure storage directories exist.
    try:
        config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        config.NOTES_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: could not create memory directories: {exc}", file=sys.stderr)
        sys.exit(1)

    # Verify the Claude Code CLI is installed and runnable.
    try:
        result = subprocess.run(
            [config.CLAUDE_COMMAND, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        print(
            f"ERROR: Claude Code CLI ('{config.CLAUDE_COMMAND}') was not found "
            f"on PATH. Install it or set CLAUDE_COMMAND in your .env.",
            file=sys.stderr,
        )
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(
            "ERROR: 'claude --version' timed out. Is the CLI working?",
            file=sys.stderr,
        )
        sys.exit(1)

    if result.returncode != 0:
        print(
            f"ERROR: 'claude --version' failed (exit {result.returncode}):\n"
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(1)

    logger.info("Claude Code detected: %s", result.stdout.strip())


def main() -> None:
    """Compose the application and start polling."""
    setup_logging()
    validate_environment()

    # Initialize collaborators.
    session_manager = SessionManager()
    formatter = MessageFormatter()
    agent = AgentHandler(session_manager, formatter)
    cmds = CommandHandlers()

    # Build the Telegram application.
    application = ApplicationBuilder().token(config.TELEGRAM_TOKEN).build()
    agent.application = application

    # Register handlers. Commands and plain text all route to handle_message,
    # which performs its own routing/access-control.
    # Route text plus rich content (photos, voice/audio, video notes, files)
    # to the agent; only slash-commands are excluded (handled separately).
    content_filter = (
        filters.TEXT
        | filters.CAPTION
        | filters.PHOTO
        | filters.VOICE
        | filters.AUDIO
        | filters.VIDEO
        | filters.VIDEO_NOTE
        | filters.Document.ALL
    )
    application.add_handler(
        MessageHandler(content_filter & ~filters.COMMAND, agent.handle_message)
    )
    for command in (
        "start", "clear", "help", "profile", "memory",
        "note", "today", "find", "context", "compress", "usage",
    ):
        application.add_handler(CommandHandler(command, agent.handle_message))

    # Direct local-tools test commands (handled outside the Claude agent).
    direct_commands = {
        "tools": cmds.tools,
        "gmail": cmds.gmail,
        "mail_full": cmds.mail_full,
        "calendar": cmds.calendar,
        "ls": cmds.ls,
        "read": cmds.read,
        "run": cmds.run,
        "notify": cmds.notify,
    }
    for name, callback in direct_commands.items():
        application.add_handler(CommandHandler(name, callback))

    logger.info("Agent started")
    application.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
