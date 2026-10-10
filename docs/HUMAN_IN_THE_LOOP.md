# Human-in-the-loop

The collaboration workflow pauses before selected plans execute. The user can
approve the displayed plan, revise their request, or cancel without running its
workers. This complements the existing clarification step.

```text
plan_tasks -> clarify_tasks (if information is missing)
           -> review_plan -> approve -> execute_tasks -> summarize_results
                          -> revise  -> plan_tasks
                          -> cancel  -> END
```

## Default behavior

`CollaborationSettings.review_policy` supports:

| Value | When confirmation is requested |
| --- | --- |
| `cross_source` (default) | Plans using multiple registered agents or requesting a join |
| `always` | Every executable collaboration plan |
| `never` | No initial plan confirmation; clarification still works |

The default is a deterministic rule based on agent boundaries, not an LLM risk
score or database query-cost estimate. Separate agents may connect to the same
physical database. The legacy `enable_collaboration=False` route is unchanged
and does not gain plan confirmation.

After a user revises a reviewed plan, the new plan requires confirmation even if
it now uses a single agent, including when another clarification is needed first.
Replanning validates task IDs, dependencies and registered agents again. Approval
does not bypass SQL validation, read restrictions, timeouts or join checks.

## CLI and web

The CLI and Chainlit show the planned tasks, dependencies and optional join keys.
Reply with:

* `approve` or `确认` to execute the displayed plan.
* `cancel` or `取消` to cancel it.
* Any other text to describe a revision. It is added to the question and replanned.

These are text-based controls, not approval buttons. Whitespace-only revisions
are invalid and remain paused. The CLI continues through repeated clarification
and confirmation steps; interrupting its review prompt cancels the plan. In the
web, replies are bound to the version most recently displayed in that session.
Reviewing a plan authorizes its described queries, not an exact SQL string: SQL
generation and validation occur afterwards in the worker.

## Python API

```python
from data_agent.agent import DataAgentFlow
from data_agent.models.collaboration import CollaborationSettings

flow = DataAgentFlow(
    config_path="src/data_agent/config/contoso.yaml",
    collaboration_settings=CollaborationSettings(review_policy="cross_source"),
)
result = await flow.run("Compare data across the available sources", thread_id="chat-1")
if result.get("__interrupt__"):
    pending = result["__interrupt__"][0].value
    # Display pending['message'] and wait for a real user decision.
    # For a plan_review, submit the version actually shown to that user:
    decision = {
        "action": "approve",  # or 'cancel', or 'revise' with nonempty feedback
        "plan_version": pending["plan_version"],
    }
    # Call only after the user has confirmed:
    result = await flow.run(thread_id="chat-1", resume_value=decision)
```

For `clarification_needed`, submit `{"question": "the clarified request"}` or
`{"action": "cancel"}`. For plan revisions, submit `action="revise"` plus the
displayed `plan_version` and `feedback`. Handle `__interrupt__` after every call,
including resumes; a revision or stale decision may pause again.

`await flow.get_pending_input(thread_id)` reads the pending payload without
replanning. If `thread_id` was omitted on the initial call, the interrupted result
includes the generated `thread_id` for resuming. New questions cannot overwrite a
pending interaction. A duplicate resume after completion is rejected. Same-thread
overlapping calls on one flow instance are rejected rather than racing checkpoints.

## A2A

The executor publishes `input-required` and ends the current response stream while
leaving the task pending. The status message includes `metadata.human_input` with
the interrupt type, plan version, plan and display text. No completed artifact is
published until execution has finished.

Continue the same A2A task and context. For plan review, include the version the
client displayed in the incoming message's metadata:

```json
{
  "review_decision": {
    "action": "revise",
    "plan_version": "version-returned-in-human_input",
    "feedback": "Only include last month"
  }
}
```

Use `approve` or `cancel` as `action` for the other decisions. Plain text alone
does not authorize a plan through A2A. Missing, invalid and stale decisions remain
paused. Clarifications use the message text. Completed, failed and canceled
queries are mapped to their respective task states. The A2A cancel operation
supports tasks paused for input; cancellation of running database queries remains
unsupported.

## State and deployment boundaries

* Every generated plan receives a checkpointed UUID version. Resuming a review
  does not regenerate the plan; an approval for an older version is rejected.
* Accepted decisions are recorded as `plan_review_decision` messages with the
  action, version and revision feedback. This is a conversation record, not a
  durable compliance audit or authenticated approver identity.
* The default saver is still `InMemorySaver`. Process restarts lose pending input.
  The Chainlit chat-resume hook currently initializes a new flow/session; it does
  not restore old paused conversations across reconnects.
* `DataAgentFlow(..., checkpointer=saver)` accepts an application-managed saver.
  Configure and close that saver in the hosting application's lifecycle. Merely
  exposing this parameter does not provision persistent storage. Stable thread
  IDs and reconnect integration are also required.
* Authentication and ownership checks for user, A2A task and conversation remain
  the hosting service's responsibility. A thread ID is not an authorization token.
  The in-process overlap guard is not a distributed lock; multi-instance services
  need shared concurrency control around each conversation.

## Offline tests

```powershell
$env:PYTHONPATH = "src"
.venv/Scripts/python.exe -m pytest tests/test_human_review.py tests/test_collaboration.py -o addopts="" -q
```

Tests use stubbed models and temporary SQLite databases; no paid model or remote
database is required. Entry-point tests import the actual CLI, Chainlit and A2A
modules and replace only their presentation/event interfaces. Install the
project's declared dependencies before running them. Remote-service integration
and browser appearance still need a configured deployment.
