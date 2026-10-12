"""Probe: characterise the process-global secret-redaction registry.

Two questions:
  1. After tests/test_development_loop.py runs, what has entered
     ``forge.secrets._KNOWN`` that makes ``redact(['reviewer','old'])``
     stop being a no-op?
  2. Is the match a whole-word match or a raw substring match? That decides
     whether this is a narrow test artifact or a real false-positive risk for
     legitimate model identifiers.
"""

from __future__ import annotations

from _bootstrap import check, summary  # noqa: E402

import forge.secrets as sec  # noqa: E402


def snapshot_known():
    return set(sec._KNOWN)


def main() -> int:
    before = snapshot_known()
    print(f"_KNOWN size BEFORE dev-loop run: {len(before)}")
    print("redact('reviewer') before:", sec.redact("reviewer"))
    print("redact('old')      before:", sec.redact("old"))

    import pytest

    print("\n-- running tests/test_development_loop.py in-process --")
    pytest.main(["../tests/test_development_loop.py", "-q", "--no-header",
                 "-p", "no:cacheprovider"])
    print("-- done --\n")

    after = snapshot_known()
    added = after - before
    print(f"_KNOWN size AFTER: {len(after)}  (added {len(added)})")
    print("redact('reviewer') after :", sec.redact("reviewer"))
    print("redact('old')      after :", sec.redact("old"))

    # Which added value is responsible for each false redaction?
    for probe in ("reviewer", "old"):
        out = sec.redact(probe)
        if out == probe:
            print(f"  no false redaction for {probe!r}")
            continue
        culprits = [v for v in added if v and v in probe]
        substring_culprits = [v for v in added if v and v in probe and len(v) >= 8]
        print(f"  FALSE REDACTION {probe!r} -> {out!r}")
        print(f"    exact/substring members of _KNOWN: {culprits[:5]}")
        print(f"    of those len>=8 (no word-boundary guard): {substring_culprits[:5]}")

    def _global_registry_grows():
        return len(added) > 0, f"{len(added)} values added to process-global _KNOWN"

    check("dev-loop run mutates the process-global secret registry", _global_registry_grows)

    def _false_positive_possible():
        bad = [p for p in ("reviewer", "old") if sec.redact(p) != p]
        return not bad, f"redact() now alters plain words: {bad}"

    check("redact() leaves plain non-secret words untouched afterwards",
          _false_positive_possible)

    return summary("redaction global-state probe")


if __name__ == "__main__":
    raise SystemExit(main())
