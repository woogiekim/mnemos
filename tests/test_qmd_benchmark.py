"""Offline labelled evaluation for the optional QMD adapter contract."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest


FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "qmd-korean-paraphrases.json"
)
REPORT_PATH = FIXTURE_PATH.with_name("qmd-korean-paraphrases-report.json")


def test_success_case_korean_paraphrase_fixture_is_bounded_and_low_overlap() -> None:
    """TC-109: the offline corpus must exercise paraphrase, not keyword identity."""
    from core.qmd_benchmark import token_overlap

    # given
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    cases = payload["cases"]

    # when
    overlaps = [token_overlap(case["query"], case["relevant_content"]) for case in cases]

    # then
    assert payload["evidence_type"] == "synthetic_adapter_contract"
    assert 30 <= len(cases) <= 50
    assert len({case["case_id"] for case in cases}) == len(cases)
    assert max(overlaps) <= 0.25


def test_success_case_qmd_evaluator_reports_recall_mrr_and_p95() -> None:
    """TC-109: labelled synthetic rankings produce reproducible retrieval metrics."""
    from core.qmd_benchmark import evaluate_fixture

    # given
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    # when
    sut = evaluate_fixture(payload, top_k=5)

    # then
    assert sut == {
        "schema_version": 1,
        "evidence_type": "synthetic_adapter_contract",
        "case_count": 30,
        "top_k": 5,
        "lexical": {
            "recall_at_5": 0.333333,
            "mrr": 0.222222,
            "p95_latency_ms": 29.0,
        },
        "qmd": {
            "recall_at_5": 1.0,
            "mrr": 1.0,
            "p95_latency_ms": 39.0,
        },
        "claim_boundary": "Synthetic fixture evidence; not a live QMD or model benchmark.",
    }


def test_failure_case_qmd_evaluator_rejects_invalid_fixture() -> None:
    """Malformed or duplicate labels must fail instead of producing a metric claim."""
    from core.qmd_benchmark import BenchmarkFixtureError, evaluate_fixture

    # given
    payload = {
        "schema_version": 1,
        "evidence_type": "synthetic_adapter_contract",
        "cases": [
            {
                "case_id": "duplicate",
                "relevant_id": "memory-a",
                "lexical_results": [],
                "qmd_results": [],
                "lexical_latency_ms": 1,
                "qmd_latency_ms": 2,
            },
            {
                "case_id": "duplicate",
                "relevant_id": "memory-b",
                "lexical_results": [],
                "qmd_results": [],
                "lexical_latency_ms": 1,
                "qmd_latency_ms": 2,
            },
        ],
    }

    # when
    try:
        evaluate_fixture(payload)
    except BenchmarkFixtureError as exc:
        sut = str(exc)
    else:
        sut = ""

    # then
    assert sut == "benchmark case_id values must be unique"


@pytest.mark.parametrize(
    ("payload", "top_k", "expected"),
    [
        ([], 5, "benchmark fixture must be an object"),
        ({}, 5, "benchmark fixture must contain cases"),
        ({"cases": []}, 5, "benchmark fixture must contain cases"),
        ({"cases": [{}]}, 5, "every benchmark case requires case_id"),
        (
            {
                "cases": [
                    {
                        "case_id": "invalid-ranking",
                        "relevant_id": "memory-a",
                        "lexical_results": None,
                        "qmd_results": [],
                        "lexical_latency_ms": 1,
                        "qmd_latency_ms": 1,
                    }
                ]
            },
            5,
            "case invalid-ranking has invalid lexical ranking",
        ),
        (
            {
                "cases": [
                    {
                        "case_id": "bool-latency",
                        "relevant_id": "memory-a",
                        "lexical_results": [],
                        "qmd_results": [],
                        "lexical_latency_ms": True,
                        "qmd_latency_ms": 1,
                    }
                ]
            },
            5,
            "case bool-latency has invalid lexical latency",
        ),
        (
            {
                "cases": [
                    {
                        "case_id": "text-latency",
                        "relevant_id": "memory-a",
                        "lexical_results": [],
                        "qmd_results": [],
                        "lexical_latency_ms": "not-a-number",
                        "qmd_latency_ms": 1,
                    }
                ]
            },
            5,
            "case text-latency has invalid lexical latency",
        ),
        (
            {
                "cases": [
                    {
                        "case_id": "negative-latency",
                        "relevant_id": "memory-a",
                        "lexical_results": [],
                        "qmd_results": [],
                        "lexical_latency_ms": -1,
                        "qmd_latency_ms": 1,
                    }
                ]
            },
            5,
            "case negative-latency has invalid lexical latency",
        ),
    ],
)
def test_failure_case_qmd_evaluator_rejects_unsafe_metric_inputs(
    payload,
    top_k: int,
    expected: str,
) -> None:
    """Invalid labels and timings must fail before publishing metrics."""
    from core.qmd_benchmark import BenchmarkFixtureError, evaluate_fixture

    # given
    expected_message = expected

    # when
    with pytest.raises(BenchmarkFixtureError) as captured:
        evaluate_fixture(payload, top_k=top_k)
    sut = str(captured.value)

    # then
    assert sut == expected_message


def test_failure_case_qmd_evaluator_rejects_non_positive_top_k() -> None:
    """Recall cutoffs must be positive before backend evaluation starts."""
    from core.qmd_benchmark import BenchmarkFixtureError, evaluate_fixture

    # given
    payload = {"cases": [{"case_id": "valid-id"}]}

    # when
    with pytest.raises(BenchmarkFixtureError) as captured:
        evaluate_fixture(payload, top_k=0)

    # then
    assert str(captured.value) == "top_k must be positive"


def test_success_case_qmd_evaluate_cli_emits_labelled_json() -> None:
    """Operators can rerun the synthetic evaluation without importing Python."""
    from core.cli import cli

    # given
    sut = CliRunner()

    # when
    result = sut.invoke(
        cli,
        ["qmd-evaluate", "--fixture", str(FIXTURE_PATH), "--json"],
    )

    # then
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["evidence_type"] == "synthetic_adapter_contract"
    assert payload["case_count"] == 30
    assert payload["claim_boundary"].startswith("Synthetic fixture evidence")


def test_success_case_qmd_evaluate_cli_emits_human_readable_metrics() -> None:
    """Text output keeps evidence type, both backends, and claim boundary visible."""
    from core.cli import cli

    # given
    sut = CliRunner()

    # when
    result = sut.invoke(cli, ["qmd-evaluate", "--fixture", str(FIXTURE_PATH)])

    # then
    assert result.exit_code == 0
    assert "evidence=synthetic_adapter_contract cases=30 top_k=5" in result.output
    assert "lexical: recall@5=" in result.output
    assert "qmd: recall@5=" in result.output
    assert "not a live QMD or model benchmark" in result.output


def test_failure_case_qmd_evaluate_cli_rejects_invalid_json(tmp_path: Path) -> None:
    """Malformed fixture bytes must not produce partial metric output."""
    from core.cli import cli

    # given
    fixture = tmp_path / "invalid.json"
    fixture.write_text("not-json", encoding="utf-8")
    sut = CliRunner()

    # when
    result = sut.invoke(cli, ["qmd-evaluate", "--fixture", str(fixture)])

    # then
    assert result.exit_code == 1
    assert result.output.startswith("Error: Expecting value")


def test_success_case_tracked_qmd_report_matches_fixture() -> None:
    """Documentation metrics must be regenerated when the labelled corpus changes."""
    from core.qmd_benchmark import evaluate_fixture

    # given
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    expected = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    # when
    sut = evaluate_fixture(fixture, top_k=5)

    # then
    assert sut == expected
