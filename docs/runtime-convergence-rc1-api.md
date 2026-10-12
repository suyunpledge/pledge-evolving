# Runtime Convergence RC1 — public adapter contract (v1)

RC1 owns the core execution path, not the GUI layout. ZCode owns GUI integration.
`forge.execution` is the public boundary. Do not read `Agent._run_id`, mutate its
policy/router from a view, or dispatch tools directly from a webview/plugin.

## Structures

| Structure | Required contract |
|---|---|
| `RunRequest` | `run_id`, `task_id`, `parent_run_id`, `entry`, `protocol_version=1`; generated IDs by default |
| `ExecutionContext` | host-owned workspace/home, frozen Policy/sandbox, scoped Registry/expose/activation, journal, cancellation, capabilities, optional checkpoint/revision guard |
| `ActionRequest` | `name`, `arguments`, `action_id`, optional `tool_call_id` |
| `ActionResult` | run/task/parent/action identity, `outcome`, `error_code`, `execution_state`, `authorization`, content/error/meta |
| `EventEnvelope` | `event_id`, protocol version, identities, `type`, flattened legacy payload; persisted events have `ordinal` |
| `RunResult` | identities, outcome/error/state/text; obtained from `RunReport.as_result()` |

`as_dict()` is available on requests/results/events. `ExecutionContext.describe()`
provides a JSON-safe descriptor without credentials, callable handles or Registry
internals. Cancellation is also passed to `ToolContext.cancellation` for bounded
host handlers; it is not an authorization grant.

Outcomes: `succeeded`, `failed`, `cancelled`, `running`, `unknown`.
Execution states: `not_started`, `running`, `completed`, `unknown`.
Authorization: `allow`, `ask`, `deny`, `unknown`. A completed run means the Agent
loop finished; inspect action/test outcomes to establish that the user's acceptance
criteria passed. Do not infer authorization from an error message.
If any action's side-effect outcome is unknown, the normalized run result is also
unknown even if the model ended in prose. Recovery projections expose
unknown_actions/requires_inspection and never automatically replay them.

## Host registration

```python
from forge.policy import ToolEffect
from forge.tools import ToolRegistry, ToolSpec

registry = ToolRegistry(expose=["read_file", "approved_reader"])
# Only maintained/verified HOST code makes this decision.
registry.register(ToolSpec("approved_reader", "Scoped read", host_handler),
                  trusted=True, effect=ToolEffect.FILE_READ)
```

Plain `register(spec)` treats the effect as UNKNOWN and denies execution, including
in bypass mode. A manifest's `read_only`, tags, name, filename, or requested effect
does not establish trust. Host classifications are snapshotted separately from
mutable ToolSpec display fields. Native tags retain their isolation restriction.
Name-only `Policy.evaluate()` remains compatible for alias/rule checks; it is not
an authorization to dispatch an unclassified handler. Actual dispatch uses Registry.

`DELEGATED_WRITE` is exclusively for a verified host broker that already enforces
external confirmation/current grants and forwards to the real policy gate. It
does not hold a local workspace lock while awaiting another executor. READ_ONLY
mode and READ_ONLY sandbox still deny it. Do not use it for a filesystem writer.
The core connector broker explicitly registers this effect. The existing GUI
declarative plugin broker has a narrow compatibility adapter: it verifies the
actual loaded host class's lambda code object, owner and dispatch method, and
derives the effect from the broker's target; a forged code filename cannot pass.
No plugin-market logic or visual file was modified. Future host adapters should
use explicit registration rather than adding provenance exceptions.

## Direct host execution

```python
from pathlib import Path
from threading import Event
from forge.execution import ActionExecutor, ActionRequest, RunRequest, create_execution_context
from forge.policy import Policy, Mode
from forge.tools import build_builtin_registry

workspace = Path(project).resolve()
run = RunRequest(entry="coding")
cancel = Event()
ctx = create_execution_context(
    home=home, workspace=workspace,
    policy=Policy(mode=Mode.READ_ONLY, workspace=workspace, non_interactive=True),
    registry=build_builtin_registry(expose=["read_file", "list_dir"]),
    run=run, cancellation=cancel, subscriber=post_event_to_ui,
)
executor = ActionExecutor()
request = ActionRequest("read_file", {"path": "README.md"})
result = executor.execute(ctx, request)
status = executor.query(ctx, request.action_id)
cancel.set()
```

`subscriber` receives an already persisted/synced, secret-protected dict. Queue it
onto the GUI thread. De-duplicate by event_id; correlate by task/run/action, not
panel selection, array position or Agent display name. An observer exception does
not change the durable result. Explicitly injected Sessions remain host-owned;
the default per-run journal is automatically closed after execution.
`tool_completed` is the action terminal fact; legacy `tool_call` is a Step view.
Upsert by action_id rather than counting these as two tool executions.

