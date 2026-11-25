from pathlib import Path
from keystone.config import load_config
from keystone.desktop_permissions import profile_policy
from keystone.development.project_check import DESCRIPTION, SCHEMA
from keystone.execution import ActionExecutor, ActionRequest, create_execution_context
from keystone.policy import Policy
from keystone.tools import build_builtin_registry


def definition():
    return {'type': 'function', 'function': {'name': 'keystone_run_check', 'description': DESCRIPTION,
            'parameters': {'type': 'object', 'properties': SCHEMA, 'required': ['task'], 'additionalProperties': False}}}


def execute(home, workspace, profile, approve, arguments, cancel):
    policy = profile_policy(Policy.from_config(load_config(home), workspace=Path(workspace).resolve()), profile)
    registry = build_builtin_registry()
    registry.activate('run_check')
    ctx = create_execution_context(home=home, workspace=workspace, policy=policy, registry=registry,
                                   capabilities={'check_dialog': approve, 'desktop_check_interactive': True}, cancellation=cancel)
    try:
        return ActionExecutor(approver=approve).execute(ctx, ActionRequest('run_check', arguments)).as_tool_result().as_dict()
    finally:
        if ctx.owns_session:
            ctx.session.close()
        ctx.secret_scope.close()
