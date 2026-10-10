"""CLI entry points for dataset validation, replay, live runs and comparisons."""

import asyncio
from collections.abc import Callable
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import typer

from data_agent.agent import DataAgentFlow
from data_agent.config_loader import ConfigLoader
from data_agent.evaluation.metrics import EvalMetrics
from data_agent.evaluation.models import EvalCase, EvalSuite, Observation
from data_agent.evaluation.report import json_text, make_report, save_report
from data_agent.evaluation.runner import (
    CaseSession,
    LiveSession,
    ReplaySession,
    run_suite,
)
from data_agent.harness import RuntimePolicy
from data_agent.harness.judge import AnswerJudge
from data_agent.harness.regression import QualityGate, assert_comparable, evaluate_gate
from data_agent.llm import get_llm
from data_agent.models.collaboration import CollaborationSettings

app = typer.Typer(
    help="Validate datasets, evaluate agents and inspect local HTML reports."
)


def read_suite(path: Path) -> EvalSuite:
    return EvalSuite.model_validate_json(path.read_text(encoding="utf-8-sig"))


def load_replay(path: Path, suite: EvalSuite) -> dict[str, list[Observation]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported replay schema_version")
    if data.get("data_version") != suite.data_version:
        raise ValueError("Replay data_version differs from the suite")
    records = data["observations"]
    if set(records) != {case.id for case in suite.cases}:
        raise ValueError("Replay case IDs must exactly match the suite")
    for case in suite.cases:
        if len(records[case.id]) != len(case.steps):
            raise ValueError(f"Replay step count differs for {case.id}")
    return {
        key: [Observation.model_validate(item) for item in items]
        for key, items in records.items()
    }


def source_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _package_versions() -> dict[str, str]:
    result = {}
    for package in ("langgraph", "langchain-core", "langchain-openai", "sqlglot"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = "unavailable"
    return result


def live_factory(
    config: str, review_policy: str
) -> tuple[Callable[[EvalCase], CaseSession], dict[str, Any]]:
    cfg = (
        ConfigLoader.load_all()
        if config == "all"
        else (
            ConfigLoader.load(Path(config))
            if Path(config).is_file()
            else ConfigLoader.load_by_name(config)
        )
    )

    def factory(case: EvalCase) -> CaseSession:
        del case
        collector = EvalMetrics()
        flow = DataAgentFlow(
            config=cfg,
            callbacks=[collector],
            collaboration_settings=CollaborationSettings(review_policy=review_policy),
        )
        return LiveSession(flow, collector)

    models = {
        agent.name: {
            "provider": agent.llm_config.provider,
            "model": agent.llm_config.model,
            "temperature": agent.llm_config.temperature,
        }
        for agent in cfg.data_agents
    }
    samples = {}
    for agent in cfg.data_agents:
        if getattr(agent.datasource, "type", None) == "sample":
            path = Path(agent.datasource.source)
            samples[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return factory, {
        "config": config,
        "models": models,
        "sample_files_sha256": samples,
        "deployment": os.getenv("AZURE_OPENAI_DEPLOYMENT"),
        "review_policy": review_policy,
    }


@app.command("validate")
def validate(dataset: Path) -> None:
    """Check the dataset schema and assertion shapes without calling a model."""
    try:
        suite = read_suite(dataset)
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(
        f"Valid: {suite.name}; {len(suite.cases)} cases; {sum(len(c.steps) for c in suite.cases)} steps"
    )


@app.command("run")
def run(  # noqa: PLR0913, C901 - public CLI input validation boundary
    dataset: Path,
    *,
    mode: Annotated[
        Literal["replay", "live"],
        typer.Option(help="Replay does not call models or measure their quality."),
    ] = "replay",
    observations: Annotated[
        Path | None, typer.Option(help="Required JSON observations for replay.")
    ] = None,
    config: Annotated[
        str | None,
        typer.Option(help="Required for live: config name, YAML path, or all."),
    ] = None,
    output: Annotated[
        Path,
        typer.Option(
            help="New report directory; existing reports are not overwritten."
        ),
    ] = Path("outputs/evaluations/latest"),
    timeout: Annotated[float, typer.Option(min=0.1, max=3600)] = 120,
    allow_approvals: Annotated[
        bool,
        typer.Option(
            help="Explicitly allow authored approve steps in live evaluation."
        ),
    ] = False,
    review_policy: Annotated[
        Literal["cross_source", "always", "never"], typer.Option()
    ] = "cross_source",
    judge_deployment: Annotated[
        str | None,
        typer.Option(
            help="Opt in to paid answer judging; model/deployment for --judge-provider."
        ),
    ] = None,
    judge_provider: Annotated[
        Literal["deepseek", "azure_openai"],
        typer.Option(help="Provider for optional answer judging."),
    ] = "deepseek",
) -> None:
    """Run every case in its own conversation. Exit 1 if any assertion fails."""
    try:
        suite = read_suite(dataset)
        if (
            any(s.expect.judge for c in suite.cases for s in c.steps)
            and not judge_deployment
        ):
            raise ValueError(
                "Suite has judge assertions; pass --judge-deployment to explicitly enable model judging"
            )
        judge = None
        if judge_deployment:
            if not any(s.expect.judge for c in suite.cases for s in c.steps):
                raise ValueError("No judge assertions in suite")
            judge = AnswerJudge(
                get_llm(provider=judge_provider, deployment_name=judge_deployment)
            )
        if (output / "report.json").exists() or (output / "report.html").exists():
            raise ValueError("Report exists; choose a new --output directory")
        metadata = {
            "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
            "source_sha256": source_hash(),
            "packages": _package_versions(),
            "timeout_seconds": timeout,
            "judge": {
                "provider": judge_provider,
                "deployment": judge_deployment,
                "rubric_version": 1,
            }
            if judge
            else None,
            "harness_policy": RuntimePolicy.from_environment().report_config()
            if mode == "live"
            else None,
        }
        if mode == "replay":
            if observations is None:
                raise ValueError("Replay requires --observations")
            records = load_replay(observations, suite)

            def factory(case: EvalCase) -> CaseSession:
                return ReplaySession(records[case.id])

            metadata["observations_sha256"] = hashlib.sha256(
                observations.read_bytes()
            ).hexdigest()
        else:
            if not config or observations is not None:
                raise ValueError(
                    "Live requires --config and does not accept --observations"
                )
            if not allow_approvals and any(
                s.resume and s.resume.action == "approve"
                for c in suite.cases
                for s in c.steps
            ):
                raise ValueError(
                    "Suite contains scripted approvals; use --allow-approvals to authorize them"
                )
            factory, details = live_factory(config, review_policy)
            if (
                suite.data_files_sha256
                and details["sample_files_sha256"] != suite.data_files_sha256
            ):
                raise ValueError(
                    "Sample data does not match suite snapshot; enable sample mode with matching files/config"
                )
            metadata.update(details)
        results = asyncio.run(run_suite(suite, factory, timeout, judge))
        report = make_report(suite, results, mode, metadata)
        json_path, html_path = save_report(report, output)
    except (ValueError, OSError, KeyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"{mode}: {report['summary']['passed']}/{len(results)} cases passed")
    typer.echo(f"HTML: {html_path.resolve()}\nJSON: {json_path.resolve()}")
    if not all(case.passed for case in results):
        raise typer.Exit(1)


@app.command("compare")
def compare(baseline: Path, candidate: Path) -> None:
    """List regressions and improvements for the same dataset, data and run mode."""
    try:
        old, new = [
            json.loads(p.read_text(encoding="utf-8")) for p in (baseline, candidate)
        ]
        assert_comparable(old, new)
        before = {c["id"]: c["passed"] for c in old["cases"]}
        after = {c["id"]: c["passed"] for c in new["cases"]}
        if before.keys() != after.keys():
            raise ValueError("Report case IDs differ")
        typer.echo(
            json_text({
                "regressions": [k for k in before if before[k] and not after[k]],
                "improvements": [k for k in before if not before[k] and after[k]],
            })
        )
    except (ValueError, OSError, KeyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


@app.command("gate")
def gate_report(candidate: Path, policy: Path, baseline: Path | None = None) -> None:
    """Exit 0 for passed gates, 1 for failed gates, 2 for invalid inputs."""
    try:
        gate = QualityGate.model_validate_json(policy.read_text(encoding="utf-8-sig"))
        new = json.loads(candidate.read_text(encoding="utf-8-sig"))
        old = json.loads(baseline.read_text(encoding="utf-8-sig")) if baseline else None
        outcome = evaluate_gate(gate, new, old)
        typer.echo(json_text(outcome))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    if not outcome["passed"]:
        raise typer.Exit(1)
