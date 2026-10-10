"""Explicit field policies, supplementing SQL validation and DB permissions."""

import sqlglot
from sqlglot import exp

from data_agent.harness.budget import current_budget
from data_agent.models.outputs import QueryResult
from data_agent.validators.sql_validator import SQLValidator


def before_query(sql: str, dialect: str) -> None:
    budget = current_budget.get()
    if not budget:
        return
    budget.check()
    budget.consume("tool_calls")
    forbidden = {name.casefold() for name in budget.policy.forbidden_sql_columns}
    if not forbidden:
        return
    if dialect == "cosmosdb":
        budget.fail("Column restrictions require a supported SQL AST dialect")
    try:
        tree = sqlglot.parse_one(
            sql, dialect=SQLValidator.DIALECT_MAP.get(dialect, dialect)
        )
    except (sqlglot.errors.ParseError, ValueError):
        budget.fail("Unable to verify restricted SQL columns")
    if any(column.name.casefold() in forbidden for column in tree.find_all(exp.Column)):
        budget.fail("SQL references a forbidden column")
    if any(not isinstance(star.parent, exp.Count) for star in tree.find_all(exp.Star)):
        budget.fail("Wildcard SQL cannot prove restricted column exclusion")


def after_query(result: QueryResult) -> QueryResult:
    budget = current_budget.get()
    if not budget:
        return result
    budget.check()
    redacted = {name.casefold() for name in budget.policy.redact_columns}
    rows = [
        [
            "[REDACTED]" if column.casefold() in redacted else value
            for column, value in zip(result.columns, row, strict=True)
        ]
        for row in result.rows[: budget.policy.max_result_rows]
    ]
    metadata = dict(result.metadata)
    if len(result.rows) > len(rows):
        metadata["truncated"] = True
    if redacted.intersection(column.casefold() for column in result.columns):
        metadata["redacted"] = True
    return result.model_copy(
        update={"rows": rows, "row_count": len(rows), "metadata": metadata}
    )
