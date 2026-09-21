# verify/ — Independent Verifier

This isn't a supplement to the author's self-checks — it's verification that is **independent of the author**. PLAN §5 lists "verification-criteria drift" as one of the three most likely places to go wrong: self-check items are written by the author, and the author can fool themselves (a previous round had a hidden test case written incorrectly, which scored a 14/14 model as 11/14). The sole purpose of this directory is to take the job of "proving it actually happened" out of the author's hands.

## The three questions it answers

1. **Are the numbers right?** — Whether the hardcoded self-check item counts, module counts, and assertion counts in README / PLAN match a live measurement taken right now.
2. **Is the evidence real?** — Of all assertions, how many are lines actually executed with verifiable content, and how many are pass-lines the gate synthesized itself.
3. **Do the seams connect?** — Each module passing its own self-checks doesn't mean their data contracts line up. This verifier is built specifically to probe the seams.

## Usage

```bash
cd agent-forge
python verify/independent_audit.py          # human-readable output
python verify/independent_audit.py --json   # machine-readable output
```

Exit codes: `0` all pass; `1` there are FAILs; `2` only WARNs.

## What it does (six groups)

| Group | Content |
| --- | --- |
| 1 | Claims vs. measurements: run `selftest` and `modules validate`, and reconcile the numbers |
| 2 | Evidence quality: scan the assertion lines of every healthy module, flag synthesized lines that "default to pass when there's no output", and compute effective density after subtracting vacuous lines |
| 3 | Gate adversarial testing: in a temporary copy, replace a module's self-check with an empty implementation → see whether the gate still lets it through; then break a piece of real behavior as a **positive control** to confirm the gate does block |
| 4 | Seams: feed the tool names produced by `mcp_bridge.discover` into `toolhost.register_tools` and see whether they're accepted |
| 5 | Permissions: does `toolhost.authorize` decide based on the tool name or the module's declaration; does the `read_only` value in the normalized output have a channel to get through |
| 6 | Hook reachability and capability-tag overlap |

Group 3 is the heart of this verifier: **it doesn't read the author's assertions, it manufactures its own counterexamples.** The positive control (deliberately break the implementation → the gate should block) and the negative control (swap in an empty test → the gate should not block) must yield opposite conclusions; otherwise the gate has no ability to judge "whether the test itself is valid."

## Discipline

- **Only reads the code under verification**: it never writes any file under `forge/`; group 3's modifications happen inside a `tempfile` copy.
- **Never cites the author's self-check conclusions**: the evidence for groups 3, 4, and 5 is constructed entirely by this file itself.
- **Reports measurements as they are**: probe failures, missing environments, and unknown conclusions are all recorded as WARN with the reason written out — never silently skipped.
