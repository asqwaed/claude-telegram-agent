"""Custom local MCP server for the claude-agent project (fastmcp).

Exposes a curated set of local capabilities to the agent that aren't covered by
the off-the-shelf MCP servers:

* **filesystem** — sandboxed read/write/list and shell execution, restricted to
  a whitelist of directories and guarded against a few catastrophic commands.
* **telegram** — proactively send a message or upload a file to the user's chat.
* **gmail** — read, search, read-full and send email via the Google API.
* **calendar** — list upcoming events and create new ones.

All Google tools share the OAuth token produced by
``credentials/google_auth.py``; run that once before using them.

Run standalone (this is what ~/.claude/mcp.json invokes)::

    python mcp/server.py
"""

import base64
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

# macOS python.org builds ship without root certs; use certifi's bundle so
# outbound HTTPS (Telegram API) verifies correctly.
try:
    import certifi

    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

# Make the project root importable so we can reuse config.py.
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import config  # noqa: E402
from fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("local-tools")

# --- Sandbox configuration -------------------------------------------------
HOME_DIR = Path.home()
ALLOWED_DIRS = [
    BASE_DIR,
    HOME_DIR / "Documents",
    HOME_DIR / "Desktop",
    HOME_DIR / "Downloads",
    HOME_DIR / "projects",
]

BLOCKED_COMMANDS = ["rm -rf /", "sudo rm", "mkfs", "dd if=", ":(){:|:&};:"]
COMMAND_TIMEOUT = 30
MAX_OUTPUT_CHARS = 10000


def is_allowed(path: str) -> bool:
    """Return True if ``path`` resolves inside one of the whitelisted dirs."""
    resolved = Path(path).resolve()
    return any(
        str(resolved).startswith(str(allowed.resolve()))
        for allowed in ALLOWED_DIRS
    )


# ===========================================================================
# Filesystem tools
# ===========================================================================
@mcp.tool()
def read_file(path: str) -> str:
    """Read a text file inside the allowed directories and return its contents.

    Falls back from utf-8 to latin-1 on decode errors so binary-ish files still
    return something rather than raising.
    """
    if not is_allowed(path):
        return f"error: path {path} is outside allowed directories"
    p = Path(path)
    if not p.exists():
        return f"error: file {path} does not exist"
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return p.read_text(encoding="latin-1")
        except OSError as exc:
            return f"error: {exc}"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write ``content`` to ``path`` (inside allowed dirs), creating parents."""
    if not is_allowed(path):
        return f"error: path {path} is outside allowed directories"
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        p.write_bytes(data)
        return f"ok: wrote {len(data)} bytes to {path}"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def list_directory(path: str) -> str:
    """List a directory tree (max 3 levels) inside the allowed directories.

    Directories are shown with a trailing ``/``; files include their size in KB.
    """
    if not is_allowed(path):
        return f"error: path {path} is outside allowed directories"
    root = Path(path)
    if not root.exists():
        return f"error: directory {path} does not exist"
    if not root.is_dir():
        return f"error: {path} is not a directory"

    lines: list[str] = [f"{root}/"]

    def walk(directory: Path, depth: int, prefix: str) -> None:
        if depth > 3:
            return
        try:
            entries = sorted(
                directory.iterdir(),
                key=lambda e: (e.is_file(), e.name.lower()),
            )
        except OSError as exc:
            lines.append(f"{prefix}  <error: {exc}>")
            return
        for entry in entries:
            if entry.is_dir():
                lines.append(f"{prefix}  {entry.name}/")
                walk(entry, depth + 1, prefix + "  ")
            else:
                try:
                    kb = entry.stat().st_size / 1024
                except OSError:
                    kb = 0.0
                lines.append(f"{prefix}  {entry.name} ({kb:.1f} KB)")

    walk(root, 1, "")
    return "\n".join(lines)


@mcp.tool()
def run_command(command: str, cwd: str = None) -> str:
    """Run a shell command (30s timeout) and return combined stdout+stderr.

    ``cwd`` defaults to the project root and must be within the allowed dirs.
    A small set of catastrophic command patterns is refused outright. Output is
    truncated to 10000 characters.
    """
    for blocked in BLOCKED_COMMANDS:
        if blocked in command:
            return f"error: command contains blocked pattern '{blocked}'"

    if cwd is not None:
        if not is_allowed(cwd):
            return f"error: path {cwd} is outside allowed directories"
        workdir = cwd
    else:
        workdir = str(BASE_DIR)

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {COMMAND_TIMEOUT}s"
    except OSError as exc:
        return f"error: {exc}"

    output = (result.stdout or "") + (result.stderr or "")
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n...[truncated]"
    return output if output.strip() else f"(no output, exit code {result.returncode})"


# ===========================================================================
# Telegram tools
# ===========================================================================
def _post_telegram(message: str) -> str:
    """Low-level send of a text message to the user's chat. 'sent' or error str."""
    token = config.TELEGRAM_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return "error: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not configured"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": message}
    ).encode("utf-8")
    try:
        with urllib.request.urlopen(
            url, data=data, timeout=30, context=_SSL_CONTEXT
        ) as resp:
            if resp.status == 200:
                return "sent"
            return f"error: HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return f"error: {exc.code} {exc.read().decode('utf-8', 'replace')[:200]}"
    except urllib.error.URLError as exc:
        return f"error: {exc.reason}"


