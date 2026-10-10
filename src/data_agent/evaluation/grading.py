"""Deterministic assertions; no LLM judge and no SQL string equivalence."""

from decimal import Decimal
from typing import Any

from data_agent.evaluation.models import Expectations, ExpectedTable, Observation


def cell_equal(left: object, right: object, spec: ExpectedTable) -> bool:
    """Keep NULL, booleans, strings and numeric IDs distinct."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float, Decimal)) and isinstance(
        right, (int, float, Decimal)
    ):
        try:
            a, b = Decimal(str(left)), Decimal(str(right))
            return (
                a.is_finite()
                and b.is_finite()
                and abs(a - b)
                <= max(
                    Decimal(str(spec.abs_tol)),
                    Decimal(str(spec.rel_tol)) * max(abs(a), abs(b)),
                )
            )
        except (OverflowError, ValueError):
            return False
    return type(left) is type(right) and left == right


def _perfect_match(edges: list[list[int]]) -> bool:
    """Bipartite matching handles duplicates and overlapping float tolerances."""
    matched: dict[int, int] = {}

    def augment(left: int, seen: set[int]) -> bool:
        for right in edges[left]:
            if right in seen:
                continue
            seen.add(right)
            if right not in matched or augment(matched[right], seen):
                matched[right] = left
                return True
        return False

    return all(augment(left, set()) for left in range(len(edges)))


def compare_table(spec: ExpectedTable, actual: dict[str, Any] | None) -> list[str]:  # noqa: PLR0911 - individual validation failures
    if actual is None:
        return ["table: no result table returned"]
    columns, rows = actual.get("columns", []), actual.get("rows", [])
    if actual.get("metadata", {}).get("truncated"):
        return ["table: result is truncated; cannot claim equality"]
    if len(columns) != len(set(columns)) or set(columns) != set(spec.columns):
        return [f"table: columns differ, expected {spec.columns}, got {columns}"]
    if any(len(row) != len(columns) for row in rows):
        return ["table: malformed row width"]
    if actual.get("row_count", len(rows)) != len(rows):
        return ["table: row_count does not match returned rows"]
    if len(rows) != len(spec.rows):
        return [f"table: expected {len(spec.rows)} rows, got {len(rows)}"]
    indices = [columns.index(column) for column in spec.columns]
    aligned = [[row[i] for i in indices] for row in rows]

    def equal(left: list, right: list) -> bool:
        return all(cell_equal(a, b, spec) for a, b in zip(left, right, strict=True))

    if spec.ordered:
        valid = all(equal(a, b) for a, b in zip(spec.rows, aligned, strict=True))
    else:
        edges = [
            [j for j, row in enumerate(aligned) if equal(expected, row)]
            for expected in spec.rows
        ]
        valid = _perfect_match(edges)
    return (
        []
        if valid
        else [
            "table: row values or multiplicities differ"
            + (" (order matters)" if spec.ordered else "")
        ]
    )


def grade(expected: Expectations, actual: Observation) -> list[str]:
    failures = []
    if expected.task_dependencies is not None:
        before = {key: set(value) for key, value in expected.task_dependencies.items()}
        after = {key: set(value) for key, value in actual.task_dependencies.items()}
        if before != after:
            failures.append("planning: task dependencies differ")
    if expected.join_keys is not None and set(expected.join_keys) != set(
        actual.join_keys
    ):
        failures.append("planning: join keys differ")
    if actual.status != expected.status:
        failures.append(f"status: expected {expected.status}, got {actual.status}")
    if expected.agents is not None and set(expected.agents) != set(actual.agents):
        failures.append(f"routing: expected {expected.agents}, got {actual.agents}")
    if expected.table is not None:
        failures.extend(compare_table(expected.table, actual.table))
    for agent, spec in expected.agent_tables.items():
        evidence = [item for item in actual.evidence if item.get("agent_name") == agent]
        if len(evidence) != 1 or evidence[0].get("status") != "success":
            failures.append(
                f"table: {agent} requires exactly one successful evidence result"
            )
        else:
            failures.extend(
                f"table: {agent}: {error.removeprefix('table: ')}"
                for error in compare_table(spec, evidence[0].get("result"))
            )
    for task, status in expected.task_statuses.items():
        if actual.task_statuses.get(task) != status:
            failures.append(
                f"task {task}: expected {status}, got {actual.task_statuses.get(task)}"
            )
    if expected.error_contains is not None and expected.error_contains not in (
        actual.error or ""
    ):
        failures.append(f"error: missing {expected.error_contains!r}")
    failures.extend(
        f"warning: missing {text!r}"
        for text in expected.warning_contains
        if not any(text in warning for warning in actual.warnings)
    )
    failures.extend(
        f"answer: missing {text!r}"
        for text in expected.answer_contains
        if text not in actual.answer
    )
    return failures
