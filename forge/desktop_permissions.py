"""Explicit desktop grants. Configuration and model output cannot select a grant."""
from dataclasses import dataclass, fields, replace
import hashlib
import json

from .policy import Decision, Mode, Policy, ReadScope, Sandbox

PROFILES = ('sandbox', 'manual', 'full')
LABELS = {'sandbox': '沙箱运行', 'manual': '手动批准', 'full': '完全访问'}


def profile_policy(base, profile):
    if profile not in PROFILES:
        raise ValueError('Unknown desktop permission profile')
    if profile == 'sandbox':
        return replace(base, mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.WORKSPACE_WRITE,
                       read_scope=ReadScope.WORKSPACE, read_roots=(), non_interactive=True)
    return replace(base, mode=Mode.DEFAULT if profile == 'manual' else Mode.BYPASS,
                   sandbox=Sandbox.FULL_ACCESS, read_scope=ReadScope.ALL,
                   ask=('*',) if profile == 'manual' else base.ask,
                   non_interactive=profile != 'manual')


def fingerprint(arguments):
    # Revision guards may add host-owned optimistic-lock metadata after approval.
    value = {k: v for k, v in arguments.items() if k != '_expected_revisions'}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class _ApprovedActionPolicy(Policy):
    original: Policy = None
    approved_tool: str = ''
    approved_digest: str = ''

    def evaluate(self, tool, *, args=None, touching=(), effect=None):
        decision = self.original.evaluate(tool, args=args, touching=touching, effect=effect)
        if (decision is Decision.ASK and tool == self.approved_tool
                and fingerprint(args or {}) == self.approved_digest):
            return Decision.ALLOW
        return decision


def approve_action(policy, tool, arguments):
    """A temporary policy for one executor call, never installed on the Agent."""
    return _ApprovedActionPolicy(**{f.name: getattr(policy, f.name) for f in fields(Policy)},
        original=policy, approved_tool=tool, approved_digest=fingerprint(arguments))
