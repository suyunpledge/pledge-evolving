# Contributing

Thanks for looking. This repository is early-stage and single-maintainer, so the fastest way to get a change in is to keep it small and verifiable.

## Before you open a PR

```bash
python run.py selftest     # must be 574/574 (no network, no API key)
python run.py doctor       # health / drift / key checks
```

If `selftest` fails on a clean checkout of `main`, that is a bug — open an issue with the failing check name and the output of `python run.py doctor`.

## Code rules

These are enforced, not advisory:

1. **Standard library only.** No new third-party runtime dependencies. (Optional extras behind a `[secrets]` marker already exist; don't extend them without discussion.)
2. **No network, no subprocess, no destructive filesystem calls, no `eval`** inside modules that are loaded through the contribution registry. `forge/registry.py` scans for these statically and quarantines violators.
3. **Every new module ships its own self-checks** (≥ 8 assertions) and they run as part of the module's admission.
4. **Deny-wins is invariant.** Any change that lets an explicit `deny` lose to a broader mode, or lets a subagent exceed its parent's permission ceiling, will be rejected regardless of how convenient it is.

## Contribution modules (the `forge/contrib/` path)

If you are an agent or a person writing a module against the frozen API:

1. Read [`CONTRACT.md`](CONTRACT.md) — it defines the module API and the hard prohibitions.
2. Start from [`forge/templates/contrib-shell.py`](forge/templates/contrib-shell.py).
3. Validate before sending:

```bash
python run.py modules validate     # who is admitted, who is quarantined, and why
python run.py modules selftests    # run every admitted module's own assertions
```

A module that fails validation still merges safely — it is quarantined with a recorded reason and cannot affect the framework. That is by design.

## Commit messages

`type(scope): summary` — `fix(coding)`, `feat(graph)`, `docs(readme)`. Explain **why** in the body when the change is not self-evident, especially for anything touching policy, secrets, or the contribution gate.

## Reporting security issues

Please do not open a public issue for a permission bypass or a secret-leak path. Email the maintainer address on the GitHub profile with reproduction steps; a fix and disclosure note will follow.

## What needs help most

- Vector recall for memory retrieval (currently exact match + recency ordering)
- A real transport with connection pooling and retry/backoff behind `model.HttpTransport`
- More vendor adapters with measured behaviour, not guesses
- Documentation of the contribution-gate pattern for use outside this repo
