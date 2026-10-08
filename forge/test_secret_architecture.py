"""Independent architecture regressions: real config rows and output boundaries.

All credentials are synthetic. No vendor requests or user state are accessed.
"""
import json
import io
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.policy import Mode, Policy
from forge.secrets import MARKER, REF, SecretLoggingFilter, SecretScope
from forge.tools import ToolContext, build_builtin_registry


class SecretArchitectureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.scope = SecretScope()
        self.addCleanup(self.scope.close)
        self.ctx = ToolContext(Policy(mode=Mode.ACCEPT_EDITS, workspace=self.root),
                               self.root, secret_scope=self.scope, isolated=True)
        self.registry = build_builtin_registry()

    def test_declared_secrets_cannot_opt_out_with_dollar_or_ref_substring(self):
        values = ('$audit-dollar-password-791', '$SYNTHETIC_PASSWORD',
                  'audit-literal-{{SECRET_REF:NOT_A_REFERENCE}}-791')
        for value in values:
            with self.subTest(value=value):
                safe = self.scope.protect({'password': value})
                self.assertTrue(REF.fullmatch(safe['password']))
                self.assertNotIn(value, json.dumps(safe))
        for value in ('$audit-text-password-792',
                      'audit-text-{{SECRET_REF:NOT_A_REFERENCE}}-792'):
            self.assertNotIn(value, self.scope.protect_text('PASSWORD="' + value + '"'))

    def test_declared_secret_containers_protect_scalar_leaves(self):
        data = {'access_token': ['audit-token-array-801', 'audit-token-array-802'],
                'password': {'value': 'audit-nested-password-803'},
                'cookies': ['audit-cookie-array-804']}
        safe = json.dumps(self.scope.protect(data))
        for value in ('audit-token-array-801', 'audit-token-array-802',
                      'audit-nested-password-803', 'audit-cookie-array-804'):
            self.assertNotIn(value, safe)

    def test_real_forge_policy_row_cannot_be_escalated(self):
        p = self.root / 'forge.patch.json'
        p.write_text(json.dumps([{'id': 'policy', 'name': 'policy:core',
                                'config': {'mode': 'default', 'allow': [], 'readScope': 'workspace'}},
                               {'id': 'model', 'config': {'primary': 'old'}}]), encoding='utf-8')
        original = p.read_bytes()
        for operation in (
                {'op': 'replace', 'path': '/0/config/allow', 'value': ['*', 'secret.use']},
                {'op': 'replace', 'path': '/0/config/readScope', 'value': 'all'},
                {'op': 'add', 'path': '/0/disabled', 'value': True},
                {'op': 'remove', 'path': '/0'},
                {'op': 'add', 'path': '/2', 'value': {'id': 'policy', 'config': {'mode': 'bypass'}}}):
            with self.subTest(operation=operation):
                p.write_bytes(original)
                view = self.registry.invoke('edit_config', {'path': p.name}, self.ctx)
                self.assertTrue(view.ok, view.error)
                result = self.registry.invoke('edit_config', {'path': p.name,
                    'revision': view.meta['revision'], 'patch': [operation]}, self.ctx)
                self.assertFalse(result.ok)
                self.assertEqual(p.read_bytes(), original)
        p.write_bytes(original)
        view = self.registry.invoke('edit_config', {'path': p.name}, self.ctx)
        normal = self.registry.invoke('edit_config', {'path': p.name,
            'revision': view.meta['revision'],
            'patch': [{'op': 'replace', 'path': '/1/config/primary', 'value': 'new'}]}, self.ctx)
        self.assertTrue(normal.ok, normal.error)

    def test_log_record_renamed_keys_and_cached_trace_are_not_retained(self):
        value = 'audit-log-record-credential-811'
        self.scope.reference(value)
        record = logging.LogRecord('audit', logging.WARNING, 'file', 1, 'safe', (), None)
        record.__dict__[value] = value
        record.exc_text = 'previous formatter: ' + value
        SecretLoggingFilter().filter(record)
        self.assertNotIn(value, str(record.__dict__))
        self.assertIn(MARKER, str(record.__dict__))

    def test_log_redaction_failure_does_not_keep_original_extras(self):
        record = logging.LogRecord('audit', logging.WARNING, 'file', 1, 'safe', (), None)
        record.credential = 'audit-fallback-credential-812'
        with patch('forge.secrets.redact', side_effect=ValueError('capacity')):
            SecretLoggingFilter().filter(record)
        self.assertNotIn('audit-fallback-credential-812', str(record.__dict__))
        self.assertIn(MARKER, record.msg)

    def test_structured_log_objects_and_standard_metadata_are_safe(self):
        value = 'audit-object-credential-821'
        self.scope.reference(value)
        class Diagnostic:
            def __str__(self):
                return value
        record = logging.LogRecord(value, logging.WARNING, value, 1, 'safe', (), None)
        record.diagnostic = Diagnostic()
        SecretLoggingFilter().filter(record)
        # A downstream JSON formatter typically uses default=str for extras.
        self.assertNotIn(value, json.dumps(record.__dict__, default=str))

    def test_full_file_inspection_does_not_make_an_unbounded_read(self):
        p = self.root / 'oversized.txt'
        p.write_text('placeholder', encoding='utf-8')
        class BoundedFile(io.BytesIO):
            def read(self, size=-1):
                if size < 0:
                    raise AssertionError('Unbounded reads can freeze the gateway before detection')
                return super().read(size)
        with patch.object(Path, 'open', return_value=BoundedFile(b'x' * (8 * 1024 * 1024 + 1))):
            with self.assertRaisesRegex(ValueError, 'limit|exceeds'):
                self.scope.file_text(p)

    def test_sensitive_mapping_is_independent_of_field_order(self):
        value = 'audit-order-credential-831'
        safe = self.scope.protect({'description': 'echo ' + value, 'password': value})
        self.assertNotIn(value, json.dumps(safe))
        self.assertIn(safe['password'], safe['description'])

    def test_agent_cannot_add_boot_time_environment_expressions(self):
        p = self.root / 'forge.patch.json'
        p.write_text(json.dumps([{'id': 'model', 'config': {'primary': 'old'}}]), encoding='utf-8')
        view = self.registry.invoke('edit_config', {'path': p.name}, self.ctx)
        original = p.read_bytes()
        result = self.registry.invoke('edit_config', {'path': p.name,
            'revision': view.meta['revision'], 'patch': [{'op': 'add', 'path': '/0/config/notes',
                'value': {'$expr': "get('env.FORGE_UNUSED_API_KEY', '')"}}]}, self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(p.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