def _notify_vault(action: str, rel_path) -> None:
    """Best-effort separate Telegram ping when the agent writes to the vault."""
    try:
        _post_telegram(f"🧠 {action}: {rel_path}")
    except Exception:  # noqa: BLE001 — notifications must never break a write.
        pass


@mcp.tool()
def send_telegram(message: str) -> str:
    """Send a text message to the user's Telegram chat. Returns 'sent' or error."""
    return _post_telegram(message)


@mcp.tool()
def send_telegram_file(file_path: str, caption: str = "") -> str:
    """Upload a local file (inside allowed dirs) to the user's Telegram chat."""
    if not is_allowed(file_path):
        return f"error: path {file_path} is outside allowed directories"
    p = Path(file_path)
    if not p.exists():
        return f"error: file {file_path} does not exist"

    token = config.TELEGRAM_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return "error: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not configured"

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    # Build a multipart/form-data body manually (no extra dependencies).
    boundary = "----claudeagentboundary"
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        parts.append(f"{value}\r\n".encode())

    field("chat_id", str(chat_id))
    if caption:
        field("caption", caption)

    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="document"; '
        f'filename="{p.name}"\r\n'.encode()
    )
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    try:
        parts.append(p.read_bytes())
    except OSError as exc:
        return f"error: {exc}"
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(url, data=body)
    req.add_header(
        "Content-Type", f"multipart/form-data; boundary={boundary}"
    )
    try:
        with urllib.request.urlopen(
            req, timeout=60, context=_SSL_CONTEXT
        ) as resp:
            if resp.status == 200:
                return f"sent: {p.name}"
            return f"error: HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return f"error: {exc.code} {exc.read().decode('utf-8', 'replace')[:200]}"
    except urllib.error.URLError as exc:
        return f"error: {exc.reason}"


# ===========================================================================
# Graphviz tool — render a DOT graph and deliver the image to Telegram
# ===========================================================================
_GRAPHVIZ_ENGINES = {"dot", "neato", "fdp", "sfdp", "circo", "twopi"}
_GRAPHVIZ_FORMATS = {"png", "svg", "pdf", "jpg", "gif"}
# Formats Telegram renders inline as a photo; everything else goes as a file.
_PHOTO_FORMATS = {"png", "jpg", "gif"}
# brew installs the binaries here; the MCP server's PATH may not include them.
_GRAPHVIZ_BIN_DIRS = ["/opt/homebrew/bin", "/usr/local/bin"]


def _resolve_engine(engine: str):
    """Return the absolute path to a Graphviz layout binary, or None."""
    found = shutil.which(engine)
    if found:
        return found
    for d in _GRAPHVIZ_BIN_DIRS:
        cand = Path(d) / engine
        if cand.exists():
            return str(cand)
    return None


