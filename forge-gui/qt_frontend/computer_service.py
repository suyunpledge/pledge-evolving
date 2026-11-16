"""Explicit native window grants, presented by Qt instead of a second GUI."""
import base64
import json
import os
from config_model import load_user_layer
from forge.computer_use import ComputerSession
from forge.computer_win32 import WindowsControlDriver, list_windows
from forge.desktop_permissions import profile_policy
from forge.policy import Policy
from forge.secrets import SecretScope
from computer_runtime import ComputerLane
from phase_client import model_catalog
import secret_store
from .workspace_service import field, button


class ComputerService:
    def __init__(self, host):
        self.host = host
        self.session = self.frame = self.lane = None
        self.scope = SecretScope()
        self.windows = []
        self.target = ''

    def view(self):
        models = [{'label': label, 'value': json.dumps(pair)} for pair, label in model_catalog(load_user_layer(self.host.home))]
        return dict(hint=self.target or '选择一个窗口，再明确授权截图或输入。',
            fields=[field('model', '视觉模型', models[0]['value'] if models else '', 'choice', models),
                field('consent', '允许把所选窗口截图发送给视觉模型', False, 'bool'),
                field('clicks', '允许鼠标与键盘输入（每次确认）', False, 'bool'),
                field('question', '分析问题', kind='multiline'), field('target', '定位目标（可选）'),
                field('text', '要输入的文字'), field('key', '快捷键，例如 ctrl+s')],
            actions=[button('windows', '刷新窗口'), button('capture', '截图'), button('analyze', '分析 / 定位'),
                button('click', '点击定位目标'), button('type', '输入文字'), button('key', '按键'), button('revoke', '撤销窗口授权')],
            rows=[dict(id=str(w['window']), title=w['title'], subtitle=f"PID {w['process_id']}", actions=[button('bind', '选择窗口')]) for w in self.windows],
            image=self.image())

    def image(self):
        if not self.frame:
            return ''
        png = self.frame['png']
        return 'data:image/png;base64,' + (base64.b64encode(png).decode() if isinstance(png, bytes) else png)

    def action(self, action, values):
        if action == 'windows':
            self.windows = list_windows(exclude_pid=os.getpid())
            return
        if action == 'revoke':
            self.close()
            return
        if action == 'bind':
            row = next(w for w in self.windows if str(w['window']) == values['id'])
            self.close()
            self.scope = SecretScope()
            self.session = ComputerSession(WindowsControlDriver(row['window'], row['process_id']), ttl=90)
            self.target = row['title']
            return
        if self.session is None:
            raise ValueError('请先选择目标窗口。')
        if action in {'type', 'key', 'click'} and (not values.get('clicks') or self.host.profile == 'sandbox'):
            raise PermissionError('请允许鼠标与键盘输入，并选择手动批准或完全访问。')
        if action in {'capture', 'analyze'}:
            self.lane = ComputerLane(self.session, rows=load_user_layer(self.host.home), selection=json.loads(values.get('model') or 'null'),
                environment={**os.environ, **secret_store.env_for()}, consent=values.get('consent') is True,
                scope=self.scope, cancel=self.host.cancel, workspace=self.host.workspace, home=self.host.home / 'gui/computer-use',
                policy=self.host.policy(), clicks=values.get('clicks') is True,
                approve=lambda target, args, frame, token: self.host.approve('向所选窗口输入', {'target': target, **args}, token))
            self.lane.executor.approver = self.host.approve
            args = {} if action == 'capture' else {'question': values.get('question') or '描述此窗口'}
            if action == 'analyze' and values.get('target'):
                args['target'] = values['target']
            result = self.lane.call('computer_screenshot' if action == 'capture' else 'computer_vision', args)
        else:
            if self.lane is None or self.frame is None:
                raise ValueError('请先重新截图。')
            args = {'observation_id': self.frame['observation_id'], 'reason': values.get('question') or '用户在 Forge 请求输入'}
            if action == 'click':
                if not getattr(self, 'point', None):
                    raise ValueError('请先用视觉模型定位目标。')
                args.update(x=self.point['x'], y=self.point['y'])
            else:
                args['text' if action == 'type' else 'key'] = values.get('text' if action == 'type' else 'key', '')
            result = self.lane.call('computer_' + action, args)
        self.frame = getattr(self.lane, 'last_frame', None)
        if action == 'analyze' and result.get('ok'):
            detail = json.loads(result.get('content', '{}'))
            self.point = detail.get('point')
        self.host.pages.output('computer', result)

    def settings_changed(self):
        if self.session:
            driver = self.session.driver
            self.session.revoke()
            self.session = ComputerSession(driver, ttl=90)
        self.frame = self.lane = None
        self.point = None
        self.host.pages.publish('computer')

    def close(self):
        if self.session:
            self.session.revoke()
        self.session = self.frame = self.lane = None
        self.target = ''
        self.scope.close()
