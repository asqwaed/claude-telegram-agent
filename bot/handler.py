"""Core Telegram message handler that drives Claude Code.

:class:`AgentHandler` is the bridge between Telegram and the Claude Code CLI.
For each incoming message it enforces access control, maintains conversation
history via the :class:`~bot.session.SessionManager`, launches Claude Code in
headless (``--print``) mode as an async subprocess, shows a live "thinking"
indicator while it runs, and finally formats and delivers the reply.
"""

import asyncio
import io
import json
import logging
import os
import tempfile
import time
import traceback
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import config
from bot import transcribe
from bot import usage as usage_tracker
from bot import vault
from bot.formatter import MessageFormatter
from bot.session import SessionManager

logger = logging.getLogger(__name__)

WELCOME_MESSAGE = (
    "👋 <b>Hi! I'm your personal AI assistant.</b>\n\n"
    "I run locally on your machine through Claude Code, so I can read and write "
    "files, run terminal commands, and help with real tasks — not just chat.\n\n"
    "Just send me a message describing what you need — text, voice, photos, "
    "forwards or replies all work. I remember our recent conversation and "
    "important facts about you between sessions.\n\n"
    "<b>Commands</b>\n"
    "/start — show this message\n"
    "/help — list commands\n"
    "/clear — wipe our conversation history (long-term notes are kept)"
)

HELP_MESSAGE = (
    "<b>Available commands</b>\n\n"
    "/start — welcome message and overview\n"
    "/help — this list\n"
    "/clear — clear the current conversation memory\n"
    "/profile — показать профиль (всегда-загружаемая память)\n"
    "/note &lt;текст&gt; — быстрая заметка в инбокс волта\n"
    "/today [текст] — дневник: показать или дописать\n"
    "/find &lt;запрос&gt; — поиск по волту\n"
    "/usage — расход токенов (инфографика); /usage chart — график по дням\n"
    "/context — наполненность контекста\n"
    "/compress — сжать историю диалога в саммари\n"
    "/tools — список команд для ручной проверки инструментов\n\n"
    "Send any other text and I'll work on it for you."
)


