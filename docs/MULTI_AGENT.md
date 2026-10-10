# Multi-agent collaboration

`DataAgentFlow` now plans a bounded task graph instead of always selecting one
agent. Existing CLI, Chainlit and A2A entry points use this workflow by default.
No new credentials or database connections are required beyond the configured
agents. The planner and final summarizer use the intent-detection model settings.

## Execution flow

```text
Question + conversation history
              |
          plan_tasks
              |-- clarification needed --> clarify_tasks --> resume
              |
         execute_tasks
           /       \
      Task A       Task B       independent tasks run concurrently
           \       /
            Task C             receives A/B evidence through depends_on
              |
       summarize_results       optional deterministic keyed join
              |
       Answer + source results + warnings
```

A worker uses its own SQL generation, validation and query execution graph.
Multi-task workers return query data directly: the coordinator generates one
combined answer instead of asking each worker to narrate the same data. Each
worker receives a fresh state and runs without its own persistent checkpoint.
The parent graph retains conversation history. A single-task request keeps the
existing answer and visualization behavior.

## Using the existing interfaces

For example, with the existing Contoso data sources:

```powershell
data-agent query "分别查询订单总收入和在途货运数量，注明各自来源" -c contoso -v
data-agent chat -c contoso
```

The first question can use sales and inventory agents in parallel without
assuming that they share a product key. CLI output includes each task's status
and dependencies. Chainlit also shows per-task SQL and source data tables.
Clarification responses in Chainlit resume the pending workflow.

Programmatic usage:

```python
from data_agent import DataAgentFlow
from data_agent.models.collaboration import CollaborationSettings

async with DataAgentFlow(
    config_path="src/data_agent/config/contoso.yaml",
    collaboration_settings=CollaborationSettings(
        max_concurrency=3,
        task_timeout=120,
        model_timeout=60,
    ),
) as flow:
    output = await flow.run(
        "分别查询订单总收入和在途货运数量，注明各自来源",
        thread_id="analysis-session",
    )
    print(output["final_response"])
    for task in output["agent_results"]:
        print(task.task_id, task.agent_name, task.status, task.depends_on)
        print(task.result)
```

Supply the usual Azure model credentials through the project's existing
configuration/environment mechanism. To retain the original router, construct
`DataAgentFlow(..., enable_collaboration=False)`. The old single-name routing
system prompt applies to that legacy mode; collaboration mode uses its own
structured planning prompt rather than the old “return one agent name” prompt.

## What agent communication means here

The planner assigns unique `task_id` values and explicit `depends_on` lists.
Independent tasks execute in bounded parallel batches. A dependent task starts
only after all its predecessors finish successfully. It receives JSON containing
their task IDs, source agent names, questions, SQL, structured rows and statuses
through `collaboration_context`. This evidence is provided to SQL generation and
SQL repair as data, not as new system instructions.

For example, after verifying a real shared identity mapping, an inventory task
could find low-stock product IDs, then a catalog task could retrieve names and
prices for exactly those IDs. If the predecessor fails, the catalog task is
marked `blocked`; it does not silently issue an unrestricted replacement query.
Oversized dependency evidence is rejected rather than silently truncated.

Each executable plan is a fixed, validated dependency graph. Users can revise a
plan before execution through [human-in-the-loop confirmation](HUMAN_IN_THE_LOOP.md).
This is not an unlimited autonomous replanning or debate loop. By default, plans
using multiple agents or requesting a join pause for confirmation.

## Cross-source joins and result shape

An optional `JoinSpec` requests a full outer one-to-one join over explicitly
named shared output columns. Workers must return matching aliases and unique
rows at the requested grain. The join runs in Python, not in an LLM:

* Values are matched exactly, including their types. Missing matches remain null.
* Duplicate or null keys, absent columns, truncated results, failed required
  tasks and excessive joined rows cause an explicit warning instead of a join.
* Non-key columns are namespaced as `task_id.column` to preserve provenance.
* No inferred ID mapping, automatic many-to-many join or invented metric formula
  is supported. Put source-local aggregates in SQL and define business mappings
  explicitly before asking for entity-level comparisons.

The returned fields include:

| Field | Meaning |
| --- | --- |
| `collaboration_plan` | Validated tasks, dependencies and optional join specification |
| `agent_results` | Complete per-task SQL, structured result, status and error |
| `collaboration_warnings` | Partial failures, join refusal or summary fallback |
| `result` | Single-task data or the deterministic joined table; otherwise null |
| `final_response` | Combined answer with failure warnings where relevant |
| `generated_sql` | Single query or separate source-labelled query blocks, not executable cross-DB SQL |

If every task fails, `error` is `all_tasks_failed`. Partial success preserves the
successful results and lists failed/blocked tasks. Summarization failure falls
back to source status/answers; it does not discard the raw results. Summary
previews are labelled when rows are omitted; the full source data remains in
`agent_results`.

## Data and operational limits

The supplied Contoso sales schema has orders and customers, **not product-level
order lines**. It cannot answer product-level sales/stock replenishment questions
without additional data. Also, the sample Cosmos IDs (`prod-001`) and PostgreSQL
inventory IDs (`PROD-0001`) differ: configure a verified mapping or use aligned
data. The coordinator deliberately does not guess that they represent the same
entity. Data-source permissions and metric/time-window definitions remain the
application owner's responsibility.

Plans are limited to eight tasks. Concurrency is bounded per request, not globally
across all users. Async task timeouts stop waiting for a worker, but a synchronous
database operation dispatched to a thread may continue: configure database/driver
statement timeouts for server-side cancellation. Cross-source reads are not a
single transaction or a shared snapshot. Query limits can restrict the underlying
data even if the returned result is not marked truncated; conclusions must respect
the source SQL's filters and limits.

Multi-task chart generation is intentionally disabled: the local plotting backend
uses process-global state and is not safe for concurrent workers. The combined
answer and joined/source tables remain available. Single-task charts are unchanged.
The parent checkpoint store is still in memory.

## Offline validation

```powershell
$env:PYTHONPATH = "src"
python -m pytest tests/test_collaboration.py -o addopts="" -q
```

The tests use stubbed model responses and temporary SQLite databases. They cover
actual concurrent scheduling, dependency evidence, timeout/partial failure,
invalid plans, safe joins, clarification resume, repeated turns, and real worker
graphs against independent databases. Live Azure model and remote database
behavior requires credentials and a separate integration run.
