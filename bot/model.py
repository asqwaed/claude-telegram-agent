"""View and switch the Claude model the bot runs on.

The bot spawns ``claude --print`` per message; the model that subprocess uses is
controlled by passing ``--model`` (see ``handler._claude_cli_args``). The chosen
alias is persisted in ``config.MODEL_FILE`` and read fresh on every turn, so a
switch takes effect on the *next* message with no restart.

``--model`` is given a value Claude Code resolves itself: the bare aliases
``opus``/``sonnet``/``haiku`` track the latest version of each tier, while Fable
is pinned by id. Unknown input is rejected so a typo can't wedge the bot onto a
non-existent model.
"""

import logging

import config

logger = logging.getLogger(__name__)

# alias -> (value passed to `claude --model`, human label)
MODELS: dict[str, tuple[str, str]] = {
    "opus": ("opus", "Opus 4.8"),
    "sonnet": ("sonnet", "Sonnet 4.6"),
    "haiku": ("haiku", "Haiku 4.5"),
    "fable": ("claude-fable-5", "Fable 5"),
}
DEFAULT_ALIAS = "opus"

# Valid `claude --effort` levels, cheapest → hardest. Default matches the global
# settings.json effortLevel the bot used before this was switchable.
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "high"


def _read_alias() -> str:
    """Current alias from the state file, falling back to the default."""
    try:
        alias = config.MODEL_FILE.read_text().strip().lower()
    except OSError:
        return DEFAULT_ALIAS
    return alias if alias in MODELS else DEFAULT_ALIAS


def current_alias() -> str:
    return _read_alias()


def current_arg() -> str:
    """The string to hand to ``claude --model`` for the active model."""
    return MODELS[_read_alias()][0]


def current_label() -> str:
    alias = _read_alias()
    return f"{MODELS[alias][1]} ({alias})"


def _resolve(name: str) -> str | None:
    """Map user input (alias or full --model value) to a known alias."""
    key = name.strip().lower()
    if key in MODELS:
        return key
    for alias, (arg, _label) in MODELS.items():
        if key == arg.lower():
            return alias
    return None


def set_model(name: str) -> tuple[bool, str]:
    """Persist a new model choice. Returns (ok, message)."""
    alias = _resolve(name)
    if alias is None:
        return False, (
            f"не знаю модель «{name}». доступны: " + ", ".join(MODELS)
        )
    try:
        config.MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.MODEL_FILE.write_text(alias)
    except OSError as exc:
        return False, f"не вышло сохранить: {exc}"
    logger.info("Model switched to %s (%s)", alias, MODELS[alias][0])
    return True, f"модель → {MODELS[alias][1]} ({alias})"


# --- Effort ------------------------------------------------------------------
def current_effort() -> str:
    """Current effort level from the state file, falling back to the default."""
    try:
        level = config.EFFORT_FILE.read_text().strip().lower()
    except OSError:
        return DEFAULT_EFFORT
    return level if level in EFFORTS else DEFAULT_EFFORT


def set_effort(level: str) -> tuple[bool, str]:
    """Persist a new effort level. Returns (ok, message)."""
    lvl = level.strip().lower()
    if lvl not in EFFORTS:
        return False, f"не знаю effort «{level}». доступны: " + ", ".join(EFFORTS)
    try:
        config.EFFORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.EFFORT_FILE.write_text(lvl)
    except OSError as exc:
        return False, f"не вышло сохранить: {exc}"
    logger.info("Effort switched to %s", lvl)
    return True, f"effort → {lvl}"


# --- Combined switch: parse "<model> and/or <effort>" in any order -----------
def apply(spec: str) -> str:
    """Apply a `/model` argument that may name a model, an effort, or both.

    Tokens are matched independently, so `sonnet high`, `high`, `opus`,
    `max sonnet` all work. Any unrecognised token aborts without changing
    anything, so a typo never half-applies.
    """
    tokens = spec.split()
    want_model = want_effort = None
    unknown: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if _resolve(low) is not None:
            want_model = low
        elif low in EFFORTS:
            want_effort = low
        else:
            unknown.append(tok)

    if unknown:
        return (
            "не понял: " + ", ".join(unknown)
            + f"\nмодели: {', '.join(MODELS)} · effort: {', '.join(EFFORTS)}"
        )
    if not (want_model or want_effort):
        return "формат: /model <модель> <effort>, напр. /model sonnet high"

    parts: list[str] = []
    if want_model:
        parts.append(set_model(want_model)[1])
    if want_effort:
        parts.append(set_effort(want_effort)[1])
    return " · ".join(parts) + " (со следующего сообщения)"


def status_text() -> str:
    """Human-readable current model + effort and the switchable options."""
    active = _read_alias()
    effort = current_effort()
    lines = [
        f"модель: <b>{MODELS[active][1]}</b> ({active}) · effort: <b>{effort}</b>",
        "",
        "переключить: <code>/model &lt;модель&gt; &lt;effort&gt;</code> "
        "(можно по отдельности)",
    ]
    for alias, (_arg, label) in MODELS.items():
        mark = " ✅" if alias == active else ""
        lines.append(f"  • <code>{alias}</code> — {label}{mark}")
    eff = "  ".join(
        (f"<b>{e}</b>" if e == effort else e) for e in EFFORTS
    )
    lines.append(f"effort: {eff}")
    return "\n".join(lines)


# --- CLI: the agent calls this from bash to view/switch on request -----------
def _main(argv: list[str]) -> int:
    """`python3 -m bot.model get | set <model and/or effort> | list`."""
    if not argv or argv[0] in ("get", "status"):
        print(f"{current_label()} · effort: {current_effort()}")
        return 0
    if argv[0] == "list":
        print(
            "models: " + ", ".join(f"{a} ({lbl})" for a, (_x, lbl) in MODELS.items())
            + "\neffort: " + ", ".join(EFFORTS)
        )
        return 0
    if argv[0] == "set":
        if len(argv) < 2:
            print("usage: set <model and/or effort>, e.g. set sonnet high")
            return 2
        print(apply(" ".join(argv[1:])))
        return 0
    print("usage: get | set <model and/or effort> | list")
    return 2


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(_main(sys.argv[1:]))
