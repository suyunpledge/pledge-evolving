"""Explicit native window grants, presented by Qt instead of a second GUI."""
import base64
import json
import os
from dataclasses import replace
from config_model import load_user_layer
from keystone.computer_use import ComputerSession, TargetCancellation
from keystone.computer_coordinates import INPUT_NAMES
from keystone.computer_win32 import WindowsControlDriver, list_windows
from keystone.secrets import SecretScope
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
        self.point = None

    def view(self):
        models = [{'label': label, 'value': json.dumps(pair)} for pair, label in model_catalog(load_user_layer(self.host.home))]
        return dict(hint=(f'已绑定「{self.target}」。手动批准或完全访问下，Work 对话可观察此窗口。'
                          if self.target else '刷新并选择要操作的窗口。'),
            fields=[field('model', '视觉模型', models[0]['value'] if models else '', 'choice', models),
                field('consent', '允许把所选窗口截图发送给视觉模型', False, 'bool'),
                field('clicks', '允许鼠标与键盘输入（每次确认）', False, 'bool'),
                field('question', '分析问题', kind='multiline'), field('target', '定位目标（可选）'),
                field('text', '要输入的文字'), field('key', '快捷键，例如 ctrl+s')],
            actions=[button('windows', '刷新窗口'), button('capture', '截图'), button('analyze', '分析 / 定位'),
                button('type', '输入文字'), button('key', '按键'), button('revoke', '撤销窗口授权')],
            rows=[dict(id=str(w['window']), title=w['title'], subtitle=f"PID {w['process_id']}", actions=[button('bind', '选择窗口')]) for w in self.windows],
            image=self.image(), observationId=(self.frame or {}).get('observation_id', ''),
            point=self.point, canInput=bool(self.lane and self.lane.clicks and self.host.profile != 'sandbox'))

    def image(self):
        if not self.frame:
            return ''
        png = self.frame['png']
        return 'data:image/png;base64,' + (base64.b64encode(png).decode() if isinstance(png, bytes) else png)

    def _lane(self, values):
        consent = values.get('consent') is True
        lane = ComputerLane(self.session, rows=load_user_layer(self.host.home) if consent else [],
            selection=json.loads(values.get('model') or 'null') if consent else None,
            environment={**os.environ, **secret_store.env_for()} if consent else {}, consent=consent,
            scope=self.scope, cancel=self.host.cancel, workspace=self.host.workspace,
            home=self.host.home / 'gui/computer-use', policy=self.host.policy(),
            clicks=self.host.profile != 'sandbox' and values.get('clicks') is True,
            approve=lambda target, args, frame, token: self.host.approve('向所选窗口输入', {'target': target, **args}, token))
        lane.executor.approver = self.host.approve
        return lane

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
            self.lane = self._lane({})
            self.host.emit('form_values', page='computer', values={'consent': False, 'clicks': False})
            return
        if self.session is None:
            raise ValueError('请先选择目标窗口。')
        if action in {'type', 'key', 'click'} and (values.get('clicks') is not True or self.host.profile == 'sandbox'):
            raise PermissionError('请允许鼠标与键盘输入，并选择手动批准或完全访问。')
        if action in {'capture', 'analyze'}:
            self.lane = self._lane(values)
            args = {} if action == 'capture' else {'question': values.get('question') or '描述此窗口'}
            if action == 'analyze' and values.get('target'):
                args['target'] = values['target']
            name = 'computer_screenshot' if action == 'capture' else 'computer_vision'
        else:
            if action not in {'click', 'type', 'key'}:
                raise ValueError('未知的屏幕操作。')
            if self.lane is None or not (self.frame or {}).get('observation_id'):
                raise ValueError('请先重新截图。')
            args = {'observation_id': self.frame['observation_id'], 'reason': values.get('question') or '用户在 Keystone 请求输入'}
            if action == 'click':
                if values.get('observation_id') != args['observation_id']:
                    raise ValueError('预览已更新，请在新截图上重新选择位置。')
                args.update(x=values.get('x'), y=values.get('y'), reason='点击截图中选定的位置')
            else:
                args['text' if action == 'type' else 'key'] = values.get('text' if action == 'type' else 'key', '')
            name = 'computer_' + action
        lane = self.lane
        result = self.call(name, args)
        if self.lane is lane and not lane.session.revoked.is_set():
            self.host.pages.output('computer', result, publish=False)

    def call(self, name, args, *, grant=None):
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
        if visual and self.lane is lane and not lane.session.revoked.is_set():
            self.frame = lane.last_frame if result.get('ok') else None
            point = (self.frame or {}).get('point')
            self.point = point if point and point.get('found') is True else None
            if grant is not None and self.host.page == 'computer':
                self.host.pages.publish('computer')
        return result

    def settings_changed(self):
        if self.session:
            driver = self.session.driver
            self.session.revoke()
            self.session = ComputerSession(driver, ttl=90)
        self.frame = self.lane = None
        self.point = None
        self.host.pages.results.pop('computer', None)
        self.host.pages.publish('computer')

    def close(self):
        if self.session:
            self.session.revoke()
        self.session = self.frame = self.lane = None
        self.target = ''
        self.point = None
        self.host.pages.results.pop('computer', None)
        self.scope.close()