Context construction requires a supplied Policy or composed config. Workspace
mismatches and persisted journal identity mismatches are rejected. Registry views
are cloned by default; Agent uses its already scoped view. Host capabilities such
as spawn/memory/skills must be attached explicitly; otherwise they cannot execute.
Do not reinstate write grants from a recovery log.

## Gateway adapter

The existing `/v1/tools/call` body/response remains compatible. New optional body:

```json
{
  "name": "write_file",
  "arguments": {
    "path": "value.py",
    "content": "VALUE = 2\n",
    "_expected_revisions": {"C:\\project\\value.py": "sha256-of-observed-bytes"}
  },
  "run_id": "run-001",
  "task_id": "task-001",
  "parent_run_id": "",
  "action_id": "action-001"
}
```

Use canonical absolute paths as revision map keys; missing files use JSON null.
Treat request IDs as correlation only: the server's Policy and expose remain the
authority. A model/plugin cannot supply an ExecutionContext or a trusted effect.
`plugin_context` remains required for the existing plugin gate and is additive.

| Endpoint | Behavior |
|---|---|
| `POST /v1/tools/call` | legacy ToolResult envelope; meta adds identities, normalized outcome/error/state and completed event_id |
| `GET /v1/runs/{run_id}/actions/{action_id}` | durable action status; known completion or unknown outcome; 404 if no action |
| `GET /v1/runs/{run_id}/events?after={ordinal}` | up to 200 events, `next_after`, `has_more`; polling cursor is per journal |
| `POST /v1/runs/{run_id}/cancel` | acknowledges `cancel_requested`; prevents new dispatch, does not promise an in-flight handler was interrupted |

New query/event/control endpoints require a configured Gateway key and enforce
its bearer token (503 without a key, 401 with missing/wrong authentication).
Legacy keyless model/tool endpoints remain compatible; they do not expose these
new journal data endpoints. This avoids turning fetch_url GET access into a way
to read other execution journals. Use the
existing protected scope header consistently for a run. Without run_id each
legacy call gets an independent identity; ZCode should allocate one run/task per
foreground task and forward it to all tool calls. Model proxy endpoints remain
independent of this action protocol.

A reused action identity with identical arguments returns its recorded result;
changed arguments produce `action_id_conflict`. An interrupted intent returns
`unknown_outcome` and is never replayed. Recovered Gateway runs are evidence-only:
query/deduplicate them, then create a NEW run (optionally parent_run_id=old run)
under current host permissions to continue. A frozen live run cannot gain grants
from a changed server config; current host restrictions also gate new submissions.

## CLI / EditorBridge compatibility

- `Agent.run(task, history=..., context_paths=...)` and ToolResult remain compatible.
- RunReport adds public identity, `as_result()`; Step adds action_id/meta.
- CLI JSON retains text/stopped/usage/planning/steps and adds identities, normalized
  result fields and sessionPath. Only `final` maps to success; step-limit/cancelled
  runs now return a nonzero exit code.
- EditorBridge keeps messageId/sessionPath and progress frames. messageId aliases
  the public run_id; progress data contains v1 event identities. Existing review
  target checks and permission modes remain in effect.
- Default `build_agent` logs use unique paths instead of sessions/current.jsonl.
  Explicit session_path is still supported for conversation lanes.

## Limits / next adapter work

ZCode should add identity arguments/event polling/cancel handling to its Gateway
client and consume public bridge/result fields. This task did not migrate the GUI
chat loop or dedicated Development test/restore publications to a new orchestrator.
Those legacy paths retain their existing checks and may still emit older event
shapes. Modes/layout state remain owned by ZCode.

Forge locks coordinate cooperating processes, including nested workspace paths.
Uncooperating editors are not locked. CLI/Gateway writes without expected revisions
remain permitted for compatibility; supply revisions for conflict detection.
Development's existing read-before-write guard supplies revisions automatically.
Checkpoint support remains optional (Git must be available). Arbitrary tool
failures can leave side effects: inspect Diff and explicitly restore a checkpoint;
there is no automatic rollback, automatic unknown-action replay, or exactly-once
guarantee. Gateway bounds live contexts to 256; persistent configured home is
required for restart queries, while legacy programmatic instances use temporary
storage. Session replay/query is currently linear in journal size.

The legacy shadow repository/history layout is preserved. Transactions lock both
workspace and shadow repository so different projects sharing one home cannot mix
the Git index during snapshot/restore. Select an explicit action checkpoint with
its checkpoint_workspace; global legacy history/rollback_last is not a substitute
for per-task selection. Keep control home outside the project when possible.
