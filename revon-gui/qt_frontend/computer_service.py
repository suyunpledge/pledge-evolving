"""Explicit native window grants, presented by Qt instead of a second GUI."""
import base64
import json
import os
from pathlib import Path
import subprocess
import threading
from dataclasses import replace
from urllib.parse import urlsplit
from config_model import load_user_layer
from revon.computer_use import ComputerSession, TargetCancellation
from revon.computer_coordinates import INPUT_NAMES
from revon.computer_win32 import WindowsControlDriver, is_edge_process, list_windows
from revon.secrets import SecretScope
from computer_runtime import ComputerLane
from phase_client import model_catalog
import secret_store
from .workspace_service import field, button

CONTROL_LABELS = {'type_text': '设置文本', 'click': '执行', 'expand': '展开菜单', 'collapse': '收起菜单'}


def launch_background_edge(home, address):
    url = str(address or '').strip()
    parts = urlsplit(url)
    if (parts.scheme not in ('https', 'http') or not parts.hostname or parts.username is not None
            or parts.password is not None or any(ord(c) < 32 for c in url)):
        raise ValueError('请输入完整的 HTTP 或 HTTPS 页面地址。')
    for key in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
        folder = os.environ.get(key)
        if folder:
            exe = Path(folder) / 'Microsoft/Edge/Application/msedge.exe'
            if exe.is_file():
                break
    else:
        raise FileNotFoundError('未找到 Microsoft Edge。')
    profile = home / 'gui' / 'background-edge'
    profile.mkdir(parents=True, exist_ok=True)
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 4  # SW_SHOWNOACTIVATE
    subprocess.Popen([str(exe), '--user-data-dir=' + str(profile), '--no-first-run',
                      '--no-default-browser-check', '--disable-sync', '--disable-backgrounding-occluded-windows',
                      '--force-renderer-accessibility', '--app=' + url], startupinfo=startup)


