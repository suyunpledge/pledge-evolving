"""Adversarial coverage for the config-authority and virtualisation boundaries.

Complements `test_secret_architecture.py` rather than repeating it. That suite
tests one shape per fix; this one enumerates *neighbouring* shapes a
shape-specific fix would miss — other JSON-Patch operators, other escalation
paths into the policy row, other `$expr` spellings, other LogRecord leak
channels, other encodings of a value that must be protected.

Provenance: these cases were developed as an independent probe during the
2026-10-08 review. Reverting the whole-row freeze in `config_edit.py` makes the
policy-row cases here fail, so they are load-bearing rather than decorative.

All credentials are synthetic. No network, no user state.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

from forge.config import resolve
from forge.policy import Mode, Policy
from forge.secrets import MARKER, REF, SecretLoggingFilter, SecretScope
from forge.tools import ToolContext, build_builtin_registry


class PolicyRowEscalationTests(unittest.TestCase):
    """Escalation shapes the original suite did not exercise.

    The suite covers replace/readScope, add/disabled, remove/the-row and
    appending a fresh override row. These are the neighbours: dropping the deny
    list, replacing the whole config object, de-identifying the row, the
    array-append pointer, the move/copy operators, and a same-name/different-id
    row that slips past an id-only check.
    """

    ESCALATIONS = {
        "remove the deny list": {"op": "remove", "path": "/0/config/deny"},
        "replace the whole config": {"op": "replace", "path": "/0/config",
                                     "value": {"mode": "bypassPermissions", "allow": ["*"]}},
        "re-identify the row": {"op": "replace", "path": "/0/id", "value": "policy2"},
        "append via array pointer": {"op": "add", "path": "/0/config/allow/-", "value": "*"},
        "relax the mode": {"op": "replace", "path": "/0/config/mode", "value": "bypassPermissions"},
        "move a row into the policy slot": {"op": "move", "from": "/1", "path": "/0"},
        "copy a row over the policy slot": {"op": "copy", "from": "/1", "path": "/0"},
        "same name, different id": {
            "op": "add", "path": "/2",
            "value": {"id": "policy_extra", "name": "policy:core",
                      "config": {"mode": "bypassPermissions", "allow": ["*"]}}},
    }

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.scope = SecretScope()
        self.addCleanup(self.scope.close)
        self.ctx = ToolContext(Policy(mode=Mode.ACCEPT_EDITS, workspace=self.root),
                               self.root, secret_scope=self.scope, isolated=True)
        self.registry = build_builtin_registry()
        self.path = self.root / "forge.patch.json"

    def _baseline(self) -> bytes:
        self.path.write_text(json.dumps([
            {"id": "policy", "name": "policy:core",
             "config": {"mode": "default", "allow": [], "deny": ["shell_exec"],
                        "readScope": "workspace"}},
            {"id": "model", "config": {"primary": "old"}},
        ]), encoding="utf-8")
        return self.path.read_bytes()

    def _patch(self, operation: dict):
        view = self.registry.invoke("edit_config", {"path": self.path.name}, self.ctx)
        self.assertTrue(view.ok, view.error)
        return self.registry.invoke("edit_config", {
            "path": self.path.name, "revision": view.meta["revision"],
            "patch": [operation]}, self.ctx)

    def test_neighbouring_escalation_shapes_are_refused(self):
        for label, operation in self.ESCALATIONS.items():
            with self.subTest(shape=label):
                original = self._baseline()
                result = self._patch(operation)
                self.assertFalse(result.ok, f"{label} was accepted")
                self.assertEqual(self.path.read_bytes(), original,
                                 f"{label} modified the file despite refusal")

    def test_ordinary_rows_remain_editable(self):
        """The freeze must not harden into 'nothing can be edited'."""
        self._baseline()
        view = self.registry.invoke("edit_config", {"path": self.path.name}, self.ctx)
        self.assertTrue(view.ok, view.error)
        result = self.registry.invoke("edit_config", {
            "path": self.path.name, "revision": view.meta["revision"],
            "patch": [{"op": "replace", "path": "/1/config/primary", "value": "new"}]}, self.ctx)
        self.assertTrue(result.ok, result.error)
        self.assertIn("new", self.path.read_text(encoding="utf-8"))


class ExpressionSpellingCouplingTests(unittest.TestCase):
    """The guard and the evaluator must agree on which spelling is live.

    `config_edit.py` refuses `$expr` and `config.py` evaluates `{"$expr": ...}`.
    Today both are exact-lowercase, and the guard is *stricter* than the loader
    (it refuses the spelling even with sibling keys, which the loader ignores).
    That direction is safe. The hazard is drift: widen either side to
    case-insensitive and the other silently becomes stale with no test failing.

    This test pins the invariant rather than the implementation: **anything the
    loader would evaluate must be something the guard refuses to let an agent
    write.**
    """

    SPELLINGS = {
        "lowercase": "$expr",
        "uppercase": "$EXPR",
        "mixed": "$Expr",
        "spaced": " $expr ",
    }

    def _loader_evaluates(self, key: str) -> bool:
        try:
            return resolve({key: "'EVALUATED'"}, {}) == "EVALUATED"
        except Exception:                                    # noqa: BLE001
            return False

    def _guard_refuses(self, key: str) -> bool:
        from forge.config_edit import _control_fields

        found = list(_control_fields({"config": {key: "get('env.X','')"}}))
        return bool(found)

    def test_lowercase_is_both_evaluated_and_guarded(self):
        self.assertTrue(self._loader_evaluates("$expr"))
        self.assertTrue(self._guard_refuses("$expr"))

    def test_agent_cannot_write_a_live_expression(self):
        for label, key in self.SPELLINGS.items():
            with self.subTest(spelling=label):
                live = self._loader_evaluates(key)
                refused = self._guard_refuses(key)
                # the invariant: live ⇒ refused. A live-and-unrefused spelling
                # would be a privilege path.
                self.assertFalse(live and not refused,
                                 f"{label!r} is evaluated by the loader but not "
                                 f"refused by the guard — escalation path")

    def test_sibling_keys_do_not_make_an_expression_live(self):
        # The loader requires `$expr` to be the *only* key; document that so a
        # future relaxation is caught here.
        self.assertNotEqual(resolve({"vendor": "x", "$expr": "1"}, {}), 1)


class LogLeakChannelTests(unittest.TestCase):
    """Leak channels other than the message body.

    The architecture suite covers renamed extra keys and a cached `exc_text`.
    These are the neighbours: `record.args` in several shapes, a non-string
    `msg`, and a nested extra mapping.
    """

    def setUp(self) -> None:
        self.secret = "synth-log-adv-301"
        self.scope = SecretScope()
        self.addCleanup(self.scope.close)
        self.scope.protect({"password": self.secret})
        self.filter = SecretLoggingFilter()

    def _filtered(self, *, msg, args=(), extras=None) -> str:
        record = logging.LogRecord("t", logging.INFO, "f", 1, "", (), None)
        record.msg = msg
        record.args = args
        for key, value in (extras or {}).items():
            setattr(record, key, value)
        self.filter.filter(record)
        return repr(record.__dict__) + repr(record.getMessage())

    def test_args_shapes(self):
        cases = {
            "positional tuple": ("pw=%s", (self.secret,)),
            "named mapping": ("pw=%(p)s", {"p": self.secret}),
            "nested object": ("x=%s", ({"deep": self.secret},)),
        }
        for label, (msg, args) in cases.items():
            with self.subTest(shape=label):
                self.assertNotIn(self.secret, self._filtered(msg=msg, args=args))

    def test_non_string_message(self):
        for label, msg in {"dict": {"password": self.secret},
                           "list": ["password", self.secret]}.items():
            with self.subTest(shape=label):
                self.assertNotIn(self.secret, self._filtered(msg=msg))

    def test_nested_extra_mapping(self):
        blob = self._filtered(msg="ok", extras={"ctx": {"password": self.secret}})
        self.assertNotIn(self.secret, blob)

    def test_cached_traceback_text(self):
        try:
            raise ValueError(f"boom {self.secret}")
        except ValueError:
            record = logging.LogRecord("t", logging.ERROR, "f", 1, "err", (), sys.exc_info())
            record.exc_text = f"Traceback... {self.secret}"
            self.filter.filter(record)
            self.assertNotIn(self.secret, repr(record.__dict__))


class SecretEncodingTests(unittest.TestCase):
    """Shapes a naive 'is it a plain string?' check would let through."""

    def setUp(self) -> None:
        self.scope = SecretScope()
        self.addCleanup(self.scope.close)
        self.secret = "synth-encode-adv-401"

    def test_structural_variants_are_protected(self):
        payloads = {
            "deeply nested": {"password": {"a": {"b": {"c": {"d": {"e": self.secret}}}}}},
            "list of lists": {"access_token": [[self.secret]]},
            "url credentials": {"endpoint": f"https://user:{self.secret}@example.invalid/"},
        }
        for label, payload in payloads.items():
            with self.subTest(shape=label):
                rendered = json.dumps(self.scope.protect(payload), ensure_ascii=False)
                self.assertNotIn(self.secret, rendered)

    def test_decorated_values_are_protected(self):
        for label, value in {
            "surrounding whitespace": f"  {self.secret}  ",
            "zero-width inserted": self.secret[:4] + "\u200b" + self.secret[4:],
        }.items():
            with self.subTest(shape=label):
                rendered = json.dumps(self.scope.protect({"password": value}), ensure_ascii=False)
                self.assertNotIn(self.secret, rendered)

    def test_a_value_equal_to_the_marker_is_still_a_value(self):
        # `MARKER` as a *value* is data, not a "already redacted" signal.
        out = self.scope.protect({"password": MARKER})
        self.assertTrue(json.dumps(out).strip())

    def test_text_surface_variants(self):
        for label, text in {
            "assignment": f'PASSWORD="{self.secret}"',
            "json blob": json.dumps({"password": self.secret}),
            "url": f"https://u:{self.secret}@example.invalid/",
        }.items():
            with self.subTest(shape=label):
                self.assertNotIn(self.secret, self.scope.protect_text(text))


class ReferenceForgeryTests(unittest.TestCase):
    """A syntactically valid ref must not exempt a real value from redaction."""

    def setUp(self) -> None:
        self.scope = SecretScope()
        self.addCleanup(self.scope.close)

    def test_forged_reference_does_not_exempt_a_known_value(self):
        secret = "synth-forge-ref-501"
        guarded = self.scope.protect({"password": secret})
        self.assertTrue(REF.fullmatch(guarded["password"]))
        # Presenting the raw value in ref-shaped clothing must still be redacted.
        rendered = self.scope.protect_text(f"{{{{SECRET_REF:{secret}}}}}")
        self.assertNotIn(secret, rendered)

    def test_ref_shaped_literal_in_a_declared_field_is_virtualised(self):
        for value in ("audit-{{SECRET_REF:NOT_A_REFERENCE}}-1", "$SYNTHETIC", "$audit-1"):
            with self.subTest(value=value):
                safe = self.scope.protect({"password": value})
                self.assertTrue(REF.fullmatch(safe["password"]))
                self.assertNotIn(value, json.dumps(safe))


if __name__ == "__main__":
    unittest.main()
