"""Direct test commands for the Telegram bot.

These slash commands call the local-tools functions (gmail, calendar, files,
shell, telegram) directly — without going through the Claude Code agent — so the
user can verify each capability deterministically and fast. The synchronous
tool functions are run in a worker thread to avoid blocking the asyncio loop.

The richer MCP-only tools (brave-search, github, youtube, spotify, playwright,
fetch, knowledge-graph memory) are exercised by simply chatting with the bot in
natural language — Claude Code picks them up from ~/.claude/mcp.json.
"""

import asyncio
import logging
import sys
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import config

logger = logging.getLogger(__name__)

# Import the local-tools module (mcp/server.py) so we can call its functions.
_BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE_DIR / "mcp"))
import server as local_tools  # noqa: E402

MAX_CHUNK = 4000


TOOLS_OVERVIEW = (
    "<b>что я умею — команды для ручной проверки</b>\n\n"
    "<b>📧 gmail</b>\n"
    "/gmail [запрос] — последние письма (без запроса = непрочитанные)\n"
    "  пример: <code>/gmail from:github</code>\n"
    "/mail_full &lt;id&gt; — полный текст письма по id из /gmail\n\n"
    "<b>📅 calendar</b>\n"
    "/calendar [дней] — события на N дней вперёд (по умолч. 7)\n\n"
    "<b>📁 files</b>\n"
    "/ls [путь] — содержимое папки (по умолч. корень проекта)\n"
    "/read &lt;путь&gt; — прочитать файл\n"
    "/run &lt;команда&gt; — выполнить shell-команду (30с, опасное заблокировано)\n\n"
    "<b>📨 telegram</b>\n"
    "/notify &lt;текст&gt; — отправить себе уведомление через бота\n\n"
    "<b>🤖 через агента (просто пиши текстом)</b>\n"
    "• поиск в вебе → «найди ...» (brave + fetch)\n"
    "• github → «покажи мои репозитории»\n"
    "• youtube → скинь ссылку: «сделай саммари этого видео»\n"
    "• spotify → «включи ...»\n"
    "• obsidian-волт → «запиши в обсидиан ...», «что у меня по X», «свяжи с ...»\n\n"
    "<b>служебные</b>\n"
    "/start /help /tools /clear /profile /note /today /find /context /compress"
)


def _allowed(update: Update) -> bool:
    """Return True if the updating user is on the allow-list."""
    user = update.effective_user
    return user is not None and user.id in config.ALLOWED_USERS


def _args_text(update: Update, command: str) -> str:
    """Return everything after the command word, trimmed."""
    text = (update.effective_message.text or "").strip()
    # Strip the leading /command (and optional @botname suffix).
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def _reply_long(update: Update, text: str) -> None:
    """Reply with arbitrary-length plain text, chunked under Telegram's limit."""
    if not text.strip():
        text = "(пусто)"
    for i in range(0, len(text), MAX_CHUNK):
        await update.effective_message.reply_text(text[i : i + MAX_CHUNK])


class CommandHandlers:
    """Bundle of direct local-tools slash-command handlers."""

    async def _guard(self, update: Update) -> bool:
        """Access-control gate; replies and returns False if denied."""
        if not _allowed(update):
            await update.effective_message.reply_text("Access denied.")
            return False
        return True

    async def tools(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Print the capability overview."""
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            TOOLS_OVERVIEW, parse_mode=ParseMode.HTML
        )

    async def gmail(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List recent emails (optional Gmail search query)."""
        if not await self._guard(update):
            return
        query = _args_text(update, "gmail")
        logger.info("/gmail query=%r", query)
        result = await asyncio.to_thread(
            local_tools.gmail_read, 10, query
        )
        await _reply_long(update, result)

    async def mail_full(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show the full body of an email by message id."""
        if not await self._guard(update):
            return
        msg_id = _args_text(update, "mail_full")
        if not msg_id:
            await update.effective_message.reply_text("укажи id: /mail_full <id>")
            return
        result = await asyncio.to_thread(local_tools.gmail_read_full, msg_id)
        await _reply_long(update, result)

    async def calendar(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List upcoming calendar events for N days."""
        if not await self._guard(update):
            return
        arg = _args_text(update, "calendar")
        try:
            days = int(arg) if arg else 7
        except ValueError:
            days = 7
        result = await asyncio.to_thread(local_tools.calendar_list_events, days)
        await _reply_long(update, result)

    async def ls(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List a directory (defaults to the project root)."""
        if not await self._guard(update):
            return
        path = _args_text(update, "ls") or str(config.BASE_DIR)
        result = await asyncio.to_thread(local_tools.list_directory, path)
        await _reply_long(update, result)

    async def read(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Read a file inside the allowed directories."""
        if not await self._guard(update):
            return
        path = _args_text(update, "read")
        if not path:
            await update.effective_message.reply_text("укажи путь: /read <путь>")
            return
        result = await asyncio.to_thread(local_tools.read_file, path)
        await _reply_long(update, result)

    async def run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Run a shell command via the sandboxed local-tools runner."""
        if not await self._guard(update):
            return
        cmd = _args_text(update, "run")
        if not cmd:
            await update.effective_message.reply_text("укажи команду: /run <cmd>")
            return
        logger.info("/run cmd=%r", cmd[:80])
        result = await asyncio.to_thread(local_tools.run_command, cmd, None)
        await _reply_long(update, result)

    async def notify(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Send a Telegram notification to the configured chat."""
        if not await self._guard(update):
            return
        text = _args_text(update, "notify")
        if not text:
            await update.effective_message.reply_text("укажи текст: /notify <текст>")
            return
        result = await asyncio.to_thread(local_tools.send_telegram, text)
        await update.effective_message.reply_text(result)
