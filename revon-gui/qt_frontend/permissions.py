"""Desktop permission boundary for tools dispatched to an existing gateway."""
from pathlib import Path
from revon.artifact_tools import READERS
from revon.policy import Decision, Policy, ToolEffect, READ_PATH_TOOLS, WRITE_TOOLS
from revon.desktop_permissions import profile_policy
from .productivity_tools import CATALOG, FILES
from .office_controller import CATALOG as OFFICE, OFFICE_WRITES

MEDIATED = READ_PATH_TOOLS | set(READERS) | CATALOG.keys() | OFFICE.keys() | {'write_file', 'edit_file', 'apply_patch', 'delete_file', 'tool_search',
                             'public_search', 'view_image', 'read_pdf', 'office_render', 'run_check', '7zip_extract', 'generate_image', 'generate_video',
                             'calculator', 'datetime', 'text_transform', 'unit_convert'}


def tool_available(profile, name):
    return isinstance(name, str) and (profile != 'sandbox' or name.removeprefix('keystone_') in MEDIATED)

def authorize(profile, workspace, name, args, approve, cancel=None, template=None):
    # /v1/tools advertises keystone_* names; the gateway resolves one prefix
    # before policy evaluation. Match that same canonical identity locally.
    if not isinstance(name, str):
        raise ValueError('工具名称必须是字符串')
    if cancel is not None and cancel.is_set():
        raise PermissionError('操作已取消')
    if not isinstance(args, dict):
        raise ValueError('工具参数必须是对象')
    if profile == 'sandbox' and name.removeprefix('keystone_') == 'comfyui_run':
        raise PermissionError('ComfyUI 是本机外部服务，请在 Work 中切换到手动批准或完全访问后重试。')
    if not tool_available(profile, name):
        raise PermissionError('此工具在沙箱模式下不可用。')
    if template is not None and not template.tool_allowed(name):
        raise PermissionError(f'当前角色「{template.name}」未开放此工具。')
    name = name.removeprefix('keystone_')
    policy = profile_policy(Policy(workspace=Path(workspace).resolve()), profile)
    paths = [str(v) for k, v in args.items() if k in {'path', 'file', 'root', 'target', 'source', 'destination'} and v]
    if name == 'apply_patch':
        blocks = args.get('patches', args.get('edits', []))
        if not isinstance(blocks, list):
            raise ValueError('补丁参数必须是数组。')
        paths.extend(str(block['path']) for block in blocks if isinstance(block, dict) and block.get('path'))
    # The gateway/connector owns registration and enforces its own independent
    # policy. Sandbox exposes only the mediated allowlist above; manual/full
    # can reach other host tools after this explicit desktop grant.
    effect = ToolEffect.FILE_READ if name in READ_PATH_TOOLS else None
    if name in {'view_image', 'read_pdf'}:
        effect = ToolEffect.FILE_READ
    if name in READERS:
        effect = ToolEffect.READ
    if name in {'office_list', 'office_read', 'office_render'}:
        effect = ToolEffect.FILE_READ
    if name in WRITE_TOOLS or name in FILES or name in OFFICE_WRITES or name in {'7zip_extract', 'comfyui_run', 'generate_image', 'generate_video'}:
        effect = ToolEffect.WRITE
    if name == 'run_check':
        # The Work handler uses the composed policy and presents the resolved command
        # through ActionExecutor. Do not ask twice or approve an unresolved task here.
        if template and template.own_policy(Path(workspace)).evaluate(name, effect=ToolEffect.DELEGATED_WRITE) is Decision.DENY:
            raise PermissionError('当前角色不允许运行项目验证。')
        return
    decision = policy.evaluate(name, args=args, touching=paths, effect=effect)
    decision = policy.resolve_ask(decision)
    if template is not None:
        # 角色模板叠在桌面授权之上，只会更严：它拒绝的一律拒绝；它要求询问而桌面
        # 授权本可直接放行时，改为征求用户批准。模板永远不能把拒绝或询问变成放行。
        role = template.own_policy(Path(workspace).resolve())
        verdict = role.evaluate(name, args=args, touching=paths, effect=effect)
        if verdict is Decision.DENY:
            raise PermissionError(f'当前角色「{template.name}」不允许此操作'
                                  f'（{template.mode.value} / {template.sandbox.value}）。')
        if verdict is Decision.ASK and decision is Decision.ALLOW:
            decision = Decision.ASK
    if decision is Decision.DENY:
        raise PermissionError('当前权限或已有拒绝规则禁止此操作。')
    if decision is Decision.ASK and not approve(name, args, cancel):
        raise PermissionError('用户未批准此操作。')
    if cancel is not None and cancel.is_set():
        raise PermissionError('操作已取消')

