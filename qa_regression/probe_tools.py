"""Probe: Tool Registry invariants (registration, conflicts, lazy activation).

Verifies claims about forge.tools.ToolRegistry directly against the source:
  - duplicate registration behaviour
  - presence/absence of an unregister path
  - name validation
  - lazy (deferred) tool gating
Each check states what it observed rather than asserting a preferred outcome
outright, so the report can distinguish design choices from defects.
"""

from __future__ import annotations

from _bootstrap import check, summary  # noqa: E402

from forge.tools import ToolRegistry, ToolSpec, build_builtin_registry  # noqa: E402


def _spec(name, desc, handler=None):
    return ToolSpec(
        name=name,
        description=desc,
        schema={"value": "string"},
        handler=handler or (lambda args, ctx: f"{name}:{args.get('value')}"),
    )


def main() -> int:
    r = ToolRegistry()

    def _register_two():
        r.register(_spec("dup", "first"))
        r.register(_spec("dup", "SECOND"))
        kept = r._specs["dup"].description
        return True, f"kept description={kept!r}; total specs={len(r._specs)}"

    check("duplicate registration observed (reports what survives)", _register_two)

    def _no_unregister():
        has = hasattr(r, "unregister")
        return True, f"has unregister(): {has}"

    check("registry unregister capability (observed)", _no_unregister)

    def _name_validation():
        # Does register() reject a syntactically odd name?
        try:
            r.register(_spec("bad name with spaces", "x"))
            return True, "register() accepted a name with spaces (no name validation)"
        except Exception as exc:  # noqa: BLE001
            return True, f"register() rejected odd name: {type(exc).__name__}: {exc}"

    check("name validation on register (observed)", _name_validation)

    def _clone_isolates_activation():
        base = build_builtin_registry()
        names = sorted(base._specs)[:3]
        for n in names:
            base.deferred(n) if hasattr(base, "deferred") else None
        child = base.clone()
        before_parent = set(getattr(base, "_activated", set()))
        for n in names:
            try:
                child.search(n)
            except Exception:  # noqa: BLE001
                pass
        after_child = set(getattr(child, "_activated", set()))
        after_parent = set(getattr(base, "_activated", set()))
        leaked = after_parent - before_parent
        return not leaked, (
            f"child activated={len(after_child)}; parent activation leaked={sorted(leaked)[:3]}"
        )

    check("child clone activation does not leak to parent", _clone_isolates_activation)

    def _invoke_rejects_unknown():
        # Contract check: the registry reports failure as a ToolResult value,
        # it does not raise. Assert the value contract, not an exception.
        #
        # invoke() post-processes the result through ctx.secret_scope, so the
        # stub must provide one -- a bare object() makes the harness, not the
        # product, produce the error.
        class _Scope:
            def protect_text(self, text):
                return text

        class _Ctx:
            secret_scope = _Scope()

        base = build_builtin_registry()
        res = base.invoke("definitely-not-a-tool", {}, _Ctx())
        rejected = (res.ok is False) and ("unknown tool" in (res.error or ""))
        return rejected, f"ok={res.ok!r} error={res.error!r}"

    check("invoke() reports unknown tool as failed ToolResult", _invoke_rejects_unknown)

    return summary("tool registry probe")


if __name__ == "__main__":
    raise SystemExit(main())
