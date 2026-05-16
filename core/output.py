"""ANSI output helpers for mnemos CLI."""
from __future__ import annotations

import os

ANSI_RESET = "\033[0m"
ANSI_ITALIC = "\033[3m"
ANSI_DIM = "\033[2m"

LAYER_COLOR: dict[str, str] = {
    "ephemeral": "\033[90m",
    "working": "\033[90m",
    "session": "\033[90m",
    "project": "\033[94m",
    "global": "\033[33m",
}

# Emoji markers for each layer (new grammar, Issue #4 task).
# Format: ``✻ {LAYER_EMOJI} <brief description>``
# The (layer) text suffix is intentionally dropped — the emoji carries the
# layer signal visually.
LAYER_EMOJI: dict[str, str] = {
    "session": "💡",
    "project": "💾",
    "global": "🧠",
}

# Legacy suffix pattern for backward-compat parsing during the 2-week
# grace period.  Callers that still produce ``✻ 🧠 content (global)``
# remain parseable; the suffix is simply ignored on display.
_LEGACY_LAYER_SUFFIX_PATTERN = r"\s*\((?:session|project|global|ephemeral|working)\)\s*$"


def _layer_emoji(layer: str) -> str:
    """Return the marker emoji for *layer*, defaulting to 🧠 for unknown layers."""
    return LAYER_EMOJI.get(layer, "🧠")


def capture_notice(content: str, layer: str, *, no_color: bool = False) -> str:
    """Return a styled capture notice using the emoji-based grammar.

    New format (this commit):
        ``✻ 💡 <content>``  (session)
        ``✻ 💾 <content>``  (project)
        ``✻ 🧠 <content>``  (global / default)

    Legacy format (2-week grace period):
        ``✻ 🧠 <content> (layer)``

    The ``(layer)`` text suffix is no longer emitted.  Downstream parsers
    that relied on the suffix must be updated to use the emoji instead.
    During the grace period the UserPromptSubmit.sh hook regex accepts both
    forms so existing conversations continue to parse correctly.

    Strips ANSI when ``NO_COLOR`` is set in the environment or
    *no_color* is True.
    """
    emoji = _layer_emoji(layer)
    text = f"✻ {emoji} {content}"
    if no_color or "NO_COLOR" in os.environ:
        return text
    color = LAYER_COLOR.get(layer, "\033[90m")
    return f"{color}{ANSI_DIM}{ANSI_ITALIC}{text}{ANSI_RESET}"
