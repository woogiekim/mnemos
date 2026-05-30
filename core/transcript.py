"""Deterministic transcript capture for autonomous host hooks."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.gateway import MemoryGateway
from core.provider import PROVIDER_CONTRACT_VERSION

_EMOJI_TO_LAYER = {"💡": "session", "💾": "project", "🧠": "global"}
_CONTROL_LINE_RE = re.compile(r"^\s*(?:\[crew\]\b|STATUS\s*:|PLAN\s*:|BLOCKER\s*:|REVIEW\s*:|ROUTE\s*:|TASK\s*:|TASK_ID\s*:|TASK_DIR\s*:|PROJECT_ROOT\s*:|BRANCH\s*:|EXECUTION_MODE\s*:|SESSION_ID\s*:|REQUIREMENTS(?:_PATH)?\s*:|HANDOFF(?:_PATH)?\s*:|QUALITY_RULE_PATH\s*:|PIPELINE_PATH\s*:|AGENT_CREW_HOME\s*:|HOST_TASK_ID\s*:|PROGRESS(?:_LOG)?\s*:|<\/?mnemos-[^>]+>|<\/?mnemos-context[^>]*>|<\/?mnemos-capture-protocol>|\{TASK_DIR\}|\{PROJECT_ROOT\}|\{BRANCH\})", re.IGNORECASE)
_HANDOFF_HINT_RE = re.compile(r"\b(?:handoff|downstream agent|stage agent|supervisor pipeline|agent-crew task|pipeline\.json|approval\.md|progress\.log)\b", re.IGNORECASE)
_TRIVIAL_RE = re.compile(r"^(?:yes|yep|yeah|no|nope|ok|okay|sure|done|thanks|thank you|completed|continue|proceed|go|좋아요|네|아니요|완료|진행)$", re.IGNORECASE)
_PROJECT_SIGNAL_RE = re.compile(r"\b(architecture|constraint|decision|decided|rationale|pattern|workflow)\b", re.IGNORECASE)
_GLOBAL_SIGNAL_RE = re.compile(r"\b(user preference|preference:|always prefer|global convention|cross-project)\b", re.IGNORECASE)
_MARKER_LINE_RE = re.compile(r"^\s*✻\s+(💡|💾|🧠)\s+(.+?)(?:\s+\([a-z]+\))?\s*$", re.MULTILINE)

# Paragraph chunker and blacklist predicates (issue #88).
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+")
_FENCED_BLOCK_RE = re.compile(r"```\s*(?:bash|sh|shell|json|yaml|toml|py|python|ts|tsx|js|jsx|rs|go)\b", re.IGNORECASE)
_FUNCTION_CALL_RE = re.compile(r"<function_calls>|<invoke\s+name=|<parameter\s+name=|tool_use:|command=\"", re.IGNORECASE)
_DECISION_WORDS = ("결론", "원인은", "근본 원인", "결정", "decided", "decision:", "root cause", "tl;dr", "summary:")
_THINKING_STUBS = ("확인하겠", "let me check", "looking at", "i'll now", "let's", "잠시")


@dataclass(frozen=True)
class TranscriptInsight:
    """A deterministic durable insight extracted from a transcript."""

    content: str
    layer: str
    kind: str
    source_index: int

    @property
    def source_key(self) -> str:
        return f"{self.kind}:{self.source_index}"


def _normalise_content(content: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", content).strip()).lower()


def _mask_code(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", text)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text")
    return ""


def load_transcript(path: str | Path) -> list[dict[str, Any]]:
    """Load a JSON transcript or JSONL event stream."""
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [row for row in parsed if isinstance(row, dict)]
        if isinstance(parsed, dict):
            rows = parsed.get("messages") or parsed.get("events")
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
            return [parsed]
    except json.JSONDecodeError:
        pass
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _clean_lines(text: str) -> tuple[list[str], int]:
    lines: list[str] = []
    dropped = 0
    for raw in _mask_code(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        if _MARKER_LINE_RE.match(line) or _CONTROL_LINE_RE.search(line) or line.startswith(("<!--", "-->", "|---", "---")):
            dropped += 1
            continue
        if re.fullmatch(r"[\-*_#\s]+", line) or (line.startswith("|") and line.endswith("|")):
            dropped += 1
            continue
        if re.fullmatch(r"[-*]\s+(?:/|~|\$|\{).+", line):
            dropped += 1
            continue
        lines.append(line)
    return lines, dropped


def _is_trivial(text: str) -> bool:
    folded = re.sub(r"\s+", " ", text).strip(" .!?,:;\"'`").lower()
    if not folded or _TRIVIAL_RE.fullmatch(folded):
        return True
    words = re.findall(r"[A-Za-z0-9가-힣_/-]+", folded)
    return len(folded) < 50 or len(words) < 8


def _looks_internal(original: str, lines: list[str], dropped: int) -> bool:
    if not lines:
        return True
    control_density = dropped / max(dropped + len(lines), 1)
    if control_density >= 0.45 and _HANDOFF_HINT_RE.search(original):
        return True
    return _HANDOFF_HINT_RE.search(original) and any(token in original for token in ("TASK_ID:", "TASK_DIR:", "STATUS:", "PLAN:", "BLOCKER:"))


def _layer_for_content(content: str, fallback: str = "session") -> str:
    if _GLOBAL_SIGNAL_RE.search(content):
        return "global"
    if _PROJECT_SIGNAL_RE.search(content):
        return "project"
    return fallback


def _paragraphs(text: str) -> list[str]:
    """Split assistant text on blank-line boundaries; collapse internal whitespace."""
    chunks: list[str] = []
    for raw in _PARAGRAPH_SPLIT_RE.split(text):
        collapsed = re.sub(r"\s+", " ", raw).strip()
        if collapsed:
            chunks.append(collapsed)
    return chunks


def _is_fenced_code_paragraph(paragraph: str) -> bool:
    return bool(_FENCED_BLOCK_RE.search(paragraph))


def _has_function_call_marker(paragraph: str) -> bool:
    if _FUNCTION_CALL_RE.search(paragraph):
        return True
    return any(_CONTROL_LINE_RE.search(line) for line in paragraph.splitlines())


def _has_decision_word(paragraph: str) -> bool:
    lowered = paragraph.lower()
    return any(word in lowered for word in _DECISION_WORDS)


def _is_short_non_decision_paragraph(paragraph: str) -> bool:
    collapsed = re.sub(r"\s+", " ", paragraph).strip()
    return len(collapsed) < 80 and not _has_decision_word(paragraph)


def _is_thinking_aloud(paragraph: str) -> bool:
    lowered = re.sub(r"\s+", " ", paragraph).strip().lower()
    return any(lowered.startswith(stub) for stub in _THINKING_STUBS)


def _is_mechanical_paragraph(paragraph: str) -> bool:
    return (
        _is_fenced_code_paragraph(paragraph)
        or _has_function_call_marker(paragraph)
        or _is_short_non_decision_paragraph(paragraph)
        or _is_thinking_aloud(paragraph)
    )


def extract_insights(messages: list[dict[str, Any]]) -> list[TranscriptInsight]:
    """Extract deterministic durable insights from transcript messages.

    Substantive prose lands as ``kind="paragraph"`` via the blacklist
    inversion introduced in issue #88. Markers and durable single-line
    statements continue to emit their respective kinds.
    """
    insights: list[TranscriptInsight] = []
    durable_re = re.compile(r"^\s*(?:[-*]\s*)?(decision|decided|root cause|summary|workflow boundary|constraint|rationale|preference)\s*[:：-]\s*(.+)$", re.IGNORECASE)
    content_seen: set[str] = set()
    for index, message in enumerate(messages):
        role = message.get("role") or message.get("type") or ""
        if role not in {"assistant", "summary", "event", "stop"}:
            continue
        text = _message_text(message)
        # 1. Marker insights — unconditional emit per issue #88 (gate removed).
        for marker in _MARKER_LINE_RE.finditer(_mask_code(text)):
            content = marker.group(2).strip()
            if content and len(content.strip()) >= 12:
                insights.append(TranscriptInsight(content, _EMOJI_TO_LAYER.get(marker.group(1), "session"), "marker", index))
        # 2. Internal pipeline-control messages still short-circuit.
        lines, dropped = _clean_lines(text)
        if _looks_internal(text, lines, dropped):
            continue
        # 3. Existing durable single-line statements (Decision:, Root cause:, ...).
        for line in lines:
            match = durable_re.match(line)
            if match:
                content = f"{match.group(1).capitalize()}: {match.group(2).strip()}"
                if not _is_trivial(content):
                    insights.append(TranscriptInsight(content, _layer_for_content(content), "durable-line", index))
                    content_seen.add(_normalise_content(content))
        # 4. Paragraph-level substantive prose (blacklist inversion).
        for paragraph in _paragraphs(text):
            if _is_mechanical_paragraph(paragraph):
                continue
            key = _normalise_content(paragraph)
            if key in content_seen:
                continue
            content_seen.add(key)
            insights.append(TranscriptInsight(paragraph, _layer_for_content(paragraph), "paragraph", index))
    deduped: list[TranscriptInsight] = []
    seen: set[tuple[str, str]] = set()
    for insight in insights:
        key = (_normalise_content(insight.content), insight.source_key)
        if key not in seen:
            seen.add(key)
            deduped.append(insight)
    return deduped


def capture_transcript(*, transcript_path: str | Path, session_id: str | None, host: str, gateway: MemoryGateway | None = None) -> dict[str, Any]:
    """Extract and persist deterministic insights from a transcript."""
    gw = gateway or MemoryGateway()
    messages = load_transcript(transcript_path)
    insights = extract_insights(messages)
    captures: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for insight in insights:
        source = {"source": "transcript", "source_host": host, "source_path": str(transcript_path), "source_key": insight.source_key, "capture_kind": insight.kind}
        try:
            item_id = gw.capture(layer=insight.layer, content=insight.content, session_id=session_id, tags=["transcript", insight.kind], extra_metadata=source, no_classify=True)
            status = "duplicate" if item_id is None or gw.last_capture_was_duplicate else "captured"
            captures.append({"status": status, "id": item_id, "layer": insight.layer, "content": insight.content, "source": source})
        except Exception as exc:
            skipped.append({"status": "error", "layer": insight.layer, "content": insight.content, "source": source, "error": str(exc)})
    return {
        "provider": "mnemos",
        "provider_contract_version": PROVIDER_CONTRACT_VERSION,
        "status": "ok",
        "mode": "deterministic-v1",
        "host": host,
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "message_count": len(messages),
        "extracted_count": len(insights),
        "captured_count": sum(1 for item in captures if item["status"] == "captured"),
        "duplicate_count": sum(1 for item in captures if item["status"] == "duplicate"),
        "skipped_count": len(skipped),
        "captures": captures,
        "skipped": skipped,
    }
