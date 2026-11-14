"""Desktop permission boundary for tools dispatched to an existing gateway."""
from pathlib import Path
from forge.policy import Decision, Policy, ToolEffect, READ_PATH_TOOLS, WRITE_TOOLS
from forge.desktop_permissions import profile_policy

MEDIATED = READ_PATH_TOOLS | {'write_file', 'edit_file', 'apply_patch', 'delete_file', 'tool_search'}


def authorize(profile, workspace, name, args, approve, cancel=None):
    if cancel is not None and cancel.is_set():
        raise PermissionError('操作已取消')
    if not isinstance(args, dict):
        raise ValueError('工具参数必须是对象')
    if profile == 'sandbox' and name not in MEDIATED:
        raise PermissionError('沙箱模式只允许工作区内的受控文件工具；系统命令、电脑操作和外部写入未开放。')
    policy = profile_policy(Policy(workspace=Path(workspace).resolve()), profile)
    paths = [str(v) for k, v in args.items() if k in {'path', 'file', 'root', 'target', 'source', 'destination'} and v]
    for block in args.get('patches', args.get('edits', [])) or []:
        if isinstance(block, dict) and block.get('path'):
            paths.append(str(block['path']))
    # The gateway/connector owns registration and enforces its own independent
    # policy. Sandbox exposes only the mediated allowlist above; manual/full
    # can reach other host tools after this explicit desktop grant.
    effect = ToolEffect.FILE_READ if name in READ_PATH_TOOLS else ToolEffect.WRITE if name in WRITE_TOOLS else None
    decision = policy.evaluate(name, args=args, touching=paths, effect=effect)
    decision = policy.resolve_ask(decision)
    if decision is Decision.DENY:
        raise PermissionError('当前权限或已有拒绝规则禁止此操作。')
    if decision is Decision.ASK and not approve(name, args, cancel):
        raise PermissionError('用户未批准此操作。')
    if cancel is not None and cancel.is_set():
        raise PermissionError('操作已取消')