class ComputerService:
    def __init__(self, host):
        self.host = host
        self.session = self.frame = self.lane = None
        self.scope = SecretScope()
        self.windows = []
        self.target = ''
        self.point = None
        self.controls = None
        self.input_mode = 'background'
        self.automatic_session = None
        self._grant_lock = threading.RLock()

    def view(self):
        models = [{'label': label, 'value': json.dumps(pair)} for pair, label in model_catalog(load_user_layer(self.host.home))]
        actions = [button('windows', '刷新窗口'), button('capture', '截图'), button('local', '本地识别 / 定位'),
                   button('analyze', '视觉模型分析')]
        rows = []
        can_launch_edge = os.name == 'nt' and self.host.profile != 'sandbox' and self.input_mode == 'background'
        if can_launch_edge:
            actions.append(button('launch_edge', '打开独立后台 Edge'))
        if self.input_mode == 'background':
            actions.append(button('observe', '读取后台控件'))
            if self.session and self.host.profile == 'full':
                actions.append(button('review_each', '恢复逐次审批') if self.automatic_session else
                               button('authorize_background', '授权此窗口后台连续执行'))
            observation = self.controls or {}
            if 'next_offset' in observation:
                actions.append(button('observe_more', '下一页后台控件'))
            for item in observation.get('state', {}).get('elements', []):
                allowed = item['actions'] if self.lane and self.lane.clicks else []
                rows.append(dict(id=observation['observation_id'] + '|' + item['ref'],
                    title=item.get('name') or item['class'], subtitle=item['class'],
                    actions=[button('control_' + op, CONTROL_LABELS[op])
                             for op in allowed]))
        else:
            actions.extend([button('type', '输入文字'), button('key', '按键')])
        hint = ('后台连续执行已开启，可随时撤销窗口授权。' if self.automatic_session else
                '后台输入默认逐次确认。坐标和快捷键需选择前台输入。')
        if self.controls and not self.controls['state']['elements']:
            hint += ' 此窗口当前未提供可操作的后台控件。'
        return dict(hint=(f'已绑定「{self.target}」。{hint}'
                          if self.target else '刷新并选择要操作的窗口。'),
            fields=[field('model', '视觉模型', models[0]['value'] if models else '', 'choice', models),
                field('backgroundUrl', '独立后台 Edge 的页面地址'),
                field('consent', '允许把所选窗口截图发送给视觉模型', bool(self.lane and self.lane.consent), 'bool'),
                field('inputMode', '操作方式', self.input_mode, 'choice', [
                    {'label': '后台控件操作', 'value': 'background'}, {'label': '前台鼠标与键盘', 'value': 'foreground'}]),
                field('clicks', '允许向所选窗口输入', bool(self.lane and self.lane.clicks), 'bool'),
                field('question', '分析问题', kind='multiline'), field('target', '定位目标（可选）'),
                field('text', '要输入的文字（后台设置会替换整个字段）'), field('key', '前台快捷键，例如 ctrl+s')],
            actions=actions + [button('revoke', '撤销窗口授权')],
            rows=rows + [dict(id=str(w['window']), title=w['title'], subtitle=f"PID {w['process_id']}",
                actions=([button('bind_edge_page', '选择 Edge 网页')] if can_launch_edge and w.get('edge') else [])
                         + [button('bind', '选择窗口')]) for w in self.windows],
            canLaunchEdge=can_launch_edge, inputMode=self.input_mode, image=self.image(), observationId=(self.frame or {}).get('observation_id', ''),
            point=self.point, canInput=bool(self.lane and not self.lane.background and self.lane.clicks and self.host.profile != 'sandbox'))

    def image(self):
        if not self.frame:
            return ''
        png = self.frame['png']
        return 'data:image/png;base64,' + (base64.b64encode(png).decode() if isinstance(png, bytes) else png)

    def _lane(self, values):
        consent = values.get('consent') is True
        session = self.session
        def approve_input(target, args, frame, token):
            if token.is_set():
                return False
            if (self.automatic_session is session and self.session is session and
                    self.input_mode == 'background' and self.host.profile == 'full'):
                return True
            return self.host.approve('向所选窗口输入', {'target': target, **args}, token)
        lane = ComputerLane(session, rows=load_user_layer(self.host.home) if consent else [],
            selection=json.loads(values.get('model') or 'null') if consent else None,
            environment={**os.environ, **secret_store.env_for()} if consent else {}, consent=consent,
            scope=self.scope, cancel=self.host.cancel, workspace=self.host.workspace,
            home=self.host.home / 'gui/computer-use', policy=self.host.policy(),
            input_mode=self.input_mode,
            clicks=self.host.profile != 'sandbox' and values.get('clicks') is True,
            approve=approve_input)
        lane.executor.approver = self.host.approve
        return lane

    def action(self, action, values):
        if action == 'launch_edge':
            if os.name != 'nt' or self.host.profile == 'sandbox' or self.input_mode != 'background':
                raise PermissionError('请在手动批准或完全访问下选择后台控件操作。')
            launch_background_edge(self.host.home, values.get('backgroundUrl'))
            self.host.pages.output('computer', 'Edge 已启动。页面加载后，刷新窗口列表并选择目标窗口。', publish=False)
            return
        if action == 'windows':
            self.windows = list_windows(exclude_pid=os.getpid())
            for row in self.windows:
                row['edge'] = is_edge_process(row['process_id'])
            return
        if action == 'revoke':
            self.close()
            return
        if action in {'bind', 'bind_edge_page'}:
            row = next(w for w in self.windows if str(w['window']) == values['id'])
            if action == 'bind_edge_page' and (self.host.profile == 'sandbox' or self.input_mode != 'background' or not row.get('edge')):
                raise PermissionError('请选择普通 Edge 网页。')
            with self._grant_lock:
                self.close()
                self.scope = SecretScope()
                driver = (WindowsControlDriver(row['window'], row['process_id'], edge_page=True)
                          if action == 'bind_edge_page' else WindowsControlDriver(row['window'], row['process_id']))
                if action == 'bind_edge_page':
                    # Pin the document the user selected before granting model access.
                    with driver.background_controls():
                        pass
                self.session = ComputerSession(driver, ttl=90)
                self.target = row['title']
                self.lane = self._lane({})
                self.host.emit('form_values', page='computer', values={'consent': False, 'clicks': False, 'inputMode': self.input_mode})
            return
        if self.session is None:
            raise ValueError('请先选择目标窗口。')
        if action in {'authorize_background', 'review_each'}:
            session = self.session
            if self.host.profile != 'full' or self.input_mode != 'background':
                raise PermissionError('连续执行仅适用于完全访问下的后台控件操作。')
            automatic = action == 'authorize_background'
            token = TargetCancellation(self.host.cancel, session.revoked)
            if automatic and not self.host.approve('允许此窗口连续执行后台控件操作？', {
                    '窗口': self.target, '目标': session.driver.target_id,
                    '范围': '可读取控件、替换可编辑字段和触发按钮，无需逐次确认。切换窗口、操作方式或权限即结束。'}, token):
                return
            with self._grant_lock:
                if token.is_set() or self.session is not session or self.host.profile != 'full':
                    raise PermissionError('窗口授权已变化，请重新选择。')
                self.settings_changed({**values, 'inputMode': 'background', 'clicks': True}, automatic=automatic)
                self.host.emit('form_values', page='computer', values={'clicks': True})
            return
        if action in {'type', 'key', 'click'} and (values.get('clicks') is not True or self.host.profile == 'sandbox'):
            raise PermissionError('请允许鼠标与键盘输入，并选择手动批准或完全访问。')
        if action in {'observe', 'observe_more', 'capture', 'analyze', 'local'}:
            with self._grant_lock:
                if self.session is None:
                    raise PermissionError('窗口授权已撤销。')
                if self.lane is None:
                    self.lane = self._lane({})
        if action in {'observe', 'observe_more'}:
            if self.input_mode != 'background':
                raise ValueError('请先选择后台控件操作。')
            name, args = 'computer_observe', {}
            if action == 'observe_more':
                if 'next_offset' not in (self.controls or {}):
                    raise ValueError('控件列表已更新，请重新读取。')
                args['offset'] = self.controls['next_offset']
        elif action.startswith('control_') and action[8:] in CONTROL_LABELS:
            if not self.lane or not self.lane.background or not self.lane.clicks or self.host.profile == 'sandbox':
                raise PermissionError('请先授权后台控件输入。')
            observation_id, ref = values['id'].split('|', 1)
            name, args = 'computer_act', {'observation_id': observation_id, 'ref': ref, 'action': action[8:]}
            if action == 'control_type_text':
                args['text'] = values.get('text', '')
        elif action in {'capture', 'analyze', 'local'}:
            args = {}
            if action == 'analyze':
                args['question'] = values.get('question') or '描述此窗口'
                if values.get('target'):
                    args['target'] = values['target']
            elif action == 'local':
                target = (values.get('target') or '').strip()
                args = {'target': target} if target else {'list_controls': True}
            name = 'computer_vision' if action == 'analyze' else 'computer_screenshot'
        else:
            if action not in {'click', 'type', 'key'}:
                raise ValueError('未知的屏幕操作。')
            if self.input_mode != 'foreground':
                raise PermissionError('后台模式仅支持按控件操作。')
            if self.lane is None or not (self.frame or {}).get('observation_id'):
                raise ValueError('请先重新截图。')
            args = {'observation_id': self.frame['observation_id'], 'reason': values.get('question') or '用户在 revon 请求输入'}
            if action == 'click':
                if values.get('observation_id') != args['observation_id']:
                    raise ValueError('预览已更新，请在新截图上重新选择位置。')
                args.update(x=values.get('x'), y=values.get('y'), reason='点击截图中选定的位置')
            else:
                args['text' if action == 'type' else 'key'] = values.get('text' if action == 'type' else 'key', '')
            name = 'computer_' + action
        lane = self.lane
        if lane is None:
            raise PermissionError('窗口授权已撤销。')
        result = self.call(name, args, grant=lane.session.grant_id, publish=False)
        with self._grant_lock:
            if self.lane is lane and not lane.session.revoked.is_set():
                self.host.pages.output('computer', result, publish=False)

    def call(self, name, args, *, grant=None, publish=True):
        lane = self.lane
        if lane is None:
            raise PermissionError('请先选择窗口并截图。')
        if grant is not None and grant != lane.session.grant_id:
            raise PermissionError('窗口授权已变化，请为新目标发起新一轮操作。')
        lane.cancel = TargetCancellation(self.host.cancel, lane.session.revoked)
        lane.ctx = replace(lane.ctx, cancellation=lane.cancel)
        visual = name in INPUT_NAMES or name in {'computer_screenshot', 'computer_vision'}
        if visual:
            lane.last_frame = None
        result = lane.call(name, args)
        semantic = lane.background and name in {'computer_act', 'computer_observe'}
        with self._grant_lock:
            if self.lane is not lane or lane.session.revoked.is_set():
                if not result.get('ok'):
                    return result
                wrote = name in INPUT_NAMES or name == 'computer_act'
                return {'ok': False, 'error': '窗口授权已变化；已发出的操作请核实结果。' if wrote else '窗口授权已变化。',
                        'meta': {'error_code': 'capability_revoked', 'execution_state': 'unknown' if wrote else 'cancelled'}}
            if semantic:
                self.controls = json.loads(result['content']) if result.get('ok') else None
                if name == 'computer_act':
                    self.frame = self.point = None
            if visual:
                self.frame = lane.last_frame if result.get('ok') else None
                point = (self.frame or {}).get('point')
                self.point = point if point and point.get('found') is True else None
            if (semantic or visual) and publish and grant is not None and self.host.page == 'computer':
                self.host.pages.publish('computer')
        return result

    def settings_changed(self, values=None, *, automatic=False):
        with self._grant_lock:
            if self.session:
                driver = self.session.driver
                self.session.revoke()
                self.session = ComputerSession(driver, ttl=90)
            self.automatic_session = self.session if automatic else None
            self.frame = self.lane = None
            self.point = self.controls = None
            if values is not None:
                self.input_mode = 'foreground' if values.get('inputMode') == 'foreground' else 'background'
                if self.session:
                    self.lane = self._lane(values)
            else:
                self.host.emit('form_values', page='computer', values={'clicks': False, 'consent': False})
        self.host.pages.results.pop('computer', None)
        self.host.pages.publish('computer')

    def close(self):
        with self._grant_lock:
            if self.session:
                self.session.revoke()
            self.automatic_session = self.session = self.frame = self.lane = None
            self.target = ''
            self.point = self.controls = None
            self.input_mode = 'background'
            self.host.pages.results.pop('computer', None)
            self.scope.close()
