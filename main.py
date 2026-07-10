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
    CallbackQueryHandler,
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


async def _post_init(application) -> None:
    """Register the slash-command menu and confirm a healthy (re)start.

    Reaching this point means imports succeeded, config loaded, the Telegram
    Application was built, and the command-menu API call below round-trips — so
    the token is valid and Telegram is reachable. That's our "the bot is up"
    signal: we mark this revision healthy (clearing the boot-failure counter and
    pinning it as last-known-good) and, if the last boot was a self-update or an
    auto-rollback, report the outcome to the user.
    """
    from telegram import BotCommand

    from bot import selfupdate

    await application.bot.set_my_commands(
        [
            BotCommand("profile", "профиль — что я о тебе помню"),
            BotCommand("note", "быстрая заметка в волт"),
            BotCommand("today", "дневник за сегодня"),
            BotCommand("find", "поиск по волту"),
            BotCommand("usage", "расход токенов и лимиты"),
            BotCommand("context", "наполненность контекста"),
            BotCommand("compress", "сжать историю диалога"),
            BotCommand("clear", "очистить историю диалога"),
            BotCommand("model", "показать / сменить модель и effort"),
            BotCommand("stop", "остановить текущую задачу"),
            BotCommand("restart", "перезапустить бота"),
            BotCommand("help", "список команд"),
        ]
    )
    logger.info("Bot command menu registered")

    # The boot succeeded — record it as known-good and report any self-update.
    selfupdate.mark_healthy()
    await selfupdate.notify_after_boot(application.bot)


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
    application = (
        ApplicationBuilder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(_post_init)
        # Process updates concurrently so a /stop (or any quick command) is
        # handled while a long Claude Code turn is still running, instead of
        # queuing behind it.
        .concurrent_updates(True)
        .build()
    )
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
    # Inline confirmation-button taps (✅ да / ✖️ отмена).
    application.add_handler(CallbackQueryHandler(agent.handle_callback))
    for command in (
        "start", "clear", "help", "profile", "memory",
        "note", "today", "find", "context", "compress", "usage", "restart",
        "model", "stop",
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
    # Include callback_query so inline-button taps reach handle_callback.
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
