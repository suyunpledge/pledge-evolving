"""「工具与插件」页：一切皆插件。

参照 DeepSeek Harness 的做法，把三类东西放在同一张表里管理：

* 运行席位（内置插件）：沉思、压缩、路由、调度、团队、策展、回放、监督。包内模块
  保底，``<home>/contrib/<席位>.py`` 过闸可接管，可在这里停用（用户层 ``seats`` 行）；
* 已安装插件：工具 / 技能 / 智能体 / 补丁层（contributions.patch，合成进配置）……
  状态一目了然：需确认 → 信任 → 启用 → 授权；
* 可安装：内置目录与已同步的在线目录。

页面只产出行数据与动作，执行仍走 plugin_market 的既有校验（指纹确认、宿主授权行拒收）。
"""
from __future__ import annotations

import copy
import json

from config_model import load_user_layer, save_user_layer

SEAT_LABELS = {
    'thinking': ('沉思', '回答/任务前的多轮假设、批判与收敛'),
    'compactor': ('上下文压缩', '长对话接近上限时压缩历史'),
    'router': ('任务路由', '按任务难度挑选模型档位'),
    'scheduler': ('定时调度', '检查到期的定时任务'),
    'teams': ('团队通信', '子 Agent 之间传递消息'),
    'supervisor': ('监督', '运行中检查偏航与停滞'),
    'curator': ('知识策展', '任务结束后整理长期知识'),
    'replay': ('回放', '把一次运行整理成时间线'),
}
CONTRIBUTION_LABELS = {'tools': '工具', 'skills': '技能', 'agents': '智能体', 'patch': '补丁层',
                       'policies': '策略', 'hooks': '钩子', 'providers': '模型', 'ui_panels': '面板'}


def button(key, label, *, danger=False):
    return dict(key=key, label=label, danger=danger)


def seat_status(home):
    """[(seat, state)] where state is builtin / home / off / empty."""
    from pathlib import Path
    from revon.extensions import SEATS, disabled_seats
    off = disabled_seats(Path(home))
    contrib = Path(home) / 'contrib'
    out = []
    for seat in SEATS:
        if seat in off:
            state = 'off'
        elif (contrib / f'{seat}.py').is_file():
            state = 'home'
        else:
            state = 'builtin'
        out.append((seat, state))
    return out


def set_seat(home, seat, enabled):
    from revon.extensions import SEATS
    if seat not in SEATS:
        raise ValueError('未知的运行席位。')
    latest = load_user_layer(home)
    rows = copy.deepcopy(latest)
    row = next((r for r in rows if r.get('id') == 'seats'), None)
    if row is None:
        row = {'id': 'seats', 'name': 'seats:disabled', 'config': {'disabled': []}}
        rows.append(row)
    disabled = [s for s in row.setdefault('config', {}).get('disabled', []) if s in SEATS and s != seat]
    if not enabled:
        disabled.append(seat)
    row['config']['disabled'] = sorted(set(disabled))
    save_user_layer(home, rows, expected_rows=latest)


def contribution_text(plugin):
    parts = []
    for kind, items in sorted(plugin.contributions.items()):
        if items:
            parts.append(f"{CONTRIBUTION_LABELS.get(kind, kind)} {len(items)}")
    tools = plugin.provides.get('tools') if isinstance(plugin.provides, dict) else None
    if tools and 'tools' not in plugin.contributions:
        parts.append(f'工具 {len(tools)}')
    return '、'.join(parts)


def plugin_state(plugin, workspace):
    if plugin.error:
        return 'error', '文件无效'
    if not plugin.installed:
        return 'available', '可安装'
    if plugin.needs_ack:
        return 'needs_ack', '需确认信任'
    if not plugin.enabled:
        return 'installed', '已安装 · 未启用'
    if plugin.declared_capabilities and not plugin.granted_capabilities(workspace):
        return 'enabled', '已启用 · 本工作区未授权'
    return 'enabled', '已启用'


def plugin_actions(plugin, state):
    if state == 'available':
        return [button('install', '安装'), button('inspect', '详情')]
    actions = [button('inspect', '详情')]
    if state == 'needs_ack':
        actions.append(button('ack', '确认信任'))
    elif state == 'installed':
        actions.append(button('enable', '启用'))
    elif state == 'enabled':
        actions.append(button('disable', '停用'))
        if plugin.declared_capabilities:
            actions.append(button('grant', '授权本工作区'))
            if plugin.grants:
                actions.append(button('revoke', '撤销授权'))
    actions.append(button('uninstall', '卸载', danger=True))
    return actions


ORDER = {'error': 0, 'needs_ack': 1, 'enabled': 2, 'installed': 3, 'available': 4}


