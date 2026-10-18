# Delivery client runtime adapter

The delivery client keeps its existing HTTP routes and HTML. It is not a second
Agent executor or a replacement Coding IDE. Tools still execute through the core
Agent, ExecutionContext, Policy, ToolRegistry and ActionExecutor.

## Host setup

```python
from client.server import ClientServer
from forge.client_runs import RunStore

store = RunStore(control_home / 'sessions' / 'client')
server = ClientServer(('127.0.0.1', 7712), workspace=project, store=store)
try:
    server.serve_forever()
finally:
    server.server_close()
    store.close()
```

`python client/server.py --workspace PROJECT` now actually uses PROJECT for task
execution and file browsing. Request workspaces may only select descendants of
that host-configured directory. Requests cannot provide a control home, registry,
trusted effect or ExecutionContext. Permissions/expose still come from the host's
existing composed config. Invalid selectors fail before a task is created.

The CLI enables durable projections at `~/.forge/sessions/client/`. Plain imports
and programmatic servers default to an in-memory store; inject a persistent store
explicitly when embedding. Each live execution has one owning server process;
cross-server cancellation/worker handoff is not provided.

## Task identity and outcomes

`POST /api/run` returns HTTP 202 with `run_id`, `task_id`, `protocol_version: 1`.
New IDs are 32 lowercase hex characters. Routes continue accepting legacy 12-hex
IDs, but old in-memory histories cannot be recovered retroactively.

The same run/task identity reaches core events, action results, session and final
RunResult. Core Agent gains a compatible optional keyword:

```python
request = RunRequest(entry='client')
report = agent.run(task, run_request=request)
result = report.as_result().as_dict()
```

Allocate a fresh request for each new execution. With a Session, admission is
recorded/synced under an OS lock before running. Reusing its run ID in that same
journal is rejected, including after restart. Without a journal this durable
admission check is unavailable. This is not an exactly-once guarantee for tools,
nor a global run-ID reservation across unrelated journals.

Legacy UI states remain `queued`, `running`, `cancelling`, `done`, `error`,
`cancelled`. Inspect normalized `outcome`, `execution_state`, `error_code` and
`requires_inspection` rather than interpreting text:

- `done`: core RunResult succeeded; this says the Agent loop finished, not that
  every test or action succeeded. Inspect correlated tool/test evidence.
- `error`: includes limits, ordinary failure and unknown side effects.
- `cancelled`: the core acknowledged cancellation at a cooperative boundary.
- Restarted unfinished records: `state=error`, `outcome=unknown`,
  `execution_state=unknown`, `error_code=execution_detached`. No worker, permission
  or tool action is reconstructed. Inspect the session/Diff and create a NEW task.

The projection journal preserves submitted/active facts; detached status is a
host-local recovery projection, not evidence that another process has exited.
`sessionPath` links to the authoritative execution journal. No automatic rollback
or unknown-action replay is performed.

## Events and reconnect

- `GET /api/run/{id}/events`: SSE, preserving existing `data:` frames.
- `Last-Event-ID: N` or `?after=N`: return events after that delivery cursor.
- `?format=json&after=N`: `{events, next_after, has_more, reset_required, state}`,
  at most 200 events per page.

Every persisted delivery event has `cursor`, `event_id`, `client_run_id` and v1
identity fields. The SSE `id:` is the delivery cursor. It is distinct from the
execution journal's `ordinal`. Preserve child `run_id`/`parent_run_id`; correlate
the stream itself by `client_run_id`, and tasks by `task_id`.

Core events arrive during execution. Legacy `step` events are compatibility views
of `tool_call`; deduplicate/update action cards by `action_id`, not by counting
`tool_completed`, `tool_call` and `step` as separate executions. Deduplicate raw
events by `event_id`. The legacy HTML continues displaying `step`/`state` frames.

The live event cache is bounded. Persistent stores replay older pages from disk
when necessary; cursors do not use array positions. An ephemeral store may emit
`reset_required`/`type=reset`; reload the task snapshot before consuming its retained
events. Heartbeats are comments. A terminal frame ends the connection; missing
runs return HTTP 404 and invalid cursors HTTP 400.

## Cancellation and configuration

`POST /api/run/{id}/cancel` acknowledges `cancel_requested=true`, `state=cancelling`.
It sets that task's core cancellation token, including child Agents. It does not
claim an arbitrary in-flight model/native call has already stopped. Terminal runs
return HTTP 409. A late cancel request does not overwrite a genuine final result.
Server shutdown requests cooperative cancellation and waits up to five seconds
for owned workers; unresolved executions require inspection after restart.

`bundle=base|coding` selects existing composed bundles, not a path. `thinking`
patches the actual thinking config. A named `model` uses the existing explicit
phase-router binding and takes precedence over automatic routing; the strategy
config remains preserved, and `routing_mode=explicit` records that choice.

## Limits

No GUI layout changes or new editor/PTY/LSP APIs are included. Journals use the
existing secret redaction, append and sync mechanisms. History is retained; no
automatic disk retention policy is imposed. Startup reconstruction and historical
event replay are linear in journal size. There is no distributed live-worker
registry, browser authentication redesign or arbitrary tool interruption.
