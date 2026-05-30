"""Payload + render unit tests for ``core.inspectview`` (issue #80).

Pinned by ``${TASK_DIR}/context/prd.md`` § Payload contract. These tests are
the canonical pin for the wire format the HTML template parses; the template
must never drift from the field set and order asserted here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from core import inspectview, policy


# --------------------------------------------------------------------------- #
# Fixtures — minimal policy.yaml that mirrors the canonical promotion chain
# --------------------------------------------------------------------------- #
@pytest.fixture
def policy_path(tmp_path) -> Path:
    """A minimal policy.yaml carrying every layer in the canonical chain."""
    cfg = {
        "layers": {
            "transient": {
                "path_template": ".agent/transient/",
                "promotes_to": None,
                "promotion": {"age_hours": 0, "access_count": 0, "quality_score": 0.0},
            },
            "ephemeral": {
                "path_template": ".agent/runs/{run_id}/scratch/",
                "promotes_to": "working",
                "promotion": {"age_hours": 0, "access_count": 1, "quality_score": 0.3},
            },
            "working": {
                "path_template": ".agent/runs/{run_id}/working/",
                "promotes_to": "session",
                "promotion": {"age_hours": 0, "access_count": 2, "quality_score": 0.5},
            },
            "session": {
                "path_template": ".agent/sessions/{session_id}/",
                "promotes_to": "project",
                "promotion": {"age_hours": 0, "access_count": 3, "quality_score": 0.6},
            },
            "project": {
                "path_template": "wiki/projects/",
                "promotes_to": "global",
                "promotion": {"age_hours": 0, "access_count": 5, "quality_score": 0.7},
            },
            "global": {
                "path_template": "wiki/global/",
                "promotes_to": None,
                "promotion": {"age_hours": 0, "access_count": 0, "quality_score": 0.0},
            },
        },
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }
    p = tmp_path / "policy.yaml"
    p.write_text(yaml.dump(cfg))
    return p


@pytest.fixture
def engine(policy_path) -> policy.PolicyEngine:
    return policy.PolicyEngine(policy_path=str(policy_path))


def _persisted_item(
    *,
    item_id: str = "m1",
    layer: str = "session",
    stage: str = "stored",
    content: str = "hello",
    tags=None,
    quality_score: float = 0.9,
    access_count: int = 5,
    run_id: str = "run-1",
    session_id: str = "sess-1",
    created_at: str = "2026-05-29T10:00:00+00:00",
    content_hash: str = "deadbeef",
    path: str = "/tmp/wiki/sessions/m1.md",
) -> dict:
    """Return a synthetic item dict in the shape of ``store.iter_layer_items``."""
    return {
        "id": item_id,
        "layer": layer,
        "stage": stage,
        "content": content,
        "tags": list(tags) if tags is not None else ["arch"],
        "quality_score": quality_score,
        "access_count": access_count,
        "run_id": run_id,
        "session_id": session_id,
        "created_at": created_at,
        "content_hash": content_hash,
        "_path": path,
    }


# --------------------------------------------------------------------------- #
# Schema + structural invariants
# --------------------------------------------------------------------------- #
class TestDeriveDisplayTitle:
    """Pure-helper tests for ``_derive_display_title`` (issue #85).

    The derivation rule is the single source of truth surfaced to the operator
    in both ``mnemos inspect`` (#80) and ``mnemos ui`` (#83), so every branch
    must be pinned independently of the surrounding payload assembly.
    """

    def test_english_first_line(self):
        assert inspectview._derive_display_title("Hello world\nrest", "id-1") == "Hello world"

    def test_korean_first_line_preserved(self):
        # Korean syllables count as 1 visible char each.
        title = inspectview._derive_display_title("안녕하세요 세계\n둘째줄", "id-1")
        assert title == "안녕하세요 세계"

    def test_strips_leading_markdown_heading_marker(self):
        assert inspectview._derive_display_title("# Heading text", "id-1") == "Heading text"
        assert inspectview._derive_display_title("### deeper", "id-1") == "deeper"
        # Mid-string hashes are NOT stripped.
        assert inspectview._derive_display_title("not # a heading", "id-1") == "not # a heading"

    def test_takes_first_non_empty_line(self):
        # Leading blank lines and a markdown heading marker on line 3.
        content = "\n\n## real first line\nbody line\n"
        assert inspectview._derive_display_title(content, "id-1") == "real first line"

    def test_leading_whitespace_stripped(self):
        assert inspectview._derive_display_title("   indented title  ", "id-1") == "indented title"

    def test_internal_whitespace_collapsed(self):
        title = inspectview._derive_display_title("a\t\tb   c", "id-1")
        assert title == "a b c"

    def test_truncates_long_title_with_ellipsis(self):
        long = "x" * 200
        title = inspectview._derive_display_title(long, "id-1")
        # 80 visible code points + "..."
        assert title == "x" * 80 + "..."

    def test_unicode_safe_truncation_korean(self):
        # 100 Korean syllables — must truncate by code point, not byte.
        long_ko = "가" * 100
        title = inspectview._derive_display_title(long_ko, "id-1")
        assert title == "가" * 80 + "..."
        # 80 visible code points before the ellipsis.
        assert len(title.replace("...", "")) == 80

    def test_unicode_safe_truncation_emoji(self):
        emoji = "🎉" * 100
        title = inspectview._derive_display_title(emoji, "id-1")
        assert title.endswith("...")
        # The pre-ellipsis prefix is 80 emoji code points.
        prefix = title[:-3]
        assert len(prefix) == 80
        assert all(ch == "🎉" for ch in prefix)

    def test_empty_content_falls_back_to_slug_id(self):
        assert inspectview._derive_display_title("", "feat-display-title-85") == "feat-display-title-85"

    def test_whitespace_only_content_falls_back_to_id(self):
        assert inspectview._derive_display_title("   \n\t  \n", "feat-foo") == "feat-foo"

    def test_heading_only_marker_falls_back_to_id(self):
        # Line is only ``#`` markers + whitespace — after stripping there's
        # nothing left, so the helper falls back to the id.
        assert inspectview._derive_display_title("### \n", "feat-foo") == "feat-foo"

    def test_uuid_id_returned_verbatim_on_empty_content(self):
        uuid = "9e6f1234-a5b1-4c2d-9e8f-0123456789ab"
        # Hyphenated but UUID-shaped → NOT treated as slug; returned verbatim.
        assert inspectview._derive_display_title("", uuid) == uuid

    def test_token_id_without_hyphen_returned_verbatim(self):
        assert inspectview._derive_display_title("", "tokenid") == "tokenid"

    def test_empty_content_and_empty_id_returns_empty_string(self):
        assert inspectview._derive_display_title("", "") == ""

    def test_none_content_treated_as_empty(self):
        # The payload assembly passes the raw stored ``content`` which may be
        # ``None``; the helper must tolerate it without raising.
        assert inspectview._derive_display_title(None, "feat-x") == "feat-x"  # type: ignore[arg-type]


class TestDeriveDisplayTitleRuleAndKeyValue:
    """Refined rule-skip + key:value extraction (issue #85 follow-up).

    The original heuristic ("first non-empty line wins, # stripped, collapse
    whitespace") regressed on ~10% of the live store: memories whose content
    begins with a YAML-style block (``---\\nname: foo\\ndescription: bar\\n---``)
    surfaced the literal ``---`` as their title. The refined rule:

    1. Skip pure horizontal-rule lines.
    2. Extract a ``name|title|summary|description|theme`` key:value when the
       line matches; an empty value falls through to the next line.
    3. Otherwise fall back to the prior heading-style extraction.

    Every branch documented in the spec is pinned by an independent test
    below — fan-in coverage matters because the helper's wrong answer is
    silently load-bearing across both ``mnemos inspect`` and ``mnemos ui``.
    """

    # ---- rule-line skipping --------------------------------------------- #
    def test_pure_rule_line_skipped(self):
        assert (
            inspectview._derive_display_title("---\nfoo bar", "id-1")
            == "foo bar"
        )

    def test_consecutive_rule_lines_skipped(self):
        # Multiple rule lines (mixing -, =, *) in a row — every one is
        # ignored until the first non-rule line surfaces.
        title = inspectview._derive_display_title("===\n---\n***\nfinal", "id-1")
        assert title == "final"

    def test_underscore_rule_skipped(self):
        # Underscores also count as a rule character — must be skipped.
        assert (
            inspectview._derive_display_title("___\nbody text", "id-1")
            == "body text"
        )

    def test_spaced_rule_line_skipped(self):
        # ``- - -`` — internal whitespace between rule characters still
        # counts as a rule line per the spec.
        assert (
            inspectview._derive_display_title("- - -\ntitle", "id-1")
            == "title"
        )

    def test_two_char_rule_is_not_a_rule(self):
        # Length 2 — explicitly NOT a rule per spec. Returned verbatim.
        assert inspectview._derive_display_title("--", "id-1") == "--"

    def test_rule_chars_with_letter_not_a_rule(self):
        # ``-x-`` contains a non-rule char — line is kept as-is.
        assert inspectview._derive_display_title("-x-", "id-1") == "-x-"

    def test_dashes_with_trailing_text_not_a_rule(self):
        # ``-- foo`` — the trailing ``foo`` disqualifies it as a rule line.
        # Whitespace is collapsed but the line is preserved.
        assert (
            inspectview._derive_display_title("-- foo", "id-1")
            == "-- foo"
        )

    def test_all_rule_lines_fall_back_to_slug_id(self):
        # Content is entirely rule lines — no candidate ever surfaces, so
        # the helper falls back to the slug-shaped id.
        assert (
            inspectview._derive_display_title(
                "---\n===\n***\n",
                "feat-only-rules-99",
            )
            == "feat-only-rules-99"
        )

    def test_all_rule_lines_fall_back_to_raw_uuid_id(self):
        # No candidate + UUID-shaped id → return UUID verbatim (no slug
        # branch).
        uuid = "9e6f1234-a5b1-4c2d-9e8f-0123456789ab"
        assert (
            inspectview._derive_display_title("---\n***\n", uuid) == uuid
        )

    # ---- KEY:VALUE extraction ------------------------------------------- #
    def test_yaml_block_name_extracted(self):
        # The exact regression that motivated the refinement: a content
        # block fenced by ``---`` lines whose first key is ``name:``.
        content = (
            "---\n"
            "name: always-use-agent-crew\n"
            "description: All code changes must go through agent-crew\n"
            "---\n"
            "body content"
        )
        assert (
            inspectview._derive_display_title(content, "id-1")
            == "always-use-agent-crew"
        )

    def test_yaml_block_korean_name_extracted(self):
        # Korean YAML value — round-trips verbatim (Unicode-safe).
        content = (
            "---\n"
            "name: dawn-core-goal\n"
            "description: dawn 프로젝트의 핵심 목표 — 5시간 안에 …\n"
            "---\n"
            "body"
        )
        assert (
            inspectview._derive_display_title(content, "id-1")
            == "dawn-core-goal"
        )

    def test_description_first_when_name_missing(self):
        # When ``name`` is absent, ``description`` is the next key in the
        # priority list. Quote stripping (one layer) also applies here.
        content = '---\ndescription: "개념 정의형 질문은 멘토에게 전달"'
        assert (
            inspectview._derive_display_title(content, "id-1")
            == "개념 정의형 질문은 멘토에게 전달"
        )

    def test_title_key_extracted(self):
        assert (
            inspectview._derive_display_title("---\ntitle: My Memory", "id-1")
            == "My Memory"
        )

    def test_summary_key_extracted(self):
        # ``summary`` is in the priority list — must also extract.
        assert (
            inspectview._derive_display_title(
                "---\nsummary: short blurb", "id-1"
            )
            == "short blurb"
        )

    def test_theme_key_extracted(self):
        # ``theme`` is in the priority list — must also extract.
        assert (
            inspectview._derive_display_title(
                "---\ntheme: dawn project", "id-1"
            )
            == "dawn project"
        )

    def test_key_matching_is_case_insensitive(self):
        # Both ``Name:`` and ``NAME:`` must be recognised — the spec calls
        # out a case-insensitive regex.
        assert (
            inspectview._derive_display_title("Name: foo", "id-1") == "foo"
        )
        assert (
            inspectview._derive_display_title("TITLE: bar", "id-1") == "bar"
        )

    def test_empty_value_falls_through(self):
        # Empty captured value MUST NOT yield the literal ``name:`` text —
        # the next non-rule, non-empty-key line wins.
        assert (
            inspectview._derive_display_title(
                "name:\nactual body", "id-1"
            )
            == "actual body"
        )

    def test_empty_value_then_only_rule_lines_falls_back_to_id(self):
        # Empty key value + no further candidates → fall back to the id.
        assert (
            inspectview._derive_display_title(
                "name:\n---\n", "feat-fallback-1"
            )
            == "feat-fallback-1"
        )

    def test_double_quoted_value_stripped(self):
        # One layer of matching ASCII double quotes is stripped.
        assert (
            inspectview._derive_display_title(
                'name: "foo bar"', "id-1"
            )
            == "foo bar"
        )

    def test_single_quoted_value_stripped(self):
        # One layer of matching ASCII single quotes is stripped.
        assert (
            inspectview._derive_display_title(
                "name: 'foo bar'", "id-1"
            )
            == "foo bar"
        )

    def test_unmatched_quote_preserved(self):
        # Unmatched quote — leave the value alone (no dequote).
        assert (
            inspectview._derive_display_title('name: "foo', "id-1")
            == '"foo'
        )

    def test_only_one_quote_layer_stripped(self):
        # Double-wrapped quotes: only ONE layer is stripped, the inner pair
        # survives.
        assert (
            inspectview._derive_display_title(
                "name: '\"foo bar\"'", "id-1"
            )
            == '"foo bar"'
        )

    def test_non_keyword_colon_line_is_plain_title(self):
        # A line like ``foo: bar`` (where ``foo`` is not in the keyword set)
        # is NOT a KEY:VALUE match — it's a plain title, returned verbatim.
        assert (
            inspectview._derive_display_title(
                "foo: bar baz", "id-1"
            )
            == "foo: bar baz"
        )

    # ---- mixed-shape interactions --------------------------------------- #
    def test_heading_wins_over_rule(self):
        # A Markdown heading on the line after a rule — heading is kept,
        # ``#`` is stripped, rule is dropped.
        assert (
            inspectview._derive_display_title(
                "# Title\n---\nbody", "id-1"
            )
            == "Title"
        )

    def test_rule_then_heading(self):
        # Rule first, heading second — same outcome (heading wins, ``#``
        # stripped).
        assert (
            inspectview._derive_display_title(
                "---\n# Title\nbody", "id-1"
            )
            == "Title"
        )

    def test_truncation_applies_to_extracted_key_value(self):
        # The Unicode-safe 80-codepoint truncation still applies to a
        # KEY:VALUE extracted candidate.
        long_value = "x" * 200
        content = f"---\nname: {long_value}\n"
        title = inspectview._derive_display_title(content, "id-1")
        assert title == "x" * 80 + "..."

    def test_truncation_applies_to_extracted_korean_key_value(self):
        # Unicode safety + truncation interact correctly on a KEY:VALUE
        # extracted value too.
        long_ko = "가" * 100
        content = f"---\ndescription: {long_ko}\n"
        title = inspectview._derive_display_title(content, "id-1")
        assert title == "가" * 80 + "..."
        assert len(title.replace("...", "")) == 80


class TestPayloadShape:
    def test_schema_version_pinned_to_one(self, engine):
        payload = inspectview.build_inspect_payload([], engine)
        assert payload["schema_version"] == 1

    def test_generated_at_is_iso8601_utc(self, engine):
        payload = inspectview.build_inspect_payload([], engine)
        # ISO-8601 with timezone offset; rejection of naïve timestamps.
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$",
            payload["generated_at"],
        ), payload["generated_at"]

    def test_layers_array_is_canonical_chain(self, engine):
        payload = inspectview.build_inspect_payload([], engine)
        assert payload["layers"] == [
            "transient",
            "ephemeral",
            "working",
            "session",
            "project",
            "global",
        ]

    def test_stages_array_matches_policy(self, engine):
        payload = inspectview.build_inspect_payload([], engine)
        assert payload["stages"] == policy.VALID_STAGES
        assert len(payload["stages"]) == 13

    def test_empty_items_yields_empty_memories(self, engine):
        payload = inspectview.build_inspect_payload([], engine)
        assert payload["memories"] == []


# --------------------------------------------------------------------------- #
# Per-item projection — every AC-relevant field is surfaced
# --------------------------------------------------------------------------- #
class TestItemProjection:
    def test_full_field_set_present(self, engine):
        item = _persisted_item()
        payload = inspectview.build_inspect_payload([item], engine)
        mem = payload["memories"][0]
        # AC1 (search): id, content, tags
        assert mem["id"] == "m1"
        assert mem["content"] == "hello"
        assert mem["tags"] == ["arch"]
        # #85 — every memory carries the derived display_title (here the
        # single-line content "hello" round-trips verbatim).
        assert mem["display_title"] == "hello"
        # AC2 (viewer): content + preview_truncated flag
        assert mem["preview_truncated"] is False
        # AC3 (lifecycle/layer filter): layer + stage
        assert mem["layer"] == "session"
        assert mem["stage"] == "stored"
        # AC4 (trust): layer + quality_score + access_count
        assert mem["quality_score"] == 0.9
        assert mem["access_count"] == 5
        # AC5 (provenance): run_id, session_id, created_at, content_hash, path
        assert mem["run_id"] == "run-1"
        assert mem["session_id"] == "sess-1"
        assert mem["created_at"] == "2026-05-29T10:00:00+00:00"
        assert mem["content_hash"] == "deadbeef"
        assert mem["path"] == "/tmp/wiki/sessions/m1.md"
        # AC6 (promotion/archive): promotable, next_layer, archived
        assert "promotable" in mem
        assert "next_layer" in mem
        assert mem["archived"] is False

    def test_field_order_is_deterministic(self, engine):
        """The dict insertion order is the same for every item so the
        embedded JSON is byte-for-byte deterministic across runs."""
        a = _persisted_item(item_id="a")
        b = _persisted_item(item_id="b", layer="global", stage="archived")
        payload = inspectview.build_inspect_payload([a, b], engine)
        assert list(payload["memories"][0].keys()) == list(payload["memories"][1].keys())
        # Spot-check the canonical order. ``display_title`` (issue #85)
        # comes right after ``id`` — the two together form the heading +
        # secondary-label pair for every list row.
        assert list(payload["memories"][0].keys())[:6] == [
            "id",
            "display_title",
            "layer",
            "stage",
            "tags",
            "content",
        ]

    def test_no_leaked_memory_objects(self, engine):
        """The projection MUST NOT leak the original front-matter dict —
        every payload value is a JSON primitive (str, int, float, bool, list,
        None) so the embedded JSON survives ``json.dumps``."""
        item = _persisted_item()
        item["extra_leaked_field"] = object()  # un-serialisable
        payload = inspectview.build_inspect_payload([item], engine)
        # If the projection had leaked extra_leaked_field, json.dumps would
        # raise TypeError. The fact that this serialises proves the field
        # set is whitelisted.
        json.dumps(payload)

    def test_missing_id_falls_back_to_path_stem(self, engine):
        item = _persisted_item(item_id="", path="/tmp/wiki/projects/fallback-id.md")
        item["id"] = ""  # explicit empty
        payload = inspectview.build_inspect_payload([item], engine)
        assert payload["memories"][0]["id"] == "fallback-id"

    def test_tags_non_string_entries_stripped(self, engine):
        item = _persisted_item(tags=["good", 42, None, "ok"])
        payload = inspectview.build_inspect_payload([item], engine)
        assert payload["memories"][0]["tags"] == ["good", "ok"]

    def test_every_memory_carries_display_title(self, engine):
        # Two items with very different shapes: heading-style markdown +
        # multi-line, and empty content driving the slug-id fallback.
        a = _persisted_item(item_id="m-a", content="# Architecture decision\nbody")
        b = _persisted_item(item_id="feat-foo-85", content="")
        payload = inspectview.build_inspect_payload([a, b], engine)
        titles = [m["display_title"] for m in payload["memories"]]
        assert titles == ["Architecture decision", "feat-foo-85"]

    def test_display_title_uses_raw_content_not_truncated_preview(self, engine):
        """The heading is derived from the FULL content even when the
        preview is truncated — the operator's stable label for a memory
        should not change with the --preview-width flag."""
        long = "First meaningful heading goes here\n" + "x" * 500
        item = _persisted_item(content=long)
        payload = inspectview.build_inspect_payload([item], engine, preview_width=5)
        mem = payload["memories"][0]
        # Preview is truncated…
        assert mem["preview_truncated"] is True
        assert mem["content"] == "First" + "..."
        # …but the heading is still the (full, first-line) derivation.
        assert mem["display_title"] == "First meaningful heading goes here"


# --------------------------------------------------------------------------- #
# Truncation behaviour
# --------------------------------------------------------------------------- #
class TestTruncation:
    def test_short_content_not_truncated(self, engine):
        item = _persisted_item(content="abc")
        payload = inspectview.build_inspect_payload([item], engine, preview_width=10)
        mem = payload["memories"][0]
        assert mem["content"] == "abc"
        assert mem["preview_truncated"] is False

    def test_long_content_truncated_with_ellipsis(self, engine):
        item = _persisted_item(content="x" * 500)
        payload = inspectview.build_inspect_payload([item], engine, preview_width=10)
        mem = payload["memories"][0]
        assert mem["content"] == "x" * 10 + "..."
        assert mem["preview_truncated"] is True

    def test_full_flag_bypasses_truncation(self, engine):
        item = _persisted_item(content="x" * 500)
        payload = inspectview.build_inspect_payload(
            [item], engine, preview_width=10, full=True
        )
        mem = payload["memories"][0]
        assert mem["content"] == "x" * 500
        assert mem["preview_truncated"] is False


# --------------------------------------------------------------------------- #
# promotable / next_layer precomputation — matches PolicyEngine
# --------------------------------------------------------------------------- #
class TestPromotionPrecomputation:
    def test_ephemeral_at_threshold_promotable_to_working(self, engine):
        item = _persisted_item(
            layer="ephemeral",
            quality_score=0.4,  # > 0.3
            access_count=2,     # >= 1
        )
        payload = inspectview.build_inspect_payload([item], engine)
        mem = payload["memories"][0]
        assert mem["promotable"] is True
        assert mem["next_layer"] == "working"
        # And it matches the engine directly.
        assert engine.check_promotion_eligible(item) is True

    def test_session_below_threshold_not_promotable(self, engine):
        item = _persisted_item(
            layer="session",
            quality_score=0.1,  # < 0.6
            access_count=0,
        )
        payload = inspectview.build_inspect_payload([item], engine)
        mem = payload["memories"][0]
        assert mem["promotable"] is False
        assert mem["next_layer"] is None

    def test_global_at_top_has_no_next_layer(self, engine):
        item = _persisted_item(layer="global", quality_score=1.0, access_count=99)
        payload = inspectview.build_inspect_payload([item], engine)
        mem = payload["memories"][0]
        # Top of chain — promotion_eligible returns False because
        # promotes_to is None.
        assert mem["promotable"] is False
        assert mem["next_layer"] is None

    def test_unknown_layer_yields_not_promotable(self, engine):
        item = _persisted_item(layer="unknown-layer")
        payload = inspectview.build_inspect_payload([item], engine)
        mem = payload["memories"][0]
        assert mem["promotable"] is False
        assert mem["next_layer"] is None


# --------------------------------------------------------------------------- #
# Archive flag derivation
# --------------------------------------------------------------------------- #
class TestArchiveFlag:
    def test_archived_stage_yields_true(self, engine):
        item = _persisted_item(stage="archived")
        payload = inspectview.build_inspect_payload([item], engine)
        assert payload["memories"][0]["archived"] is True

    def test_non_archived_stage_yields_false(self, engine):
        item = _persisted_item(stage="stored")
        payload = inspectview.build_inspect_payload([item], engine)
        assert payload["memories"][0]["archived"] is False


# --------------------------------------------------------------------------- #
# render_html — placeholder substitution
# --------------------------------------------------------------------------- #
class TestRenderHtml:
    def test_substitutes_placeholder_exactly_once(self, engine, tmp_path):
        tpl = tmp_path / "inspect.html"
        tpl.write_text(
            '<script id="inspect-data" type="application/json">'
            "__INSPECT_DATA_JSON__</script>"
        )
        payload = inspectview.build_inspect_payload([], engine)
        out = inspectview.render_html(payload, template_path=tpl)
        assert "__INSPECT_DATA_JSON__" not in out
        # The substituted JSON must be parseable.
        m = re.search(
            r'<script id="inspect-data" type="application/json">(.*?)</script>',
            out,
        )
        assert m is not None
        parsed = json.loads(m.group(1))
        assert parsed["schema_version"] == 1

    def test_default_template_path_uses_importlib_resources(self, engine):
        """No ``template_path`` falls back to ``files('core.templates')`` —
        the warning-free 3.11+ API form pinned by issue #68."""
        out = inspectview.render_html(
            inspectview.build_inspect_payload([], engine),
            template_path=None,
        )
        assert '<script id="inspect-data"' in out
        assert "__INSPECT_DATA_JSON__" not in out


# --------------------------------------------------------------------------- #
# write_inspect_html — file I/O and parent-dir creation
# --------------------------------------------------------------------------- #
class TestWriteInspectHtml:
    def test_creates_parent_directories(self, engine, tmp_path):
        nested = tmp_path / "deep" / "deeper" / "out.html"
        item = _persisted_item()
        written = inspectview.write_inspect_html(
            [item],
            nested,
            engine,
        )
        assert nested.exists()
        assert written.is_absolute()
        text = nested.read_text(encoding="utf-8")
        assert '<script id="inspect-data"' in text
        assert "__INSPECT_DATA_JSON__" not in text

    def test_returns_resolved_absolute_path(self, engine, tmp_path):
        out = tmp_path / "out.html"
        written = inspectview.write_inspect_html([], out, engine)
        assert written == out.resolve()


# --------------------------------------------------------------------------- #
# Issue #92 — ``content_full`` is always emitted (drill-down full content)
# --------------------------------------------------------------------------- #
class TestContentFullField92:
    """The drill-down panel needs the original untruncated content even when
    the list rows render a preview-truncated string. Issue #92 promotes
    ``content_full`` to a first-class always-present field so the template
    can read it unconditionally — no ``--full`` toggle required."""

    def test_content_full_always_present_when_truncated(self, engine):
        # Long ASCII content + a tiny preview_width forces truncation.
        long = "x" * 500
        item = _persisted_item(content=long)
        payload = inspectview.build_inspect_payload(
            [item], engine, preview_width=10
        )
        mem = payload["memories"][0]
        # The list-preview field is the truncated string …
        assert mem["content"] == "x" * 10 + "..."
        assert mem["preview_truncated"] is True
        # … but ``content_full`` carries the verbatim raw content.
        assert mem["content_full"] == long
        assert "content_full" in mem

    def test_content_full_always_present_when_short(self, engine):
        # Short content fits in the preview — the two fields should be
        # equal but ``content_full`` must STILL be present as a separate key.
        item = _persisted_item(content="abc")
        payload = inspectview.build_inspect_payload(
            [item], engine, preview_width=240
        )
        mem = payload["memories"][0]
        assert mem["content"] == "abc"
        assert mem["content_full"] == "abc"
        assert mem["preview_truncated"] is False

    def test_content_full_equals_content_in_full_mode(self, engine):
        long = "x" * 500
        item = _persisted_item(content=long)
        payload = inspectview.build_inspect_payload(
            [item], engine, preview_width=10, full=True
        )
        mem = payload["memories"][0]
        # ``--full`` bypasses truncation on ``content`` — both fields equal.
        assert mem["content"] == long
        assert mem["content_full"] == long
        assert mem["preview_truncated"] is False

    def test_content_full_unicode_korean_round_trip(self, engine):
        # Korean content with a single very long line. The preview is
        # bounded by Unicode codepoints (not bytes); ``content_full``
        # must equal the original verbatim.
        ko_long = "가" * 300  # 300 Korean syllables, one logical line
        item = _persisted_item(content=ko_long)
        payload = inspectview.build_inspect_payload(
            [item], engine, preview_width=50
        )
        mem = payload["memories"][0]
        # Preview is capped at 50 codepoints + ellipsis marker.
        assert mem["preview_truncated"] is True
        # The preview is built by character-slice; assert codepoint count.
        prefix = mem["content"][: -len("...")] if mem["content"].endswith("...") else mem["content"]
        assert len(prefix) <= 50
        # The full content survives verbatim.
        assert mem["content_full"] == ko_long
        assert len(mem["content_full"]) == 300

    def test_content_full_present_for_every_memory(self, engine):
        a = _persisted_item(item_id="a", content="alpha")
        b = _persisted_item(item_id="b", content="beta " * 100)
        payload = inspectview.build_inspect_payload(
            [a, b], engine, preview_width=20
        )
        for mem in payload["memories"]:
            assert "content_full" in mem
            assert isinstance(mem["content_full"], str)

    def test_content_full_position_after_content_in_field_order(self, engine):
        """``content_full`` is added immediately after ``content`` so the
        emitted JSON stays grouped + deterministic across runs."""
        item = _persisted_item(content="hello")
        payload = inspectview.build_inspect_payload([item], engine)
        keys = list(payload["memories"][0].keys())
        i_content = keys.index("content")
        i_full = keys.index("content_full")
        assert i_full == i_content + 1
        # And ``preview_truncated`` still immediately follows ``content_full``.
        assert keys[i_full + 1] == "preview_truncated"
