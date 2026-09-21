# forge — A Unified Agent Framework

Distills the most worth-copying designs from seven different agent frameworks — **Codex / Hermes Agent / DeepSeek Harness / Claude Code / WorkBuddy (CodeBuddy) / OpenClaw / OpenCode** — into a single, runnable minimal kernel.

This isn't a concept diagram, it's working code: `forge selftest` runs a full offline check suite (no network, no API keys; item counts follow the command's actual output) covering config synthesis, permission adjudication, lazy tool loading, capability trust, dual-write memory, session replay, shadow snapshots, model fallback, protocol gateway and protocol translation, native tool calling, self-evolution, heterogeneous federation, contrib-module consistency gating and cross-module integration, cost accounting, and subagent orchestration — plus a static security baseline (table-aligned invariants and four-way coverage for the no-network / no-subprocess / no-destructive-file-API bans).

Zero third-party dependencies (pure standard library), Python ≥ 3.10.

## v2 additions (0.2.0)

| Module | Borrowed from | What it does |
| --- | --- | --- |
| `evolution.py` | **Hermes** (curator / learning / journey / checkpoints) | Signal extraction → candidates → safety gate → ledger → rollback → metabolism. Long-term goals always need a human nod |
| `federation.py` | Built from scratch this round (all failure modes grew out of real testing) | Descriptors / dispatch / failure classification / output normalization / cost guardrails for heterogeneous CLI workers |
| `wire.py` | The protocol mismatch between Claude Code and MiMo | Bidirectional Anthropic ↔ OpenAI translation, including SSE streaming and incremental tool calls |

New commands:

```bash
python run.py evolution stats                # Candidate pool + ledger overview (defaults to ~/.forge; use --home . to see the repo's own bundled data)
python run.py evolution observe "From now on, write reports in Chinese" --nominate
python run.py evolution approve <id> && python run.py evolution apply <id>
python run.py evolution rollback <id>        # Reversible
python run.py evolution curate               # Archives expired candidates (never deletes)
python run.py federation roster              # Capability/cost/permission profile for the worker queue (read from the home's federation.json)
python run.py gateway --upstream https://api.example.com/v1 --upstream-wire openai \
    --model-map claude-sonnet-5=<upstream-model> --models claude-sonnet-5 --key $KEY
```

### Four hard rules of self-evolution

1. **Nothing lands without approval**: only low-risk `note`-type candidates can take effect automatically; everything else goes into pending.
2. **Long-term goals always need a human nod**: `MEMORY.md` / `AGENTS.md` / `SOUL.md` / `USER.md` / `TOOLS.md` / `SKILL.md` have no config option to bypass this.
3. **Every write carries provenance**: the target file gets an appended candidate id + timestamp + evidence reference, so it can be traced after the fact.
4. **Archive, never delete**: reject / quarantine / stale are all `restore`-able; another snapshot is taken right before any rollback.

## v3: each agent writes its own module (0.3.0)

The new framework isn't just existing agents strung together — instead, **the interfaces are frozen first, then each agent writes its own module, and the code is merged in against the contract**.

- Contract: [`CONTRACT.md`](CONTRACT.md) — the frozen module API, hard bans, and a ready-to-paste task brief for each agent
- Gate: `forge/registry.py` — statically scans source code (bans network/subprocess/destructive file APIs/`eval`) + dynamic loading + runs the contributed module's own self-checks (≥ 8 assertions)
- Reference example: [`forge/contrib/heartbeat.py`](forge/contrib/heartbeat.py) — the first contrib module admitted, passing all 10 of its own self-checks

```bash
python run.py modules validate    # Consistency snapshot: who's in, who's isolated, and why
python run.py modules selftests   # Runs the self-checks bundled with every admitted module
```

Isolation, not half-mounting: if a contrib module's import throws, or its self-checks fail, or it references a banned library, it gets flagged with a reason and blocked — without affecting the core framework.

## v4: four steps toward a complete architecture (0.4.0)

1. **Native tool calling** (`toolwire.py`) — the main loop moves from "text-protocol only" to **native-first, text-fallback**: tool declarations are generated per-wire, calls are parsed from native fields, and tool results flow back over the same wire **with the call id preserved**. The id matters: in the next request round, the assistant message and the tool message must line up — reconstructing that from prose is just guessing.
2. **Cross-module integration tests** (`selftest:test_contrib_integration`) — the gate only proves each module is valid **in isolation**; this step proves the **seams**: the worker picked by the router must be the same one dispatched to by the team layer, the scheduler's due set must be fed to that same dispatch round, and the compactor must agree with the main loop on what counts as "too big."
3. **Normalizing hook-name drift** (`registry.HOOK_ALIASES`) — three authors, three naming conventions (`due_jobs` / `on_due_check`, `replay` / `on_replay`). Rather than requiring everyone to rename their own code, an alias table resolves to the canonical name, and any drift is recorded as a **warning** in the index.
4. **Cost accounting** (`pricing.py` + `forge cost`) — turns effective unit prices, reverse-derived from two real invoices, into hard numbers, so "who should this task be routed to" can be decided by the numbers.

```bash
python run.py cost rate --models deepseek-flash,mimo-v2.5 --tokens 1273002379
python run.py modules validate        # now includes naming-drift warnings
python run.py selftest                # item counts follow actual output
```

## v4.5: the fix rounds (2026-09-16, signed off round-by-round with an adversarial review seat)

After v4 shipped, it went into fix rounds: each round had its own independent review seat write its own attack probes, and only passed once signed off. Four batches, all cleared; g3's review tally is back to zero:

1. **Static security baseline (H1–H10 closed)**: the contrib gate expanded from "scan imports" to a doubly-closed layer — alias-aware + reference-triggers-block + dunder attribute table + **table-aligned invariants** (CALLS and FROM_NAMES are twin-pinned bidirectionally; an unbalanced table expansion turns the check red on commit). Six root-spelling variants, `operator.attrgetter` laundering, and every `os.*` file-mutation primitive were closed one by one; the number of `gate:*` assertions follows the actual self-test output, and the independent probes are all green.
2. **Tool-host declarations as semantics (F4-2/F4-3)**: `readOnlyHint` is normalized and passed through, `authorize` honors `spec["read_only"]`, and the **declaration takes precedence over name heuristics** — after stripping the `server__` prefix, the heuristic no longer flips; a write tool named `read_*` can no longer sneak past the check.
3. **Baseline semantics for contrib mounting (F2-2/H11)**: `mount_contrib_extensions` uses a dual registry — bundled contrib modules get a baseline seat, while a `home/contrib` module with the same name **only takes over if it passes the gate itself and its hooks resolve**. A rejected file no longer makes the seat silently disappear; takeover/fallback is logged as a warning and a `mount_warning` event fires on the first `run()` (fired only once, not repeatedly).
4. **Router/teams primary keys frozen (F2-3)**: workers/members are keyed by `id` (a single keyspace), with `name` used only for display and sorting; the integration self-test now constructs counter-examples (id ≠ name), so any bug masked by coincidentally-equal values now turns red.
5. **`validate` warning lines (F5-4) + regex cleanup (F5-5)**: `modules validate` now prints `[warn]` lines inline; leftover generation-time regex artifacts in the evolution signal matching have been cleaned up.

## v0.7.0: three-tier smart model routing

Added `forge/routing.py` (SmartRouter, a ModelRouter subclass, with zero changes to the frozen contract). Task-level tier selection, with three strategies:

| Strategy | Routing logic |
| --- | --- |
| `economy` | Walks tiers in ascending order of effective unit price (from `pricing.py`'s invoice-derived rates); only escalates on retriable failures; unknown-priced tiers are treated conservatively and placed last, so they can't be mistaken for a free tier |
| `balanced` | Starts on the mid-tier workhorse; escalates on failure + falls back to the frozen chain as a floor (default) |
| `premium` | Two-stage pipeline: mid-tier drafts → top-tier integrates/adjudicates; any failure in the integration stage (including fatal ones) falls back to the draft — the draft has already been paid for, so it isn't discarded; messages strictly alternate roles (system/user/assistant/user), and the integration tier only receives the original task + the draft, never the full conversation |

```bash
python run.py run "task" --strategy economy      # Explicit CLI override (CLI > config > default balanced)
```

Configured under the `model.routing` block in `bundles/base.json`: `tiers` (the tier table, declared in cost order), `premium` (the integration/adjudication tier), `small` (the miscellaneous-tasks tier — failures here don't escalate, so they can't burn through the review tier). The single authoritative definition of "unknown price = 0.0" in the pricing table is documented in a comment at the top of `pricing.py` — routing interprets it conservatively while budget scaling interprets it generously, and this difference is intentional.

## Quick Start

```bash
cd agent-forge

python run.py selftest            # Offline self-checks, item count follows actual output
python run.py dump-config         # View the synthesized config tree
python run.py doctor              # Health / drift / key checks
python run.py capabilities list   # Capability listing
python run.py run "Read the README and summarize it"
python run.py gateway --upstream https://api.deepseek.com --port 8799 --models claude-sonnet-5
```

`run.py` is the required entry point: AutoClaw's embedded Python uses a `._pth` layout, so `python -m forge.cli` fails with `No module named 'forge'` (the current directory isn't added to `sys.path`, and `PYTHONPATH` has no effect either). With a standard Python install elsewhere, `python -m forge.cli` works fine.

Zero third-party dependencies (pure standard library), Python ≥ 3.10.

## Local inference services (ollama / llamacpp / mnn)

A few adaptations that make a *self-hosted* engine usable are wrong for a cloud provider, so they are gated behind an optional `service` field on the provider row. Absent or unrecognised means "not local" and nothing below applies — a typo can never reroute a provider.

```json
{"id": "local", "name": "provider:local",
 "config": {"service": "ollama", "wire": "openai",
            "baseURL": "http://127.0.0.1:11434", "model": "qwen3:8b"}}
```

What the gate changes, and why (both measured against Ollama 0.34.0 serving qwen3:8b):

| Gate | Local engine | Everything else |
| --- | --- | --- |
| Chat / tool-loop path | `/v1/chat/completions` (a bare `host:port` base URL gains the `/v1` segment) | `/chat/completions`, unchanged |
| Thinking mode | `off` unless explicitly set to `on`; `smart` is downgraded | the configured mode, untouched |
| Thinking output budget | always bounded (ceiling `LOCAL_THINKING_CAP`, overridable downward via `thinking.tokenCap`) | never touched |

Reasons, in order: the native chat surface rejects a replayed `tool_calls` history with HTTP 400, so a multi-turn tool loop has to use the OpenAI-compatible path; and a small local model's contemplation can fail to converge (measured: 420 s with no answer), which a bounded output budget cuts off cheaply. Cloud providers keep their existing base path and behaviour.

## Seven frameworks → one landing spot

| Source framework | Design borrowed | Landing module | Self-test anchor |
| --- | --- | --- | --- |
| **DeepSeek Harness** | Empty root + ordered patch layers, last-write-wins by id, whole-line replacement instead of deep merge, `dump-default` recovery channel, `$expr` lazy expressions | `config.py` | `config:*` |
| **WorkBuddy / CodeBuddy** | Two-dimensional permissions (mode baseline + allow/ask/deny exceptions, deny always wins), subagent permission ceiling, Defer/NoDefer lazy loading, command-level blacklist, typed dual-write memory, trace metering | `policy.py` `tools.py` `memory.py` | `policy:*` `tools:*` `memory:*` |
| **Codex** | Three-tier sandbox tied to approvals, rollout JSONL event stream (`session_meta` + ordinal), resume/fork, versioned & rebuildable derived index, point-path overrides + strict mode | `policy.py` `session.py` | `session:*` |
| **Hermes Agent** | Fallback chains triggered by error type, MoA multi-slot aggregation, shadow-git checkpoints and rollback, curator that only archives (never deletes), skill provenance | `model.py` `checkpoint.py` `memory.py` | `model:*` (3) `checkpoint:*` (3) |
| **OpenClaw** | Layered system-prompt assembly, context-compaction thresholds and compaction events, bounded-slice memory injection, subagent depth/budget/output detoxification, serialized sessions | `loop.py` `memory.py` | `loop:*` |
| **OpenCode** | Provider-as-data (`provider[] + model + small_model`), unified wire adapter layer, keys never live in config files | `model.py` `cli.py` | `model:*` `doctor:*` |
| **Claude Code** | CLI output shapes (`-p` non-interactive / structured output), permission-mode enum, layered settings with overrides | `cli.py` `policy.py` | `cli:*` |

Design rationale, alternatives that were rejected, and the reasoning behind each trade-off are in [`DESIGN.md`](DESIGN.md). The seat-anchor counts for each source framework (e.g. `config:*`) grow with every fix round; they follow the actual `selftest` output and are intentionally not hardcoded here.

## What the kernel looks like

```
Task
 └─ Agent.run()                     loop.py
      ├─ System prompt assembly  ← capability index + memory slice + permission state (all bounded)
      ├─ Context compaction      ← folds the middle section once past the threshold, keeps the tail, logs a compaction event
      ├─ ModelRouter              ← primary model → fallback chain by error type → (optionally) MoA aggregation
      ├─ Tool calling             ← Defer hides by default → tool_search activates → permission adjudication → execution
      │    └─ Before write ops    → CheckpointStore shadow snapshot
      └─ spawn_subagent           ← independent context / depth ceiling / budget / permission ceiling / output detoxification
```

Every decision is logged into `sessions/*.jsonl`: `session_meta`, `user_message`, `tool_decision`, `tool_call`, `subagent_spawn`, `compaction`, `checkpoint`, `assistant_message`. The log is the fact; the index is a cache — rebuild it anytime with `sessions --rebuild`.

## Five non-negotiables

1. **Deny always wins**: no matter how permissive the mode or how bold the subagent, explicit deny rules and command blacklists take precedence over every other adjudication.
2. **Subagents cannot escalate privileges**: a subagent's mode is capped by its parent session's ceiling; a subagent spawned under a `plan`-mode parent can never obtain `bypassPermissions`.
3. **The user layer can be entirely stripped out**: `dump-default-config` skips the user layer, so a broken user config can never permanently lock out startup.
4. **Archive, never delete**: the curator only marks agent-built knowledge as archived; `restore` is always reversible.
5. **Keys never live in config files**: `doctor` flags an inline `apiKey` directly as a warning; providers go through environment variables or the loopback gateway.

## Directory Layout

```
agent-forge/
├── forge/
│   ├── config.py       Empty root + patch-layer synthesis (DSH)
│   ├── policy.py       Two-dimensional permissions + three-tier sandbox + command blacklist (CodeBuddy / Codex)
│   ├── tools.py         Tool registration + lazy loading + ToolSearch (CodeBuddy)
│   ├── capability.py    Unified contract for skills/plugins/connectors (Hermes / Codex)
│   ├── memory.py        Typed dual-write memory + curator (CodeBuddy / OpenClaw)
│   ├── compaction.py    Context-compaction strategy (split out from memory, independently testable)
│   ├── subagent.py      Subagent orchestration: spawning/budget/output sanitization (split out from loop)
│   ├── thinking.py      Deliberation engine: budget control / convergence detection / three-tier mode (split out from loop)
│   ├── session.py       Append-only event log + derived index + fork (Codex)
│   ├── model.py         Provider abstraction + fallback chain + MoA (OpenCode / Hermes)
│   ├── routing.py       Three-tier smart routing (economy / balanced / premium)
│   ├── local_service.py  Engine gate: ollama / llamacpp / mnn get local-only paths
│   ├── checkpoint.py    Shadow-git snapshots and rollback (Hermes)
│   ├── loop.py          Agent main loop (OpenClaw / CodeBuddy)
│   ├── gateway.py       Loopback protocol gateway (built and tested through this integration)
│   ├── wire.py          Anthropic ↔ OpenAI protocol translation
│   ├── toolwire.py      Native tool-calling protocol adapter
│   ├── federation.py    Heterogeneous CLI federation dispatch
│   ├── evolution.py     Self-evolution
│   ├── registry.py      Contrib-module registry + consistency gate
│   ├── pricing.py       Cost accounting
│   ├── guard.py         Shared security ban table
│   ├── smoke.py         End-to-end smoke tests
│   ├── selftest.py      Offline verification suite
│   ├── cli.py            Command-line entry point
│   └── test_new_modules.py  Standalone tests for the new modules
└── bundles/
    └── base.json        Baseline config layer (provider / policy / loop / model)
```

## Known Limitations

- `gateway` and `model.HttpTransport` use standard-library HTTP with no connection pooling or retry/backoff policy; a production deployment would need to swap in a real transport.
- The principle behind permission adjudication is "the sandbox intercepts **write** paths + the blacklist intercepts programs," not kernel-level isolation (on Windows, true shell confinement would require restricted tokens / ACLs). **Reads are not sandboxed**: `read_file` / `list_dir` / `grep` can read paths outside the workspace (relative, absolute, or `..` — all resolved as-is). Convergence on the read side relies on declaration (the `read_only` annotation during toolhost normalization and `authorize`'s decision) and runtime allowlisting, not a path sandbox; scenarios needing read isolation should declare read tools as read-only and constrain them at the `deny`/`ask` layer.
- **`allow` rules always outrank the mode baseline** (except deny): in `read-only` mode, an explicit `allow` rule for a write-class tool will let it through — mode is a baseline, not a hard ceiling; this is intentional semantics, not a defect. Scenarios that need read-only to be a hard isolation guarantee should use sandbox tiers and `deny` rules instead.
- `capability.install` only does versioned directory writes with no signature verification; supply-chain auditing would need to hook into something like OSV externally.
- Memory retrieval is exact match + recency/pinned ordering, with no vector recall — hooking up a vector store is an obvious next step.
- `read_only` is declarative, not enforced — a write tool marked `read_only=True` will bypass both the sandbox path check and the read-only-mode write gate (a WB-P2 authorize-declaration trust gap; currently limited in impact since the toolhost isn't mounted at runtime).
- The premium two-stage pipeline sends **the draft and the original task** to the integration/adjudication tier (informed use: configuring a premium tier implies consent to this data flow); the full conversation history is never sent out.
- `mount_contrib_extensions`'s dual registry covers the static gate and hook resolvability as a fallback; runtime hook exceptions are caught as a fallback inside `_use_extension` (it does not fall back to the bundled module) — scenarios needing "a bad hook never takes the seat" would require runtime-degraded retry instead (complex; logged as a known gap, not implemented).
- `teams.deliver`'s `to` direction only supports id-based addressing (no name→id resolution), while the `from` direction supports unique name-based resolution — this asymmetry is intentional and safe, but note: `to` cannot use a display name.
