# Coding submission adapter contract

This adapter keeps the existing DevelopmentRuntime/EditorBridge/Agent pipeline.
It does not create a new executor, GUI layout or permissions system.

## Model identity

EditorBridge's catalog rows contain an opaque string `id` and a display `label`.
The `id` is not itself a `(provider, model)` tuple. Resolve it through the host's
`DevelopmentRuntime.catalog[id]` before `ModeState.task_snapshot(explicit_model=...)`.
Reject stale selections; never silently substitute another model for a named choice.

## Host controls

RuntimeContext exposes these view-only synchronization methods:

```python
context.set_model_options(['继承配置', *catalog_labels])
context.set_routing_primary(selected_label)
context.set_sandbox('read-only')
context.set_role('simple')
```

They do not dispatch tools or rewrite a running Agent. Coding currently supports
only the existing `read-only` / `workspace-write` execution grants. Higher Mode,
ReadScope, SecretScope and fallback/MOA fields are host-controlled displays, not
extra per-task capabilities. Unsupported editable preview controls are disabled.

## Submission and feedback

- Validate and capture model, grant, role, planning and attached buffer contents
  on the Tk thread before consuming the draft.
- Accepted local submission means a worker was requested, not that the Agent has
  completed a step. Core events and results remain the authoritative task state.
- Display accepted, queued and rejected feedback in the conversation, select that
  tab, and preserve a rejected draft. Do not rely on a transient status bar alone.
- Keep send available during execution for the existing three-entry FIFO.
- Freeze the accepted task's choices; automatic queue draining must not clear a
  newer draft or take its newly attached documents.
- Submission notices do not call `set_agent_status`: a queued/rejected draft must
  not relabel the currently executing Agent. ZCode status components should consume
  the existing execution events/results rather than infer Agent state from Send.

Project/session switching and competing lane operations wait for accepted and
queued submissions. Work/Coding visibility changes retain the existing behavior:
they do not cancel an already accepted background task.

## Cancellation

The runtime adds one compatible optional argument:

```python
from threading import Event

cancel = Event()  # Allocate at host acceptance, before the worker takes the lane.
cancel.set()     # May happen before runtime.run starts.
result = runtime.run(prompt, snapshot=snapshot, planning=planning,
                     contexts=contexts, cancellation=cancel)
```

If omitted, Runtime still allocates its own Event. The supplied token reaches the
existing Agent/ToolContext and child cancellation checks; it is not an authorization
grant. Cancel clears this workbench's queued requests and requests a cooperative
stop of its active submission. Arbitrary in-flight calls may take time to return.

The UI's pending-submission flag covers only the interval before Runtime admission;
it is not a second durable execution state. A running task's identity, outcomes,
unknown actions and actual model continue to come from the existing core protocol.

## Integration boundaries

The minimal production patch touches only DevelopmentWorkbench, policy widgets
and `forge/development/runtime.py`. It does not depend on the separate HTTP-client
RunStore changes. A reviewed preview combines it with the current uncommitted
Coding components and original mainline core; the original layout/status methods
are preserved. Recheck snapshot hashes before applying that patch to a busy worktree.
Running Python GUI processes need a deliberate reload/restart to load changed source.
