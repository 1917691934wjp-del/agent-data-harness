"""Benchmark integrity and fail-closed per-source evidence grading."""

import json
from pathlib import Path

import pytest

from data_agent.evaluation.cli import load_replay, read_suite
from data_agent.evaluation.grading import grade
from data_agent.evaluation.models import Expectations, Observation
from data_agent.evaluation.report import make_report
from data_agent.evaluation.runner import ReplaySession, run_suite

ROOT = Path(__file__).resolve().parents[1] / "evals" / "benchmark200"


def test_splits_have_no_shared_cases_or_reference_queries():
    full = read_suite(ROOT / "suite.json")
    development = read_suite(ROOT / "development.json")
    acceptance = read_suite(ROOT / "acceptance.json")
    assert len(full.cases) == 200
    assert sum(len(case.steps) for case in full.cases) == 460
    assert len(development.cases) == 120 and len(acceptance.cases) == 80
    dev_ids, test_ids = (
        {c.id for c in development.cases},
        {c.id for c in acceptance.cases},
    )
    assert not dev_ids & test_ids
    assert dev_ids | test_ids == {c.id for c in full.cases}
    assert {c.id for c in read_suite(ROOT / "smoke.json").cases} <= dev_ids
    references = json.loads((ROOT / "reference_sql.json").read_text(encoding="utf-8"))

    def queries(ids):
        return {
            (row["source"], " ".join(row["sql"].lower().split()))
            for case_id in ids
            for row in references[case_id]
        }

    assert not queries(dev_ids) & queries(test_ids)
    assert len(queries(dev_ids | test_ids)) == 150
    for case in full.cases:
        for reference in references[case.id]:
            expected = case.steps[reference["step"] - 1].expect
            assert (
                expected.table is not None
                or reference["agent"] in expected.agent_tables
            )
            assert len(reference["reference_table"]["rows"]) <= 200


@pytest.mark.asyncio
async def test_oracle_and_eight_negative_mutations():
    suite = read_suite(ROOT / "suite.json")
    for filename, count in [("oracle_replay.json", 200), ("negative_replay.json", 192)]:
        observations = load_replay(ROOT / filename, suite)
        results = await run_suite(
            suite, lambda case: ReplaySession(observations[case.id])
        )
        assert sum(result.passed for result in results) == count
        report = make_report(suite, results, "replay", {})
        assert report["summary"]["total_tokens"] is None
        assert report["summary"]["p95_ms"] is None
        assert (
            report["summary"]["harness_metrics"]["result_accuracy"] < 1
            if count < 200
            else report["summary"]["harness_metrics"]["result_accuracy"] == 1
        )


def test_agent_tables_require_single_successful_untruncated_evidence():
    spec = Expectations(
        status="success", agent_tables={"sales": {"columns": ["total"], "rows": [[5]]}}
    )
    good = Observation(
        status="success",
        evidence=[
            {
                "agent_name": "sales",
                "status": "success",
                "result": {"columns": ["total"], "rows": [[5]], "row_count": 1},
            }
        ],
    )
    assert grade(spec, good) == []
    candidates = [
        Observation(status="success"),
        good.model_copy(deep=True),
        good.model_copy(deep=True),
        good.model_copy(deep=True),
        good.model_copy(deep=True),
    ]
    candidates[1].evidence *= 2
    candidates[2].evidence[0]["result"]["rows"] = [[999]]
    candidates[3].evidence[0]["status"] = "error"
    candidates[4].evidence[0]["result"]["metadata"] = {"truncated": True}
    assert all(
        any(error.startswith("table:") for error in grade(spec, candidate))
        for candidate in candidates
    )


def test_reference_gold_is_not_passed_to_model_question():
    suite = read_suite(ROOT / "suite.json")
    for case in suite.cases:
        for step in case.steps:
            if step.question:
                assert "SELECT " not in step.question.upper()
            if step.resume and step.resume.feedback:
                assert "SELECT " not in step.resume.feedback.upper()