class AgentHandler:
    """Handles Telegram updates by delegating reasoning to Claude Code."""

    def __init__(
        self,
        session_manager: SessionManager,
        formatter: MessageFormatter,
    ) -> None:
        """Store collaborators. The Application is attached later via setup."""
        self.session = session_manager
        self.formatter = formatter
        # Set by main.py once the Application is built, in case we ever need to
        # send messages outside of an update context.
        self.application = None

    # --- Access control ----------------------------------------------------
    def _is_allowed(self, user_id: int) -> bool:
        """Return True if the user is on the allow-list."""
        return user_id in config.ALLOWED_USERS

    # --- Telegram input extraction ----------------------------------------
    @staticmethod
    def _message_kind(m) -> str:
        """Classify an incoming message by its primary content."""
        if m.photo:
            return "photo"
        if m.voice:
            return "voice"
        if m.audio:
            return "audio"
        if m.video_note:
            return "video_note"
        if m.video:
            return "video"
        if m.document:
            return "document"
        if getattr(m, "forward_origin", None):
            return "forward"
        if m.reply_to_message:
            return "reply"
        return "text"

    @staticmethod
    def _sender_label(m) -> str:
        """Human label for who sent a message."""
        u = getattr(m, "from_user", None)
        if u:
            return u.full_name or (("@" + u.username) if u.username else "кто-то")
        chat = getattr(m, "sender_chat", None) or getattr(m, "chat", None)
        return getattr(chat, "title", None) or "кто-то"

    @staticmethod
    def _media_label(m) -> str:
        """Short placeholder for a message that carries media but no text."""
        if m.photo:
            return "[фото]"
        if m.voice or m.audio:
            return "[голосовое]"
        if m.video_note:
            return "[кружок]"
        if m.video:
            return "[видео]"
        if m.document:
            return f"[файл: {getattr(m.document, 'file_name', '')}]"
        return ""

    @staticmethod
    def _forward_label(m) -> str:
        """Origin of a forwarded message ('' if not forwarded)."""
        fo = getattr(m, "forward_origin", None)
        if not fo:
            return ""
        # Cover the MessageOrigin* variants without importing each type.
        user = getattr(fo, "sender_user", None)
        if user is not None:
            return user.full_name
        name = getattr(fo, "sender_user_name", None)
        if name:
            return name  # hidden user
        chat = getattr(fo, "sender_chat", None) or getattr(fo, "chat", None)
        if chat is not None:
            return getattr(chat, "title", None) or getattr(chat, "full_name", "канал")
        return "неизвестно"

    async def _download(
        self, tg_obj, suffix: str = ".bin", keep: bool = False, name: str = None
    ) -> str | None:
        """Download a Telegram file object to disk; return its path or None."""
        try:
            f = await tg_obj.get_file()
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_file failed: %s", exc)
            return None
        fname = name or f"{f.file_unique_id}{suffix}"
        base = config.MEDIA_DIR if keep else Path(tempfile.gettempdir())
        target = base / fname
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            await f.download_to_drive(str(target))
            return str(target)
        except Exception as exc:  # noqa: BLE001
            logger.warning("download failed: %s", exc)
            return None

    async def _gather_input(self, message, base_text: str) -> str:
        """Assemble one enriched message: forward/reply context, transcript, media.

        Images are downloaded and referenced by path so Claude can view them via
        its Read tool; voice/audio is transcribed locally with faster-whisper.
        """
        parts: list[str] = []

        fwd = self._forward_label(message)
        if fwd:
            parts.append(f"[ПЕРЕСЛАНО от {fwd}]")

        reply = message.reply_to_message
        if reply:
            who = self._sender_label(reply)
            quoted = (reply.text or reply.caption or self._media_label(reply) or "").strip()
            parts.append(f"[В ОТВЕТ НА сообщение от {who}]:\n{quoted[:1200]}")

        if base_text:
            parts.append(base_text)

        # Voice / audio / round-video → transcribe (off the event loop).
        voice = message.voice or message.audio or message.video_note
        if voice:
            path = await self._download(voice, suffix=".ogg")
            if path:
                transcript = await asyncio.to_thread(transcribe.transcribe, path)
                try:
                    os.remove(path)
                except OSError:
                    pass
                parts.append(
                    f"[ГОЛОСОВОЕ, расшифровка]:\n{transcript}"
                    if transcript
                    else "[ГОЛОСОВОЕ: не удалось расшифровать]"
                )

        # Photo → keep on disk and let Claude look at it.
        if message.photo:
            path = await self._download(message.photo[-1], suffix=".jpg", keep=True)
            if path:
                parts.append(
                    f"[ИЗОБРАЖЕНИЕ прикреплено: {path}\n"
                    f"посмотри его своим Read-инструментом и учти в ответе]"
                )

        # Generic document/video file → reference the path.
        if message.document:
            path = await self._download(
                message.document, keep=True,
                name=getattr(message.document, "file_name", None),
            )
            if path:
                parts.append(f"[ФАЙЛ прикреплён: {path}]")
        elif message.video:
            path = await self._download(message.video, suffix=".mp4", keep=True)
            if path:
                parts.append(f"[ВИДЕО прикреплено: {path}]")

        return "\n\n".join(p for p in parts if p).strip() or "(пустое сообщение)"

    # --- Claude Code invocation helpers -----------------------------------
    def _claude_cli_args(self, prompt: str, json_output: bool = False) -> list[str]:
        """Build the Claude Code CLI argv for a prompt.

        The curated MCP servers live in config.MCP_CONFIG_PATH, which Claude Code
        does not auto-load, so we pass it explicitly when present (without it the
        agent has zero MCP tools). NOTE: ``--mcp-config`` is variadic, so it must
        be followed immediately by another flag — otherwise it swallows the
        prompt as a config path (ENAMETOOLONG). Keep the prompt strictly last.

        ``json_output`` adds ``--output-format json`` so the result carries token
        usage / cost metadata alongside the text.
        """
        args = [config.CLAUDE_COMMAND]
        if config.MCP_CONFIG_PATH.is_file():
            args += ["--mcp-config", str(config.MCP_CONFIG_PATH)]
        else:
            logger.warning(
                "MCP config not found at %s; agent will run without MCP tools.",
                config.MCP_CONFIG_PATH,
            )
        if json_output:
            args += ["--output-format", "json"]
        args += ["--print", "--dangerously-skip-permissions", prompt]
        return args

    @staticmethod
    def _parse_claude_json(stdout: str) -> tuple[str, dict | None]:
        """Parse ``--output-format json`` stdout into (reply_text, usage_dict).

        Falls back to (raw_stdout, None) if the output isn't the expected JSON,
        so the bot keeps working even if the CLI format changes.
        """
        try:
            obj = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return stdout, None
        if not isinstance(obj, dict):
            return stdout, None
        text = obj.get("result") or obj.get("text") or ""
        u = obj.get("usage") or {}
        usage = {
            "input": int(u.get("input_tokens", 0) or 0),
            "output": int(u.get("output_tokens", 0) or 0),
            "cache_read": int(u.get("cache_read_input_tokens", 0) or 0),
            "cache_creation": int(u.get("cache_creation_input_tokens", 0) or 0),
            "cost": float(obj.get("total_cost_usd", 0.0) or 0.0),
            "duration_ms": int(obj.get("duration_ms", 0) or 0),
            "turns": int(obj.get("num_turns", 0) or 0),
        }
        return (text or stdout), usage

    async def _run_claude_plain(self, prompt: str, timeout: int = 90) -> str:
        """Run a one-shot Claude Code prompt and return stdout (or 'error: ...').

        Used for internal tasks like history compression — no thinking animation.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._claude_cli_args(prompt),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(config.BASE_DIR),
            )
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            return "error: timed out"
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        if proc.returncode != 0:
            return f"error: {err_b.decode('utf-8', 'replace')[:200]}"
        return out_b.decode("utf-8", "replace").strip()

    # --- Prompt construction ----------------------------------------------
    def _build_prompt(self, chat_id: int, message_text: str) -> str:
        """Assemble the full prompt passed to Claude Code."""
        context_string = self.session.get_context_string(chat_id)
        return (
            f"{context_string}\n\n"
            f"[NEW MESSAGE]\n"
            f"{message_text}\n\n"
            f"[INSTRUCTIONS]\n"
            f"After responding, persist anything worth remembering (see the "
            f"memory rules in CLAUDE.md): durable personal facts → update "
            f"{config.PROFILE_PATH} concisely; richer notes, people, projects, "
            f"journal and relationships → the Obsidian vault via the obs_* tools "
            f"with [[wikilinks]]. Do it silently, without mentioning it."
        )

    # --- Context inspection & compression ---------------------------------
    def _context_report(self, chat_id: int) -> str:
        """Build a human-readable summary of the current context fullness."""
        s = self.session.context_stats(chat_id)
        pct = s["percent"]
        filled = min(pct // 10, 10)
        bar = "█" * filled + "░" * (10 - filled)
        warn = "  ⚠️ скоро сожмётся" if pct >= 90 else ""
        return (
            "<b>контекст</b>\n"
            f"{bar} {pct}%{warn}\n"
            f"сообщений: {s['messages']}\n"
            f"~токенов: {s['tokens']} / {s['limit']}\n"
            f"из них профиль: ~{s['profile_tokens']}"
        )

    async def _compress_context(self, chat_id: int) -> str:
        """Summarize older history into one entry, keeping recent messages.

        Returns a short status string for the user.
        """
        older = self.session.messages_to_compress(
            chat_id, config.COMPRESS_KEEP_RECENT
        )
        if not older:
            return "нечего сжимать — история короткая"

        convo = "\n".join(
            f"{m.get('role', 'user')}: {m.get('content', '')}" for m in older
        )
        prompt = (
            "Сожми эту историю диалога в краткое саммари на русском. Сохрани "
            "факты, имена, числа, договорённости, незавершённые задачи и контекст. "
            "Пиши только саммари, без преамбулы и комментариев:\n\n" + convo
        )
        summary = await self._run_claude_plain(prompt)
        if not summary or summary.startswith("error"):
            logger.error("Compression failed for chat_id=%s: %s", chat_id, summary)
            return "не вышло сжать (claude не ответил), история осталась как была"

        self.session.apply_compression(
            chat_id, summary, config.COMPRESS_KEEP_RECENT
        )
        s = self.session.context_stats(chat_id)
        logger.info(
            "Compressed context chat_id=%s -> %d tokens", chat_id, s["tokens"]
        )
        return f"сжато ✅ теперь ~{s['tokens']} токенов ({s['percent']}%)"

    # --- Main entry point --------------------------------------------------
    async def handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Route a Telegram update: commands or a Claude Code conversation turn."""
        message = update.effective_message
        if message is None:
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        text = (message.text or message.caption or "").strip()

        logger.info(
            "Incoming chat_id=%s user_id=%s kind=%s text=%r",
            chat_id,
            user_id,
            self._message_kind(message),
            text[:50],
        )

        # 1. Access control.
        if not self._is_allowed(user_id):
            logger.warning("Access denied for user_id=%s", user_id)
            await message.reply_text("Access denied.")
            return

        # 2. Anything that isn't a slash-command (text, photo, voice, forward,
        #    reply, file, …) is a conversation turn.
        if not (message.text and text.startswith("/")):
            await self._handle_conversation(update, context, chat_id, text)
            return

        # 3. Commands.
        if text.startswith("/start"):
            await message.reply_text(WELCOME_MESSAGE, parse_mode=ParseMode.HTML)
            return
        if text.startswith("/help"):
            await message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.HTML)
            return
        if text.startswith("/clear"):
            self.session.clear(chat_id)
            await message.reply_text("Memory cleared.")
            return
        if text.startswith("/profile") or text.startswith("/memory"):
            profile = vault.read_profile().strip()
            if not profile:
                await message.reply_text("профиль пустой")
            else:
                safe = (
                    profile.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                await message.reply_text(
                    f"<pre>{safe}</pre>", parse_mode=ParseMode.HTML
                )
            return
        if text.startswith("/note"):
            body = text[len("/note"):].strip()
            if not body:
                await message.reply_text("что записать? `/note текст`")
                return
            try:
                rel = vault.capture(body)
                await message.reply_text(f"📥 в инбокс: {rel}")
            except OSError as exc:
                await message.reply_text(f"не вышло: {exc}")
            return
        if text.startswith("/today"):
            body = text[len("/today"):].strip()
            if body:
                try:
                    vault.append_today(body)
                    await message.reply_text("📓 дописал в дневник")
                except OSError as exc:
                    await message.reply_text(f"не вышло: {exc}")
            else:
                content = vault.read_today().strip()
                await message.reply_text(content or "дневник за сегодня пустой")
            return
        if text.startswith("/find"):
            query = text[len("/find"):].strip()
            if not query:
                await message.reply_text("что искать? `/find запрос`")
                return
            hits = vault.search(query)
            if not hits:
                await message.reply_text(f"ничего по «{query}»")
            else:
                safe = "\n".join(hits).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                await message.reply_text(f"<pre>{safe}</pre>", parse_mode=ParseMode.HTML)
            return
        if text.startswith("/usage"):
            arg = text[len("/usage"):].strip().lower()
            if arg in ("chart", "график", "graph"):
                png = usage_tracker.render_chart()
                if png is None:
                    await message.reply_text(
                        "график недоступен (matplotlib не установлен)"
                    )
                else:
                    await message.reply_photo(
                        photo=io.BytesIO(png),
                        caption="📈 токены по дням",
                    )
            else:
                await message.reply_text(
                    usage_tracker.infographic(), parse_mode=ParseMode.HTML
                )
            return
        if text.startswith("/context"):
            await message.reply_text(
                self._context_report(chat_id), parse_mode=ParseMode.HTML
            )
            return
        if text.startswith("/compress"):
            note = await message.reply_text("⏳ сжимаю историю...")
            result = await self._compress_context(chat_id)
            await self._edit(context, chat_id, note.message_id, result)
            return

        # 4. Unknown slash-command → treat as a normal conversation turn.
        await self._handle_conversation(update, context, chat_id, text)

    async def _handle_conversation(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        text: str,
    ) -> None:
        """Run a single Claude Code turn with a live thinking indicator."""
        start_time = time.monotonic()
        message = update.effective_message

        # a/b. Send the initial thinking message, tailored to the content kind.
        kind = self._message_kind(message)
        thinking_text = {
            "voice": "🎧 слушаю голосовое...",
            "audio": "🎧 слушаю аудио...",
            "video_note": "🎧 слушаю кружок...",
            "photo": "👀 смотрю картинку...",
        }.get(kind, "⏳ Thinking...")
        thinking = await message.reply_text(thinking_text)

        # c. Gather all input (text + reply/forward context + voice transcript +
        #    downloaded images/files) into one enriched message, then record it.
        text = await self._gather_input(message, text)
        self.session.add_message(chat_id, "user", text)

        # c2. Auto-compress the history if the context budget is exceeded, so
        #     the conversation never silently bloats Claude's prompt.
        if self.session.context_stats(chat_id)["tokens"] > config.CONTEXT_TOKEN_LIMIT:
            await self._edit(
                context, chat_id, thinking.message_id, "⏳ контекст переполнен, сжимаю..."
            )
            await self._compress_context(chat_id)

        # d. Build the full prompt.
        prompt = self._build_prompt(chat_id, text)

        proc: asyncio.subprocess.Process | None = None
        try:
            # e. Launch Claude Code in headless mode (JSON output carries token
            #    usage / cost metadata alongside the text).
            proc = await asyncio.create_subprocess_exec(
                *self._claude_cli_args(prompt, json_output=True),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(config.BASE_DIR),
            )

            # f. Drive the subprocess while animating the thinking message.
            stdout_b, stderr_b = await self._run_with_progress(
                proc, context, chat_id, thinking.message_id, start_time
            )

            raw_stdout = stdout_b.decode("utf-8", errors="replace").strip()
            stderr = stderr_b.decode("utf-8", errors="replace").strip()

            # Split the JSON envelope into reply text + usage; record usage.
            stdout, usage = self._parse_claude_json(raw_stdout)
            stdout = stdout.strip()
            if usage:
                usage_tracker.record(chat_id, usage)

            if proc.returncode != 0:
                # Subprocess failed; surface a trimmed stderr to the user.
                detail = stderr or stdout or "unknown error"
                logger.error(
                    "Claude Code FAILED chat_id=%s exit=%s elapsed=%.2fs: %s",
                    chat_id,
                    proc.returncode,
                    time.monotonic() - start_time,
                    detail[:500],
                )
                await self._edit(
                    context,
                    chat_id,
                    thinking.message_id,
                    f"❌ Claude Code error: {detail[:500]}",
                )
                return

            if not stdout:
                logger.warning(
                    "Claude Code EMPTY response chat_id=%s elapsed=%.2fs",
                    chat_id,
                    time.monotonic() - start_time,
                )
                await self._edit(
                    context,
                    chat_id,
                    thinking.message_id,
                    "⚠️ Claude Code returned an empty response.",
                )
                return

            # g. Success: persist, format, and deliver the response.
            self.session.add_message(chat_id, "assistant", stdout)

            # Delete the thinking message, then deliver the response. A very
            # large single code block is sent as a file instead of inlined.
            await self._delete(context, chat_id, thinking.message_id)

            if self.formatter.needs_file_upload(stdout):
                await self._send_with_file(context, chat_id, stdout)
                file_uploaded = True
            else:
                file_uploaded = False
                for chunk in self.formatter.format_for_telegram(stdout):
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        parse_mode=ParseMode.HTML,
                    )

            elapsed = time.monotonic() - start_time
            tok = (
                f" in={usage['input']} out={usage['output']} "
                f"cost=${usage['cost']:.4f}"
                if usage
                else ""
            )
            logger.info(
                "Response sent chat_id=%s length=%d file=%s elapsed=%.2fs%s",
                chat_id,
                len(stdout),
                file_uploaded,
                elapsed,
                tok,
            )

        except asyncio.TimeoutError:
            logger.warning(
                "Claude Code TIMEOUT chat_id=%s after %ss (elapsed=%.2fs)",
                chat_id,
                config.CLAUDE_TIMEOUT,
                time.monotonic() - start_time,
            )
            await self._edit(
                context,
                chat_id,
                thinking.message_id,
                f"⚠️ Request timed out after {config.CLAUDE_TIMEOUT}s. "
                f"Claude Code took too long.",
            )
        except Exception as exc:  # noqa: BLE001 — report any failure to the user.
            logger.error(
                "Unhandled error in conversation chat_id=%s (elapsed=%.2fs):\n%s",
                chat_id,
                time.monotonic() - start_time,
                traceback.format_exc(),
            )
            await self._edit(
                context,
                chat_id,
                thinking.message_id,
                f"❌ Error: {exc}",
            )
        finally:
            # Always make sure the subprocess is not left running.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass

    # --- Subprocess driving ------------------------------------------------
    async def _run_with_progress(
        self,
        proc: asyncio.subprocess.Process,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int,
        start_time: float,
    ) -> tuple[bytes, bytes]:
        """Await the subprocess, animating the thinking message until done.

        Raises :class:`asyncio.TimeoutError` (after killing the process) once
        ``CLAUDE_TIMEOUT`` seconds elapse without completion.
        """
        comm_task = asyncio.ensure_future(proc.communicate())
        dots = 3  # "⏳ Thinking..." already shows three dots.

        while True:
            done, _ = await asyncio.wait(
                {comm_task}, timeout=config.THINKING_UPDATE_INTERVAL
            )
            if comm_task in done:
                return comm_task.result()

            elapsed = time.monotonic() - start_time
            if elapsed >= config.CLAUDE_TIMEOUT:
                proc.kill()
                # Drain the pipes so the transport closes cleanly.
                try:
                    await comm_task
                except Exception:  # noqa: BLE001
                    pass
                raise asyncio.TimeoutError

            # Animate by growing the dot trail; Telegram rejects no-op edits, so
            # the text must actually change each tick.
            dots += 1
            await self._edit(
                context,
                chat_id,
                message_id,
                "⏳ Thinking" + ("." * dots),
            )

    async def _send_with_file(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        stdout: str,
    ) -> None:
        """Send a response whose largest code block is uploaded as a file.

        The oversized code block is removed from the prose (replaced by a short
        placeholder), the remaining text is sent as normal HTML chunks, and the
        code is attached as a ``response.<ext>`` document.
        """
        remaining, code, lang = self.formatter.pop_largest_code_block(stdout)

        # Send the surrounding prose first (if any meaningful text remains).
        if remaining.strip():
            for chunk in self.formatter.format_for_telegram(remaining):
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                )

        # Upload the code as an in-memory document.
        buffer = io.BytesIO(code.encode("utf-8"))
        buffer.name = f"response.{lang}"
        await context.bot.send_document(
            chat_id=chat_id,
            document=buffer,
            filename=buffer.name,
        )

    # --- Telegram helpers (tolerant of edit/delete races) ------------------
    async def _edit(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> None:
        """Edit a message, ignoring benign Telegram errors (e.g. no change)."""
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text
            )
        except TelegramError as exc:
            logger.debug("Edit message failed (ignored): %s", exc)

    async def _delete(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int,
    ) -> None:
        """Delete a message, ignoring benign Telegram errors."""
        try:
            await context.bot.delete_message(
                chat_id=chat_id, message_id=message_id
            )
        except TelegramError as exc:
            logger.debug("Delete message failed (ignored): %s", exc)
