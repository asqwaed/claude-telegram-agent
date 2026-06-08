"""Markdown-to-Telegram-HTML formatting, splitting, and file-upload helpers.

Claude Code emits standard Markdown. Telegram's ``HTML`` parse mode supports
only a small subset of tags (``<b>``, ``<i>``, ``<code>``, ``<pre>``, ...), and
requires that any literal ``<``, ``>`` and ``&`` outside of those tags be
escaped. :class:`MessageFormatter` converts the common Markdown constructs to
that subset, splits the result into Telegram-sized chunks without ever cutting a
fenced code block or an inline tag in half, and decides when a giant code block
should be uploaded as a file instead of inlined.
"""

import re


class MessageFormatter:
    """Converts Markdown to Telegram HTML, splits it, and prepares uploads."""

    # Telegram hard limit on a single text message.
    MAX_MESSAGE_LENGTH = 4096
    # Code blocks longer than this are sent as a .txt file instead of inline.
    MAX_CODE_BLOCK_LENGTH = 3500

    # Fenced code blocks: ```lang\n ... \n``` (lang optional). DOTALL so the
    # body may span multiple lines; non-greedy so adjacent blocks don't merge.
    CODE_BLOCK_PATTERN = re.compile(
        r"```([A-Za-z0-9_+\-]*)[ \t]*\n?(.*?)```",
        re.DOTALL,
    )

    # Inline code: `code` (single backtick, no embedded backtick/newline).
    _INLINE_CODE_PATTERN = re.compile(r"`([^`\n]+?)`")
    # Bold: **text** or __text__.
    _BOLD_PATTERN = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
    # Italic with * — must not be adjacent to other * (that's bold/leftover).
    _ITALIC_STAR_PATTERN = re.compile(
        r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])"
    )
    # Italic with _ — only when bordered by non-word chars / string ends, so
    # snake_case identifiers like my_var_name are left untouched.
    _ITALIC_UNDERSCORE_PATTERN = re.compile(
        r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])"
    )

    # Matches a fully-formed <pre>...</pre> block in already-rendered HTML.
    _PRE_BLOCK_PATTERN = re.compile(r"<pre>.*?</pre>", re.DOTALL)

    @staticmethod
    def _escape(text: str) -> str:
        """Escape the three HTML-significant characters for Telegram."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _render_inline(self, text: str) -> str:
        """Render inline Markdown (code/bold/italic) within a plain segment.

        Inline code is stashed first so its contents are escaped exactly once
        and never interpreted as emphasis. The remaining text is escaped (this
        happens BEFORE any HTML tags are inserted, avoiding double-escaping),
        then bold and italic markers are rewritten to Telegram tags.
        """
        # 1. Stash inline code spans behind placeholders.
        code_spans: list[str] = []

        def _stash(match: re.Match) -> str:
            code_spans.append(match.group(1))
            return f"\x00CODE{len(code_spans) - 1}\x00"

        text = self._INLINE_CODE_PATTERN.sub(_stash, text)

        # 2. Escape bare & < > before inserting any HTML tags.
        text = self._escape(text)

        # 3. Bold first (so ** is consumed before single-* italic runs).
        text = self._BOLD_PATTERN.sub(lambda m: f"<b>{m.group(2)}</b>", text)
        # 4. Italic: * variant, then _ variant (snake_case safe).
        text = self._ITALIC_STAR_PATTERN.sub(lambda m: f"<i>{m.group(1)}</i>", text)
        text = self._ITALIC_UNDERSCORE_PATTERN.sub(
            lambda m: f"<i>{m.group(1)}</i>", text
        )

        # 5. Restore inline code, escaping its contents.
        def _restore(match: re.Match) -> str:
            idx = int(match.group(1))
            return f"<code>{self._escape(code_spans[idx])}</code>"

        return re.sub(r"\x00CODE(\d+)\x00", _restore, text)

    def _strip_code_fence(self, match: re.Match) -> tuple[str, str]:
        """Return (lang, code) for a fenced-block regex match, fence trimmed."""
        lang = match.group(1).strip()
        code = match.group(2)
        if code.endswith("\n"):
            code = code[:-1]
        return lang, code

    def _to_html(self, text: str) -> str:
        """Convert a full Markdown string to Telegram HTML."""
        out: list[str] = []
        last_end = 0
        for match in self.CODE_BLOCK_PATTERN.finditer(text):
            out.append(self._render_inline(text[last_end:match.start()]))
            lang, code = self._strip_code_fence(match)
            escaped = self._escape(code)
            if lang:
                out.append(
                    f'<pre><code class="language-{lang}">{escaped}</code></pre>'
                )
            else:
                out.append(f"<pre><code>{escaped}</code></pre>")
            last_end = match.end()
        out.append(self._render_inline(text[last_end:]))
        return "".join(out)

    def format_for_telegram(self, text: str) -> list[str]:
        """Convert Markdown ``text`` to a list of Telegram-ready HTML chunks.

        Each returned string is valid Telegram HTML no longer than
        ``MAX_MESSAGE_LENGTH``, never splits a ``<pre>`` block or an inline tag
        across chunks, and prefers paragraph boundaries when splitting prose.
        """
        if not text or not text.strip():
            return ["<i>(пусто)</i>"]
        html = self._to_html(text)
        return self.split_long_message(html)

    # --- Splitting ---------------------------------------------------------
    @staticmethod
    def _hard_split(chunk: str, max_len: int) -> list[str]:
        """Split a plain (non-<pre>) HTML string into <= max_len pieces.

        Prefers a paragraph break (double newline), then a single newline, then
        a raw length cut as a last resort.
        """
        pieces: list[str] = []
        while len(chunk) > max_len:
            window = chunk[:max_len]
            split_at = window.rfind("\n\n")
            if split_at <= 0:
                split_at = window.rfind("\n")
            if split_at <= 0:
                split_at = max_len
            pieces.append(chunk[:split_at])
            chunk = chunk[split_at:].lstrip("\n")
        if chunk:
            pieces.append(chunk)
        return pieces

    def _split_oversized_pre(self, block: str, max_len: int) -> list[str]:
        """Split one oversized <pre> block into several valid <pre> blocks.

        The inner code is broken on newline boundaries and each piece is
        re-wrapped with the original opening tag so every emitted chunk stays
        valid Telegram HTML.
        """
        match = re.match(
            r"<pre>(<code(?: class=\"[^\"]*\")?>)(.*)</code></pre>",
            block,
            re.DOTALL,
        )
        if not match:
            return self._hard_split(block, max_len)

        open_tag = match.group(1)
        inner = match.group(2)
        overhead = len("<pre>") + len(open_tag) + len("</code></pre>")
        budget = max(1, max_len - overhead)

        pieces: list[str] = []
        for part in self._hard_split(inner, budget):
            pieces.append(f"<pre>{open_tag}{part}</code></pre>")
        return pieces

    def split_long_message(self, text: str) -> list[str]:
        """Split rendered HTML into chunks, never breaking a tag or <pre> block.

        The text is tokenized into atomic units — each ``<pre>`` block and the
        prose between blocks — which are greedily packed into chunks no larger
        than ``MAX_MESSAGE_LENGTH``. Prose is only ever cut on newline /
        paragraph boundaries, which (since the inline ``<b>/<i>/<code>`` tags we
        emit never contain newlines) guarantees those tags are never split.
        """
        max_len = self.MAX_MESSAGE_LENGTH

        units: list[tuple[bool, str]] = []
        last_end = 0
        for match in self._PRE_BLOCK_PATTERN.finditer(text):
            if match.start() > last_end:
                units.append((False, text[last_end:match.start()]))
            units.append((True, match.group(0)))
            last_end = match.end()
        if last_end < len(text):
            units.append((False, text[last_end:]))

        chunks: list[str] = []
        current = ""

        def _flush() -> None:
            nonlocal current
            if current:
                chunks.append(current)
                current = ""

        for is_pre, content in units:
            if not content:
                continue

            if len(current) + len(content) <= max_len:
                current += content
                continue

            _flush()

            if len(content) <= max_len:
                current = content
                continue

            pieces = (
                self._split_oversized_pre(content, max_len)
                if is_pre
                else self._hard_split(content, max_len)
            )
            for piece in pieces:
                if len(current) + len(piece) <= max_len:
                    current += piece
                else:
                    _flush()
                    current = piece

        _flush()
        return chunks if chunks else ["<i>(пусто)</i>"]

    # --- File upload helpers ----------------------------------------------
    def _largest_code_block(self, text: str) -> tuple[str, str] | None:
        """Return (code, lang) of the longest fenced block, or None if none."""
        best: tuple[str, str] | None = None
        best_len = -1
        for match in self.CODE_BLOCK_PATTERN.finditer(text):
            lang, code = self._strip_code_fence(match)
            if len(code) > best_len:
                best_len = len(code)
                best = (code, lang)
        return best

    def needs_file_upload(self, text: str) -> bool:
        """True if the text's largest code block exceeds MAX_CODE_BLOCK_LENGTH."""
        block = self._largest_code_block(text)
        if block is None:
            return False
        code, _lang = block
        return len(code) > self.MAX_CODE_BLOCK_LENGTH

    def pop_largest_code_block(self, text: str) -> tuple[str, str, str]:
        """Remove the largest fenced code block and return the parts.

        Returns ``(remaining_text, code, language)``. The block is replaced in
        the remaining text by a short placeholder noting it was sent as a file.
        ``language`` is ``"txt"`` when no hint is present. When there is no code
        block at all, returns ``(text, "", "txt")``.
        """
        best_match: re.Match | None = None
        best_len = -1
        for match in self.CODE_BLOCK_PATTERN.finditer(text):
            _lang, code = self._strip_code_fence(match)
            if len(code) > best_len:
                best_len = len(code)
                best_match = match
        if best_match is None:
            return (text, "", "txt")

        lang, code = self._strip_code_fence(best_match)
        placeholder = "📎 (код отправлен файлом)"
        remaining = (
            text[: best_match.start()] + placeholder + text[best_match.end():]
        )
        return (remaining, code, lang or "txt")

    def extract_code_for_file(self, text: str) -> tuple[str, str]:
        """Return (code_content, language) for the largest code block.

        ``language`` is the fence's language hint, or ``"txt"`` when absent or
        when there is no code block at all.
        """
        block = self._largest_code_block(text)
        if block is None:
            return ("", "txt")
        code, lang = block
        return (code, lang or "txt")

    # --- Misc --------------------------------------------------------------
    @staticmethod
    def make_thinking_text(dots: int) -> str:
        """Return the animated thinking string for the given dot count."""
        return "⏳ Thinking" + ("." * dots)
