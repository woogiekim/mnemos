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
_SIGNAL_RE = re.compile(r"\b(decision|decided|root cause|cause|summary|implemented|fixed|changed files|verification|captures|constraint|workflow|boundary|rationale|preference|architecture|regression)\b", re.IGNORECASE)
_PROJECT_SIGNAL_RE = re.compile(r"\b(architecture|constraint|decision|decided|rationale|pattern|workflow)\b", re.IGNORECASE)
_GLOBAL_SIGNAL_RE = re.compile(r"\b(user preference|preference:|always prefer|global convention|cross-project)\b", re.IGNORECASE)
_MARKER_LINE_RE = re.compile(r"^\s*✻\s+(💡|💾|🧠)\s+(.+?)(?:\s+\([a-z]+\))?\s*$", re.MULTILINE)


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


def _summarize(lines: list[str]) -> str:
    kept: list[str] = []
    for line in lines:
        if re.fullmatch(r"#{1,6}\s+\S.{0,60}", line):
            continue
        kept.append(re.sub(r"\s+", " ", line))
        if len(" ".join(kept)) >= 360 or len(kept) >= 5:
            break
    summary = " ".join(kept).strip()
    return summary[:497].rstrip() + "..." if len(summary) > 500 else summary


def _layer_for_content(content: str, fallback: str = "session") -> str:
    if _GLOBAL_SIGNAL_RE.search(content):
        return "global"
    if _PROJECT_SIGNAL_RE.search(content):
        return "project"
    return fallback


def extract_insights(messages: list[dict[str, Any]]) -> list[TranscriptInsight]:
    """Extract deterministic durable insights from transcript messages."""
    insights: list[TranscriptInsight] = []
    durable_re = re.compile(r"^\s*(?:[-*]\s*)?(decision|decided|root cause|summary|workflow boundary|constraint|rationale|preference)\s*[:：-]\s*(.+)$", re.IGNORECASE)
    for index, message in enumerate(messages):
        role = message.get("role") or message.get("type") or ""
        if role not in {"assistant", "summary", "event", "stop"}:
            continue
        text = _message_text(message)
        for marker in _MARKER_LINE_RE.finditer(_mask_code(text)):
            content = marker.group(2).strip()
            if content and len(content.strip()) >= 12:
                insights.append(TranscriptInsight(content, _EMOJI_TO_LAYER.get(marker.group(1), "session"), "marker", index))
        lines, dropped = _clean_lines(text)
        if _looks_internal(text, lines, dropped):
            continue
        for line in lines:
            match = durable_re.match(line)
            if match:
                content = f"{match.group(1).capitalize()}: {match.group(2).strip()}"
                if not _is_trivial(content):
                    insights.append(TranscriptInsight(content, _layer_for_content(content), "durable-line", index))
        summary = _summarize(lines)
        if summary and not _is_trivial(summary) and _SIGNAL_RE.search(summary):
            insights.append(TranscriptInsight(f"AI conversation insight: {summary}", _layer_for_content(summary), "assistant-summary", index))
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
