# Coding Runtime readiness and host model settings

The workbench owns an isolated DevelopmentRuntime. Opening Coding does not borrow
the Work Agent, its Policy, tool registry, session, memory or checkpoints.

## Desktop host wiring

```python
state = ModeState(Path.home() / '.forge-development')
state.switch('development')
bench = DevelopmentWorkbench(parent, state, hosted=True,
                             workspace_request=workspace_request,
                             model_settings_home=work_home)
```

The existing desktop `_build_coding_surface` supplies this capability. The only
host changes are the initial mode and the explicit model-settings path. Work/Coding
panel construction, navigation, layout and visibility semantics remain unchanged.

`model_settings_home` is a trusted Python-host capability, not an Agent tool or an
EditorBridge JSONL parameter. If omitted, existing independent-runtime behavior
continues. Standalone Development launch explicitly supplies `~/.forge` as the
model source, while its own home continues to store execution state.

## Model settings projection

`ModelSettingsSource(home)` implements the trusted EditorBridge factory:

```python
router = factory(isolated_config, isolated_home)
revision = factory.signature(isolated_home)
```

Only enabled `provider:*` rows and `model:router` cross this seam. Legacy desktop
provider rows with an omitted `name` and a `baseURL` are supported. An explicit
Coding model configuration takes precedence over the host source. Without one,
the source uses the desktop's saved model rows; packaged example providers are
not presented as if they were configured desktop models.

Provider expressions are resolved with a private environment mapping. Only the
model rows' referenced credential names are loaded from the appropriate store;
connector credentials are excluded. No process environment is changed. Provider
continues to bind each credential to its endpoint. No key or Work config is copied
into Coding files, prompts, results, diagnostics or session records. The original
VS Code bridge path retains its existing credential/onboarding behavior.

The factory returns a router, never a replacement execution Config. Coding's
Policy, sandbox, exposed tools, limits and execution stores still come from its
isolated initialization contract. A Work `bypassPermissions` setting cannot enter
Coding through model inheritance. The default project grant remains read-only.

## Readiness and recovery

`DevelopmentRuntime.readiness(explicit_model=None)` returns public metadata:

```json
{"ready": false, "code": "credential_missing", "message": "所选模型未配置 API 密钥…"}
```

Codes are `project_required`, `initialization_failed`, `model_missing`,
`model_unavailable`, `endpoint_missing`, `credential_missing`, and `ready`.
This checks local configuration and credential presence; `ready` is not a claim
that a provider's network, account balance or authentication has been tested.
Borrowed custom routers can retain their own transport contract.

Initialization commits `ready=True` only after Agent, checkpoint and remembered
state setup succeeds. On failure, an owned partial bridge is closed and replaced;
the Runtime remains retryable and retains a redacted diagnostic. A failed project
switch keeps the previously loaded project/runtime.

Before a new idle submission, `refresh_models()` checks source file revisions. It
acquires the existing Runtime lane and builds the complete replacement router and
catalog before swapping them. Failed or changing settings leave the prior route
intact, reject that send and preserve its draft. Repairing and saving settings
allows the next Send to retry without discarding history, session or checkpoints.
A removed named selection is rejected rather than silently changed to another
model. Running and already queued tasks retain their accepted configuration.

The revision uses file mtime/size and detects changes made by the existing atomic
settings save operations. It is not a cross-process lock or a cryptographic file
identity guarantee. An external editor deliberately preserving both metadata
values may require reopening the project/application to reload settings.

## First Send

1. A nonempty draft with no project opens the existing folder chooser.
2. Cancel leaves the draft untouched and creates no task. Identical repeated
   refusal notices are not appended repeatedly.
3. Choosing a project asynchronously initializes the existing Runtime. Send does
   not clear the draft while loading; failures show the actual redacted reason.
4. After successful loading, the original unchanged draft is submitted once.
   Editing it or pressing Cancel prevents automatic submission. The pending
   intent is also checked in the idle callback after loading completes.
5. After an initialization failure, Send retries the same selected project.
   Users can also explicitly open another project. Configuration preflight errors
   retain the draft and never fabricate an executed Agent task.

There is no automatic network retry of completed/unknown actions. Normal execution,
task identity, cooperative cancellation, FIFO admission, file protection and event
results continue through the existing Runtime/Bridge/Agent/ActionExecutor path.
The UI worker also reports BaseException interruptions back through its result
queue, so a core KeyboardInterrupt/SystemExit does not leave the send UI permanently
busy. This clears host admission state; it does not replay or complete the interrupted run.
