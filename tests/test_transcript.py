"""Tests for the substantive-paragraph capture contract (issue #88).

Spec: prd.md § "Acceptance criteria" — extract_insights inverted from a marker
WHITELIST to a mechanics BLACKLIST with paragraph chunking. Markers still emit
kind="marker" insights and influence layer routing. The new kind="paragraph"
covers substantive prose; kind="assistant-summary" is REMOVED.

These tests are derived from prd.md / handoff.md / pipeline.json only — the
implementer (core/transcript.py) runs in parallel and is NOT read here.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# 1, 2. Paragraph chunker
# ---------------------------------------------------------------------------


def test_paragraphs_splits_on_blank_lines() -> None:
    # Spec: prd.md § "Acceptance criteria" — paragraph chunking (blank-line split)
    from core.transcript import _paragraphs

    assert _paragraphs("a\n\nb\n\n\nc") == ["a", "b", "c"]


def test_paragraphs_collapses_internal_whitespace() -> None:
    # Spec: prd.md § "Acceptance criteria" — internal-whitespace collapse
    from core.transcript import _paragraphs

    assert _paragraphs("foo   bar\n  baz") == ["foo bar baz"]


# ---------------------------------------------------------------------------
# 3, 4. Fenced-code blacklist (positive + negative)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["bash", "sh", "json", "yaml", "toml"])
def test_fenced_code_paragraph_is_skipped(lang: str) -> None:
    # Spec: prd.md § "Acceptance criteria" — fenced-code blacklist rule
    from core.transcript import extract_insights

    body = f"```{lang}\nfoo bar baz qux quux corge\nmore mechanical content here\n```"
    transcript = [{"role": "assistant", "content": body}]
    assert extract_insights(transcript) == []


def test_non_fenced_paragraph_is_not_filtered_by_fence_rule() -> None:
    # Spec: prd.md § "Acceptance criteria" — substantive prose survives the
    # blacklist when no fence is present.
    from core.transcript import extract_insights

    body = (
        "This is a substantive design observation about how the capture surface "
        "now reaches paragraph granularity and what that implies for downstream "
        "consumers reading the persisted memory store."
    )
    transcript = [{"role": "assistant", "content": body}]
    insights = extract_insights(transcript)
    paragraph_kinds = [i for i in insights if i.kind == "paragraph"]
    assert len(paragraph_kinds) == 1


# ---------------------------------------------------------------------------
# 5, 6. Function-call marker blacklist (positive + negative)
# ---------------------------------------------------------------------------


def test_function_call_marker_paragraph_is_skipped() -> None:
    # Spec: prd.md § "Acceptance criteria" — function-call marker blacklist
    from core.transcript import extract_insights

    body = (
        "<function_calls>\n<invoke name=\"Read\">\n<parameter name=\"file_path\">"
        "/tmp/foo</parameter>\n</invoke>\n</function_calls>"
    )
    transcript = [{"role": "assistant", "content": body}]
    assert extract_insights(transcript) == []


def test_function_call_marker_negative() -> None:
    # Spec: prd.md § "Acceptance criteria" — paragraphs without function-call
    # markers survive that rule.
    from core.transcript import extract_insights

    body = (
        "The substantive observation here is that the new capture pipeline now "
        "ingests paragraph-level prose from every assistant turn rather than "
        "depending exclusively on emoji markers as the whitelist gate."
    )
    transcript = [{"role": "assistant", "content": body}]
    insights = extract_insights(transcript)
    assert any(i.kind == "paragraph" for i in insights)


# ---------------------------------------------------------------------------
# 7, 8. Short-paragraph + decision-word blacklist (positive + negative)
# ---------------------------------------------------------------------------


def test_short_paragraph_without_decision_word_is_skipped() -> None:
    # Spec: prd.md § "Acceptance criteria" — short + no decision word → SKIP
    from core.transcript import extract_insights

    transcript = [{"role": "assistant", "content": "ok done"}]
    assert extract_insights(transcript) == []


def test_short_paragraph_with_decision_word_is_kept() -> None:
    # Spec: prd.md § "Acceptance criteria" — short paragraph with decision word
    # keeps a "Decision"-bearing insight (durable-line or paragraph; either OK).
    from core.transcript import extract_insights

    transcript = [{"role": "assistant", "content": "Decision: pick X"}]
    insights = extract_insights(transcript)
    assert len(insights) >= 1
    assert any("Decision" in i.content for i in insights)


# ---------------------------------------------------------------------------
# 9, 10. Thinking-aloud stub blacklist (English + Korean)
# ---------------------------------------------------------------------------


def test_thinking_aloud_stub_english_is_skipped() -> None:
    # Spec: prd.md § "Acceptance criteria" — English thinking-aloud stub
    from core.transcript import extract_insights

    body = (
        "let me check the file once more before deciding on the right capture "
        "boundary for this transcript path, since the previous result was "
        "inconsistent with my expectation."
    )
    transcript = [{"role": "assistant", "content": body}]
    assert extract_insights(transcript) == []


def test_thinking_aloud_stub_korean_is_skipped() -> None:
    # Spec: prd.md § "Acceptance criteria" — Korean thinking-aloud stub
    from core.transcript import extract_insights

    body = (
        "확인하겠습니다. 곧 결과를 보고드리겠습니다. 추가로 몇 가지 더 살펴본 "
        "후에 최종 보고서를 정리해서 전달하도록 하겠습니다."
    )
    transcript = [{"role": "assistant", "content": body}]
    insights = extract_insights(transcript)
    paragraph_kinds = [i for i in insights if i.kind == "paragraph"]
    assert paragraph_kinds == []


# ---------------------------------------------------------------------------
# 11. Korean substantive prose is kept
# ---------------------------------------------------------------------------


def test_korean_substantive_paragraph_is_kept() -> None:
    # Spec: prd.md § "Acceptance criteria" — substantive Korean prose passes.
    from core.transcript import extract_insights

    body = (
        "이번 변경에서는 어시스턴트가 작성하는 모든 단락을 저장소에 캡처하도록 "
        "기본 동작을 바꾸었으며, 도구 호출 로그나 코드 블록은 블랙리스트로 "
        "걸러져서 영속화되지 않는다는 점이 핵심이다."
    )
    transcript = [{"role": "assistant", "content": body}]
    insights = extract_insights(transcript)
    paragraph_insights = [i for i in insights if i.kind == "paragraph"]
    assert len(paragraph_insights) == 1
    # Whitespace-collapsed content equals the paragraph form.
    import re

    collapsed = re.sub(r"\s+", " ", body).strip()
    assert paragraph_insights[0].content == collapsed


# ---------------------------------------------------------------------------
# 12. Marker line emits marker insight + routes layer to global
# ---------------------------------------------------------------------------


def test_marker_line_emits_marker_insight_and_routes_layer_to_global() -> None:
    # Spec: prd.md § "Acceptance criteria" — markers still emit kind="marker"
    # AND route layer (the 🧠 emoji maps to global).
    from core.transcript import extract_insights

    transcript = [
        {
            "role": "assistant",
            "content": "✻ 🧠 user preference: always require TDD before implementation",
        }
    ]
    insights = extract_insights(transcript)
    markers = [i for i in insights if i.kind == "marker"]
    assert len(markers) >= 1
    assert any(m.layer == "global" for m in markers)


# ---------------------------------------------------------------------------
# 13. Transcript-scope dedupe
# ---------------------------------------------------------------------------


def test_duplicate_paragraph_within_same_transcript_skipped() -> None:
    # Spec: prd.md § "Acceptance criteria" — transcript-scope dedupe by
    # _normalise_content.
    from core.transcript import extract_insights

    paragraph = (
        "The substantive observation here is that paragraph deduplication is "
        "scoped to a single transcript and keyed by the normalised content "
        "string used elsewhere in the module."
    )
    body = f"{paragraph}\n\n{paragraph}"
    transcript = [{"role": "assistant", "content": body}]
    insights = extract_insights(transcript)
    paragraph_kinds = [i for i in insights if i.kind == "paragraph"]
    assert len(paragraph_kinds) == 1


# ---------------------------------------------------------------------------
# 14. MNEMOS_BEHAVIOR_BLOCK — end-of-turn capture obligation section
# ---------------------------------------------------------------------------


def test_behavior_block_contains_end_of_turn_section() -> None:
    # Spec: prd.md § "Acceptance criteria" — new mandatory section with
    # Capture-these / Do-NOT-capture lists plus positive + negative examples.
    from core.adapters.base import MNEMOS_BEHAVIOR_BLOCK

    assert "### End-of-turn capture obligation" in MNEMOS_BEHAVIOR_BLOCK
    assert "Capture these" in MNEMOS_BEHAVIOR_BLOCK
    assert "Do NOT capture" in MNEMOS_BEHAVIOR_BLOCK
    # Positive example fragment (one substantive design conclusion):
    assert "Architecture decision" in MNEMOS_BEHAVIOR_BLOCK
    # Negative example fragment (a tool-call dump):
    assert "<function_calls>" in MNEMOS_BEHAVIOR_BLOCK


# ---------------------------------------------------------------------------
# 15. Layer routing on the paragraph emission path
# ---------------------------------------------------------------------------


def test_user_preference_paragraph_routes_to_global_layer() -> None:
    # Spec: prd.md § "Acceptance criteria" — _layer_for_content keeps routing
    # global on the paragraph emission path.
    from core.transcript import extract_insights

    body = (
        "User preference: always require explicit error handling everywhere in "
        "the codebase for any module touching the database layer or external "
        "API surface."
    )
    transcript = [{"role": "assistant", "content": body}]
    insights = extract_insights(transcript)
    assert any(i.layer == "global" for i in insights)


# ---------------------------------------------------------------------------
# 16. Decision-word expansion (issue #89 follow-up to #88)
# ---------------------------------------------------------------------------


class TestDecisionWordExpansion89:
    """Issue #89: short durable statements like "user preference: …" were being
    dropped by the mechanics blacklist's short-paragraph rule because
    `_DECISION_WORDS` lacked "preference" / "constraint" and their Korean
    equivalents. These tests pin the expansion in place.
    """

    def test_short_user_preference_passes_and_routes_global(self) -> None:
        from core.transcript import extract_insights

        transcript = [
            {
                "role": "assistant",
                "content": "user preference: always TDD for new core code.",
            }
        ]
        insights = extract_insights(transcript)
        paragraph_insights = [i for i in insights if i.kind == "paragraph"]
        assert len(paragraph_insights) == 1
        assert paragraph_insights[0].layer == "global"
        assert "user preference" in paragraph_insights[0].content

    def test_short_project_constraint_passes(self) -> None:
        from core.transcript import extract_insights

        transcript = [
            {
                "role": "assistant",
                "content": "project constraint: never auto-merge.",
            }
        ]
        insights = extract_insights(transcript)
        paragraph_insights = [i for i in insights if i.kind == "paragraph"]
        assert len(paragraph_insights) == 1
        # Length sanity: confirms we'd have been dropped without "constraint"
        # in _DECISION_WORDS.
        assert len(paragraph_insights[0].content) < 80
        # Layer routing intentionally not pinned to a specific non-global
        # name; just confirms it is NOT mis-routed to global.
        assert paragraph_insights[0].layer != "global"

    def test_short_korean_선호_passes(self) -> None:
        from core.transcript import extract_insights

        transcript = [{"role": "assistant", "content": "사용자 선호: 항상 TDD."}]
        insights = extract_insights(transcript)
        paragraph_insights = [i for i in insights if i.kind == "paragraph"]
        assert len(paragraph_insights) == 1

    def test_short_korean_제약_passes(self) -> None:
        from core.transcript import extract_insights

        transcript = [{"role": "assistant", "content": "제약: PyPI 사용 금지."}]
        insights = extract_insights(transcript)
        paragraph_insights = [i for i in insights if i.kind == "paragraph"]
        assert len(paragraph_insights) == 1

    def test_short_paragraph_without_any_decision_word_still_dropped(self) -> None:
        # Regression guard: the short-paragraph filter must still drop a tiny
        # non-decision paragraph after the _DECISION_WORDS expansion.
        from core.transcript import extract_insights

        transcript = [{"role": "assistant", "content": "안녕하세요"}]
        assert extract_insights(transcript) == []
