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


def capture_notice(content: str, layer: str, *, no_color: bool = False) -> str:
    """Return a styled capture notice; strips ANSI when NO_COLOR is set or no_color=True."""
    if no_color or "NO_COLOR" in os.environ:
        return f"✻ 🧠 {content} ({layer})"
    color = LAYER_COLOR.get(layer, "\033[90m")
    return f"{color}{ANSI_DIM}{ANSI_ITALIC}✻ 🧠 {content} ({layer}){ANSI_RESET}"


def promote_notice(item_id: str, target_layer: str, *, no_color: bool = False) -> str:
    """Return a styled promotion notice.

    Format: ✻ 🧠 promoted <item_id> → <target_layer>

    Strips ANSI when NO_COLOR is set in the environment or no_color=True.
    Uses the destination layer's colour for styling.
    """
    if no_color or "NO_COLOR" in os.environ:
        return f"✻ 🧠 promoted {item_id} → {target_layer}"
    color = LAYER_COLOR.get(target_layer, "\033[90m")
    return f"{color}{ANSI_DIM}{ANSI_ITALIC}✻ 🧠 promoted {item_id} → {target_layer}{ANSI_RESET}"