def view(home, market, workspace, query=''):
    query = str(query or '').strip().lower()
    rows = []
    for seat, state in seat_status(home):
        name, summary = SEAT_LABELS.get(seat, (seat, ''))
        status = {'builtin': '内置模块', 'home': '已由 contrib/' + seat + '.py 接管', 'off': '已停用'}[state]
        rows.append(dict(id='seat:' + seat, title=f'⚙ {name}（运行席位）', subtitle=f'{summary} · {status}',
                         actions=[button('seatOn', '启用') if state == 'off' else button('seatOff', '停用')]))
    plugins = []
    catalog = market.catalog()
    for plugin in catalog:
        state, label = plugin_state(plugin, workspace)
        contributes = contribution_text(plugin)
        bits = [plugin.kind_label, f'v{plugin.version}', label]
        if contributes:
            bits.insert(2, '贡献：' + contributes)
        if plugin.error:
            bits.append(plugin.error[:80])
        plugins.append((ORDER[state], plugin.name.lower(), dict(
            id=plugin.id, installed=plugin.installed, state=state, title=f'{plugin.icon} {plugin.name or plugin.id}',
            subtitle=(plugin.summary or plugin.description)[:120] + ' · ' + ' · '.join(bits),
            actions=plugin_actions(plugin, state))))
    rows += [row for *_, row in sorted(plugins, key=lambda item: item[:2])]
    if query:
        rows = [r for r in rows if query in (r['id'] + r['title'] + r['subtitle']).lower()]
    from revon.config import plugin_layers
    enabled = sum(1 for p in catalog if p.enabled)
    layers = plugin_layers(home)
    return dict(
        hint=('一切皆插件：运行席位、工具、技能、智能体与配置补丁层都在这里管理。'
              f'已启用 {enabled} 个插件，{len(layers)} 个补丁层参与配置合成（位于内置配置与你的用户层之间，用户层优先）。'
              '插件不能修改权限、沙箱与工具暴露等授权行。'),
        fields=[dict(key='query', label='筛选（名称、类型、状态）', value=query, kind='text', options=[]),
                dict(key='path', label='本地插件目录', value='', kind='text', options=[]),
                dict(key='capabilities', label='授权能力（逗号分隔，留空为全部声明能力）', value='', kind='text', options=[])],
        actions=[button('filter', '筛选'), button('installDir', '导入插件'), button('sync', '刷新在线目录'),
                 button('updates', '检查更新'), button('doctor', '体检'), button('provenance', '配置来源'),
                 button('audit', '审计记录'), button('logs', '运行日志')],
        rows=rows)


def act(home, market, workspace, action, values, approve):
    """Run one page action; returns the JSON-able result shown under the page (or None)."""
    from pathlib import Path
    key = str(values.get('id', ''))
    if key.startswith('seat:'):
        if action not in {'seatOn', 'seatOff'}:
            raise ValueError('未知的运行席位操作。')
        set_seat(Path(home), key[5:], action == 'seatOn')
        return {'seat': key[5:], 'enabled': action == 'seatOn', 'note': '新开的对话与任务生效'}
    if action == 'filter':
        return None
    if action == 'installDir':
        return market.install_from_dir(values['path']).to_dict()
    if action == 'sync':
        return market.sync_sources()
    if action == 'updates':
        return market.check_updates() or '没有可用更新。'
    if action == 'doctor':
        from plugin_market import doctor_report
        text, healthy = doctor_report(market, workspace)
        return {'healthy': healthy, 'report': text}
    if action == 'provenance':
        from revon.config import load_config
        from revon.cli import BUNDLE_DIR
        cfg = load_config(Path(home), bundles=sorted(BUNDLE_DIR.glob('*.json')))
        return {'layers': cfg.history, 'rows': cfg.provenance(), 'warnings': cfg.warnings}
    if action == 'audit':
        return market.audit_tail()
    if action == 'logs':
        return market.log_tail()
    plugin = market.find(key)
    if plugin is None:
        raise ValueError('插件已移除。')
    if action in {'ack', 'grant', 'uninstall'} and not approve('插件 ' + action, plugin.to_dict()):
        return None
    if action == 'inspect':
        return plugin.to_dict()
    if action == 'install':
        return market.install_from_catalog(key).to_dict()
    if action == 'ack':
        return market.ack(key, expected_fingerprint=plugin.ack_of()).to_dict()
    if action == 'grant':
        declared = sorted(plugin.declared_capabilities)
        wanted = [c.strip() for c in str(values.get('capabilities', '')).replace('，', ',').split(',') if c.strip()]
        unknown = [c for c in wanted if c not in declared]
        if unknown:
            raise ValueError('插件未声明这些能力：' + '、'.join(unknown))
        return market.grant(key, wanted or declared, workspace, expected_fingerprint=plugin.ack_of())
    if action == 'revoke':
        return market.revoke_grants(key, workspace=workspace)
    if action in {'enable', 'disable', 'uninstall'}:
        result = getattr(market, action)(key)
        return result.to_dict() if hasattr(result, 'to_dict') else result
    raise ValueError('未知操作。')


def render(result):
    from revon.secrets import redact
    return json.dumps(redact(result), ensure_ascii=False, indent=2, default=str)