def _upload_telegram_media(method: str, field_name: str, p: Path,
                           caption: str) -> str:
    """Upload a local file to the user's chat via ``method`` (sendPhoto/Document).

    Builds the multipart/form-data body by hand (no extra deps), mirroring
    ``send_telegram_file``. Returns 'sent: <name>' or an error string.
    """
    token = config.TELEGRAM_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return "error: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not configured"

    url = f"https://api.telegram.org/bot{token}/{method}"
    boundary = "----claudeagentboundary"
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        parts.append(f"{value}\r\n".encode())

    field("chat_id", str(chat_id))
    if caption:
        field("caption", caption)

    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{field_name}"; '
        f'filename="{p.name}"\r\n'.encode()
    )
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    try:
        parts.append(p.read_bytes())
    except OSError as exc:
        return f"error: {exc}"
    parts.append(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(url, data=b"".join(parts))
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(
            req, timeout=60, context=_SSL_CONTEXT
        ) as resp:
            if resp.status == 200:
                return f"sent: {p.name}"
            return f"error: HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return f"error: {exc.code} {exc.read().decode('utf-8', 'replace')[:200]}"
    except urllib.error.URLError as exc:
        return f"error: {exc.reason}"


@mcp.tool()
def graphviz_render(dot_source: str, caption: str = "", engine: str = "dot",
                    fmt: str = "png") -> str:
    """Render a Graphviz DOT graph to an image and send it to the user's chat.

    Use this whenever the user asks for a diagram, chart, graph, tree, flow,
    schema, state machine, or dependency map — write the graph in the DOT
    language and this renders it and delivers the picture to Telegram directly.

    Args:
        dot_source: the graph in DOT, e.g. 'digraph G { A -> B; B -> C }'.
        caption: optional caption shown under the image.
        engine: layout engine — dot (hierarchy), neato/fdp (force), circo
            (circular), twopi (radial). Default 'dot'.
        fmt: output format — png (default, inline photo), svg/pdf (sent as a
            file), jpg, gif.

    Returns 'sent: <name>' on success, or an 'error: ...' string.
    """
    engine = (engine or "dot").strip().lower()
    fmt = (fmt or "png").strip().lower()
    if engine not in _GRAPHVIZ_ENGINES:
        return (f"error: unknown engine '{engine}'; "
                f"use one of {sorted(_GRAPHVIZ_ENGINES)}")
    if fmt not in _GRAPHVIZ_FORMATS:
        return (f"error: unknown format '{fmt}'; "
                f"use one of {sorted(_GRAPHVIZ_FORMATS)}")
    if not (dot_source or "").strip():
        return "error: dot_source is empty"

    binary = _resolve_engine(engine)
    if not binary:
        return (f"error: '{engine}' not found — install Graphviz "
                f"(brew install graphviz)")

    tmpdir = Path(tempfile.mkdtemp(prefix="gv_"))
    src = tmpdir / "graph.dot"
    out = tmpdir / f"graph.{fmt}"
    try:
        src.write_text(dot_source, encoding="utf-8")
        proc = subprocess.run(
            [binary, f"-T{fmt}", str(src), "-o", str(out)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0 or not out.exists():
            err = (proc.stderr or proc.stdout or "unknown error").strip()
            return f"error: graphviz failed: {err[:400]}"
        if fmt in _PHOTO_FORMATS:
            return _upload_telegram_media("sendPhoto", "photo", out, caption)
        return _upload_telegram_media("sendDocument", "document", out, caption)
    except subprocess.TimeoutExpired:
        return "error: graphviz timed out (graph too large?)"
    except OSError as exc:
        return f"error: {exc}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# Obsidian vault tools (file-based knowledge base)
# ===========================================================================
# The vault is plain .md files under config.VAULT_DIR (inside Documents, which
# is already whitelisted). Relationships are expressed as [[wikilinks]] inside
# notes — this replaces the old JSON knowledge graph.


def _find_note(name: str):
    """Resolve a note name/title/relative path to an existing .md Path or None."""
    name = (name or "").strip()
    if not name:
        return None
    direct = config.VAULT_DIR / (name if name.endswith(".md") else name + ".md")
    if direct.exists():
        return direct
    stem = Path(name).stem.lower()
    for p in config.VAULT_DIR.rglob("*.md"):
        if p.stem.lower() == stem:
            return p
    return None


@mcp.tool()
def obs_capture(text: str, title: str = "") -> str:
    """Quick-capture a note into the vault's Inbox (00 Inbox).

    A timestamp header is added. ``title`` optionally names the file; otherwise
    a slug from the timestamp is used. Returns the created path.
    """
    now = datetime.now()
    if title.strip():
        fname = title.strip().replace("/", "-") + ".md"
    else:
        fname = now.strftime("%Y-%m-%d_%H%M%S") + ".md"
    path = config.INBOX_DIR / fname
    body = f"---\ncreated: {now.isoformat(timespec='seconds')}\n---\n{text}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        rel = path.relative_to(config.VAULT_DIR)
        _notify_vault("captured", rel)
        return f"captured: {rel}"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def obs_daily(text: str = "") -> str:
    """Today's daily note. With ``text`` → append a timestamped line; else read it.

    File: Journal/YYYY-MM-DD.md (created on first append).
    """
    today = datetime.now().strftime("%Y-%m-%d")
    path = config.JOURNAL_DIR / f"{today}.md"
    if not text.strip():
        if not path.exists():
            return f"(daily note {today} is empty)"
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"error: {exc}"
    stamp = datetime.now().strftime("%H:%M")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(f"# {today}\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"- {stamp} {text}\n")
        _notify_vault("daily", f"Journal/{today}.md")
        return f"appended to Journal/{today}.md"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def obs_search(query: str, limit: int = 20) -> str:
    """Full-text search across the vault. Returns file:line snippets."""
    q = query.lower()
    hits = []
    for p in sorted(config.VAULT_DIR.rglob("*.md")):
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if q in line.lower():
                    rel = p.relative_to(config.VAULT_DIR)
                    hits.append(f"{rel}:{i}: {line.strip()[:160]}")
                    if len(hits) >= limit:
                        return "\n".join(hits)
        except OSError:
            continue
    return "\n".join(hits) if hits else f"(nothing found for '{query}')"


@mcp.tool()
def obs_read(note: str) -> str:
    """Read a note by title or relative path (truncated to 8000 chars)."""
    path = _find_note(note)
    if path is None:
        return f"error: note '{note}' not found"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"error: {exc}"
    return text[:8000] + ("\n...[truncated]" if len(text) > 8000 else "")


@mcp.tool()
def obs_write(note: str, content: str, folder: str = "00 Inbox") -> str:
    """Create or OVERWRITE a note. ``note`` is the title; ``folder`` its location.

    Ask the user before overwriting an existing note.
    """
    existing = _find_note(note)
    path = existing or (config.VAULT_DIR / folder / (note + ".md"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        verb = "overwrote" if existing else "created"
        rel = path.relative_to(config.VAULT_DIR)
        _notify_vault(verb, rel)
        return f"{verb}: {rel}"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def obs_append(note: str, text: str, folder: str = "00 Inbox") -> str:
    """Append text to a note (created under ``folder`` if it doesn't exist)."""
    path = _find_note(note) or (config.VAULT_DIR / folder / (note + ".md"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        prefix = "" if not existed else "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(prefix + text + "\n")
        rel = path.relative_to(config.VAULT_DIR)
        _notify_vault("appended" if existed else "created", rel)
        return f"appended: {rel}"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def obs_backlinks(note: str) -> str:
    """List notes that link to ``note`` via [[wikilinks]]."""
    target = Path(note).stem.lower()
    found = []
    for p in sorted(config.VAULT_DIR.rglob("*.md")):
        try:
            content = p.read_text(encoding="utf-8").lower()
        except OSError:
            continue
        if f"[[{target}" in content:
            found.append(str(p.relative_to(config.VAULT_DIR)))
    return "\n".join(found) if found else f"(no backlinks to '{note}')"


@mcp.tool()
def obs_list(folder: str = "") -> str:
    """List notes in the vault (optionally within a subfolder)."""
    root = config.VAULT_DIR / folder if folder else config.VAULT_DIR
    if not root.exists():
        return f"error: folder '{folder}' not found"
    notes = sorted(
        str(p.relative_to(config.VAULT_DIR)) for p in root.rglob("*.md")
    )
    return "\n".join(notes) if notes else "(no notes)"


# ===========================================================================
# Telegram MTProto tools (user accounts, via Telethon)
# ===========================================================================
# These act as one of the real user accounts configured in config.TELEGRAM_
# ACCOUNTS (default: the first alias). Each call opens a short-lived Telethon
# client using the saved per-account session, performs the op, and disconnects.

import asyncio  # noqa: E402
import threading  # noqa: E402

_WHISPER_MODEL = None  # lazily-loaded faster-whisper model (shared)


def _run_async(coro):
    """Run a coroutine to completion even if an event loop is already running.

    fastmcp may invoke sync tools from within its own event loop; ``asyncio.run``
    would raise there. In that case we run the coroutine in a dedicated thread
    with its own loop (Telethon creates and uses its client inside that loop).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict = {}

    def worker():
        try:
            box["result"] = asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["result"]


def _get_whisper():
    """Load and cache the faster-whisper model for voice transcription."""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel

        _WHISPER_MODEL = WhisperModel(
            config.WHISPER_MODEL, device="cpu", compute_type="int8"
        )
    return _WHISPER_MODEL


def _resolve_tg_account(account: str | None):
    """Resolve an account alias to (alias, creds, session_path) or error str."""
    alias = (account or config.DEFAULT_TELEGRAM_ACCOUNT).strip().lower()
    acct = config.TELEGRAM_ACCOUNTS.get(alias)
    if acct is None:
        return (
            f"error: unknown telegram account '{account}'. "
            f"valid: {', '.join(config.TELEGRAM_ACCOUNTS) or '(none)'}"
        )
    if not (acct["api_id"] and acct["api_hash"]):
        return f"error: account '{alias}' missing api_id/api_hash in .env"
    session = config.telegram_session_path(alias)
    if not session.exists():
        return (
            f"error: account '{alias}' not authorized; "
            f"run python credentials/telegram_auth.py {alias}"
        )
    return (alias, acct, session)


def _run_telegram(account: str | None, coro_func):
    """Run ``coro_func(client)`` against an authorized client; manage lifecycle.

    Returns whatever the coroutine returns, or a human-readable ``error: ...``
    string on any setup/connection failure.
    """
    resolved = _resolve_tg_account(account)
    if isinstance(resolved, str):
        return resolved
    alias, acct, session = resolved

    from telethon import TelegramClient

    async def runner():
        client = TelegramClient(
            str(session).removesuffix(".session"),
            int(acct["api_id"]),
            acct["api_hash"],
        )
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return (
                    f"error: account '{alias}' session invalid; "
                    f"re-run python credentials/telegram_auth.py {alias}"
                )
            return await coro_func(client)
        finally:
            await client.disconnect()

    try:
        return _run_async(runner())
    except Exception as exc:  # noqa: BLE001
        return f"error: telegram ({type(exc).__name__}): {exc}"


async def _resolve_entity(client, chat: str):
    """Resolve a chat reference (username/phone/id/name) to a Telethon entity."""
    # Direct resolution (username, phone, t.me link, or numeric id string).
    for candidate in (chat, None):
        if candidate is None:
            break
        try:
            return await client.get_entity(candidate)
        except Exception:  # noqa: BLE001
            pass
    try:
        return await client.get_entity(int(chat))
    except Exception:  # noqa: BLE001
        pass
    # Fall back to a case-insensitive name search over the dialog list.
    needle = str(chat).lower()
    async for dialog in client.iter_dialogs():
        if needle in (dialog.name or "").lower():
            return dialog.entity
    return None


async def _sender_label(message) -> str:
    """Best-effort human label for a message's sender."""
    try:
        sender = await message.get_sender()
    except Exception:  # noqa: BLE001
        sender = None
    if not sender:
        return str(getattr(message, "sender_id", "?"))
    return (
        getattr(sender, "first_name", None)
        or getattr(sender, "title", None)
        or getattr(sender, "username", None)
        or str(getattr(message, "sender_id", "?"))
    )


async def _format_message(message) -> str:
    """Render one message as a single line: [id] date sender [flags]: text."""
    who = await _sender_label(message)
    flags = ""
    if getattr(message, "forward", None):
        try:
            src = await message.forward.get_sender() if message.forward else None
            src_name = (
                getattr(src, "first_name", None)
                or getattr(src, "title", None)
                or getattr(src, "username", None)
                if src
                else None
            )
        except Exception:  # noqa: BLE001
            src_name = None
        flags += f" [forwarded{' from ' + src_name if src_name else ''}]"
    text = message.text or ""
    if not text and message.media:
        text = f"<{type(message.media).__name__}>"
    date = message.date.strftime("%Y-%m-%d %H:%M") if message.date else ""
    return f"[{message.id}] {date} {who}{flags}: {text}"


@mcp.tool()
def tg_list_dialogs(limit: int = 20, unread_only: bool = False, account: str = "") -> str:
    """List Telegram chats (most recent first) with unread counts.

    ``account``: which user account ("" → default). ``unread_only`` keeps only
    chats with unread messages. Returns one line per chat: name, kind, unread.
    """
    async def coro(client):
        lines = []
        async for d in client.iter_dialogs(limit=None if unread_only else limit):
            if unread_only and not d.unread_count:
                continue
            kind = "channel" if d.is_channel else "group" if d.is_group else "user"
            unread = f" | unread: {d.unread_count}" if d.unread_count else ""
            lines.append(f"{d.name} [{kind}]{unread}")
            if len(lines) >= limit:
                break
        return "\n".join(lines) if lines else "(no chats)"

    return _run_telegram(account, coro)


@mcp.tool()
def tg_read_chat(chat: str, limit: int = 20, account: str = "") -> str:
    """Read the most recent messages of a chat (by username/id/name).

    Forwarded messages are flagged with their original sender. ``account``: ""
    → default. Returns oldest→newest, one message per line.
    """
    async def coro(client):
        entity = await _resolve_entity(client, chat)
        if entity is None:
            return f"error: chat '{chat}' not found"
        msgs = []
        async for m in client.iter_messages(entity, limit=limit):
            msgs.append(await _format_message(m))
        return "\n".join(reversed(msgs)) if msgs else "(no messages)"

    return _run_telegram(account, coro)


@mcp.tool()
def tg_search(query: str, chat: str = "", limit: int = 20, account: str = "") -> str:
    """Search messages by text, globally or within one chat.

    Leave ``chat`` empty to search across all chats. ``account``: "" → default.
    """
    async def coro(client):
        entity = None
        if chat:
            entity = await _resolve_entity(client, chat)
            if entity is None:
                return f"error: chat '{chat}' not found"
        msgs = []
        async for m in client.iter_messages(entity, search=query, limit=limit):
            label = await _format_message(m)
            if entity is None:
                # Add chat context for global search.
                try:
                    ch = await m.get_chat()
                    cname = getattr(ch, "title", None) or getattr(ch, "first_name", "?")
                except Exception:  # noqa: BLE001
                    cname = "?"
                label = f"{{{cname}}} {label}"
            msgs.append(label)
        return "\n".join(msgs) if msgs else f"(nothing found for '{query}')"

    return _run_telegram(account, coro)


@mcp.tool()
def tg_unread_summary(max_chats: int = 15, per_chat: int = 6, account: str = "") -> str:
    """Collect unread messages across chats so the agent can summarize them.

    Returns up to ``per_chat`` recent messages per unread chat, grouped by chat.
    ``account``: "" → default.
    """
    async def coro(client):
        blocks = []
        async for d in client.iter_dialogs():
            if not d.unread_count:
                continue
            n = min(d.unread_count, per_chat)
            msgs = []
            async for m in client.iter_messages(d.entity, limit=n):
                msgs.append(await _format_message(m))
            header = f"=== {d.name} ({d.unread_count} unread) ==="
            blocks.append(header + "\n" + "\n".join(reversed(msgs)))
            if len(blocks) >= max_chats:
                break
        return "\n\n".join(blocks) if blocks else "(no unread messages)"

    return _run_telegram(account, coro)


@mcp.tool()
def tg_channel_digest(per_channel: int = 4, max_channels: int = 12, account: str = "") -> str:
    """Recent posts from the broadcast channels you're subscribed to.

    ``account``: "" → default. Returns up to ``per_channel`` latest posts per
    channel, grouped by channel.
    """
    async def coro(client):
        blocks = []
        async for d in client.iter_dialogs():
            ent = d.entity
            if not (d.is_channel and getattr(ent, "broadcast", False)):
                continue
            msgs = []
            async for m in client.iter_messages(ent, limit=per_channel):
                msgs.append(await _format_message(m))
            blocks.append(f"=== {d.name} ===\n" + "\n".join(reversed(msgs)))
            if len(blocks) >= max_channels:
                break
        return "\n\n".join(blocks) if blocks else "(no channels)"

    return _run_telegram(account, coro)


@mcp.tool()
def tg_transcribe_voice(chat: str, message_id: int, account: str = "") -> str:
    """Download a voice/audio/round-video message and transcribe it to text.

    Uses local faster-whisper (config.WHISPER_MODEL). ``account``: "" → default.
    """
    import os
    import tempfile

    async def coro(client):
        entity = await _resolve_entity(client, chat)
        if entity is None:
            return f"error: chat '{chat}' not found"
        msg = await client.get_messages(entity, ids=message_id)
        if not msg or not msg.media:
            return "error: that message has no downloadable media"
        tmp = tempfile.mktemp(suffix=".ogg")
        path = await client.download_media(msg, file=tmp)
        return ("FILE", path)

    res = _run_telegram(account, coro)
    if isinstance(res, str):
        return res
    _, path = res
    try:
        model = _get_whisper()
        segments, _info = model.transcribe(path)
        text = " ".join(s.text.strip() for s in segments).strip()
        return text or "(silence / nothing recognized)"
    except Exception as exc:  # noqa: BLE001
        return f"error: transcription failed ({exc})"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@mcp.tool()
def tg_send(chat: str, text: str, account: str = "") -> str:
    """Send a message AS THE USER to a chat. Ask the user before calling.

    ``chat``: username/id/name. ``account``: "" → default. Returns 'sent to ...'.
    """
    async def coro(client):
        entity = await _resolve_entity(client, chat)
        if entity is None:
            return f"error: chat '{chat}' not found"
        m = await client.send_message(entity, text)
        return f"sent to {chat} (msg {m.id})"

    return _run_telegram(account, coro)


@mcp.tool()
def tg_reply(chat: str, message_id: int, text: str, account: str = "") -> str:
    """Reply AS THE USER to a specific message. Ask the user before calling."""
    async def coro(client):
        entity = await _resolve_entity(client, chat)
        if entity is None:
            return f"error: chat '{chat}' not found"
        m = await client.send_message(entity, text, reply_to=message_id)
        return f"replied in {chat} (msg {m.id})"

    return _run_telegram(account, coro)


@mcp.tool()
def tg_react(chat: str, message_id: int, emoji: str = "👍", account: str = "") -> str:
    """Set an emoji reaction on a message AS THE USER. Pass emoji='' to clear."""
    async def coro(client):
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji

        entity = await _resolve_entity(client, chat)
        if entity is None:
            return f"error: chat '{chat}' not found"
        reaction = [ReactionEmoji(emoticon=emoji)] if emoji else None
        await client(
            SendReactionRequest(peer=entity, msg_id=message_id, reaction=reaction)
        )
        return f"reacted {emoji or '(cleared)'} on msg {message_id}"

    return _run_telegram(account, coro)


@mcp.tool()
def tg_forward(from_chat: str, message_id: int, to_chat: str, account: str = "") -> str:
    """Forward a message from one chat to another AS THE USER. Confirm first."""
    async def coro(client):
        src = await _resolve_entity(client, from_chat)
        dst = await _resolve_entity(client, to_chat)
        if src is None:
            return f"error: chat '{from_chat}' not found"
        if dst is None:
            return f"error: chat '{to_chat}' not found"
        await client.forward_messages(dst, message_id, src)
        return f"forwarded msg {message_id}: {from_chat} -> {to_chat}"

    return _run_telegram(account, coro)


@mcp.tool()
def tg_edit(chat: str, message_id: int, text: str, account: str = "") -> str:
    """Edit one of your own messages. Confirm before calling."""
    async def coro(client):
        entity = await _resolve_entity(client, chat)
        if entity is None:
            return f"error: chat '{chat}' not found"
        await client.edit_message(entity, message_id, text)
        return f"edited msg {message_id} in {chat}"

    return _run_telegram(account, coro)


@mcp.tool()
def tg_delete(chat: str, message_id: int, account: str = "") -> str:
    """Delete a message AS THE USER. Destructive — confirm before calling."""
    async def coro(client):
        entity = await _resolve_entity(client, chat)
        if entity is None:
            return f"error: chat '{chat}' not found"
        await client.delete_messages(entity, [message_id])
        return f"deleted msg {message_id} in {chat}"

    return _run_telegram(account, coro)


@mcp.tool()
def tg_mark_read(chat: str, account: str = "") -> str:
    """Mark a chat's messages as read AS THE USER."""
    async def coro(client):
        entity = await _resolve_entity(client, chat)
        if entity is None:
            return f"error: chat '{chat}' not found"
        await client.send_read_acknowledge(entity)
        return f"marked {chat} as read"

    return _run_telegram(account, coro)


if __name__ == "__main__":
    mcp.run()
