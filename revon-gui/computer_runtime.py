"""Headless screenshot-bound computer tools shared by desktop frontends."""
from dataclasses import replace
import json
from revon.computer_use import register_computer_tools, TargetCancellation
from revon.execution import ActionExecutor, ActionRequest, create_execution_context
from revon.policy import ToolEffect
from revon.tools import ToolRegistry, ToolSpec, ToolResult
from phase_client import VisionClient
from revon.computer_coordinates import READ_NAMES, CLICK_NAME, TYPE_NAME, KEY_NAME, INPUT_NAMES, POINTER_ACTIONS, screenshot_content

class ComputerLane:
    def __init__(self, session, *, rows, selection, environment, consent, scope, cancel,
                 workspace, home, policy, clicks=False, approve=None, input_mode='foreground'):
        self.session, self.scope = session, scope
        self.cancel = TargetCancellation(cancel, session.revoked)
        self.rows, self.selection, self.environment = rows, selection, environment
        self.consent = consent
        if input_mode not in {'background', 'foreground'}:
            raise ValueError('未知的窗口输入方式。')
        self.background = input_mode == 'background'
        self.clicks, self.approve = clicks, approve
        self.last_frame = None
        registry = ToolRegistry()
        register_computer_tools(registry, background=self.background,
            approve=(lambda target, args: approve(target, args, None, self.cancel)) if approve else None)
        if not (self.background and clicks and approve):
            registry.unregister('computer_act')
        native_refs = {} if self.background else {
            'observation_id': {'type': 'string', 'description': 'Native observation ID, required with control_ref'},
            'control_ref': {'type': 'string', 'description': 'ref of the button or editable field in that observation'}}
        registry.register(ToolSpec('computer_screenshot',
            'Read the selected window locally. Use target or list_controls for visual recognition. '
            'To interact in background, use fresh computer_observe refs; pixels are read-only.' if self.background else
            'Capture the selected window locally; returns size, hash and a fresh observation_id, not pixels. '
            'For a classic control, pass the observation_id and control_ref from computer_observe to get '
            'its verified point locally, then use this screenshot\'s new observation_id and point for computer_click. '
            'On Windows the snapshot includes VisFunc accessibility labels/states. Use target to find a named '
            'control or visible text locally (UI Automation then OCR). Only a unique match returns point; '
            'role optionally restricts the search to a UI Automation type. Use computer_vision for other visuals. '
            'To only read public web pages, web_search/fetch_url are faster than driving a browser; '
            'use the window when the page needs a login or interaction.',
            self._screenshot, schema={'type': 'object', 'properties': {
                **native_refs,
                'list_controls': {'type': 'boolean', 'description': 'List the selected window’s local controls'},
                'target': {'type': 'string', 'maxLength': 256, 'description': 'Control label or visible text'},
                'role': {'type': 'string', 'maxLength': 40, 'description': 'Optional UIA role, e.g. button or edit'}},
                'additionalProperties': False}),
            trusted=True, effect=ToolEffect.READ, requires=('computer',))
        registry.register(ToolSpec('computer_vision',
            'Analyze the selected window using the consented vision model. Visual locations are read-only '
            'in background mode.' if self.background else
            'Analyze a fresh selected-window screenshot with the explicitly selected vision model. '
            'With target (e.g. "the search box"), returns an ORIGINAL screenshot pixel point plus an '
            'observation_id for computer_click. Use it again after input to verify the result. '
            'Requires user cloud consent. Plain description is not a coordinate or proof of an action.',
            self._vision, schema={'type': 'object', 'properties': {
                'question': {'type': 'string', 'maxLength': 4000, 'description': 'What to read or check in the window'},
                'target': {'type':'string','maxLength':1000, 'description': 'Element to locate for a click'}},
                'required': ['question'], 'additionalProperties': False}),
            trusted=True, effect=ToolEffect.READ, requires=('computer',))
        if clicks and approve is not None and not self.background:
            observation = {'type':'string','description':'observation_id from the latest screenshot, vision or input result'}
            reason = {'type':'string','maxLength':500,'description':'Short reason shown to the user in the approval prompt'}
            registry.register(ToolSpec(CLICK_NAME,
                'Pointer input at a screenshot pixel of the selected window client area: click (default), '
                'double_click, right_click, or scroll_up/scroll_down there (amount = wheel notches). '
                'Each call needs user approval; changed/expired screenshots reject. Right-click menus are '
                'separate windows that screenshots do not show: choose items with computer_key. Returns '
                'a post-input screenshot with a new observation_id; changed does not prove success.',
                self._click, read_only=False, schema={'type':'object','properties':{
                    'observation_id':observation,
                    'x':{'type':'integer','description':'Original screenshot pixel x'},
                    'y':{'type':'integer','description':'Original screenshot pixel y'},
                    'action':{'type':'string','enum':list(POINTER_ACTIONS),'description':'Default click'},
                    'amount':{'type':'integer','minimum':1,'maximum':10,'description':'Scroll notches, only for scroll actions (default 3)'},
                    'reason':reason},
                    'required':['observation_id','x','y','reason'],'additionalProperties':False}),
                trusted=True,effect=ToolEffect.DELEGATED_WRITE,requires=('computer',))
            registry.register(ToolSpec(TYPE_NAME,
                'Type text into whatever has keyboard focus in the selected window (click the field first). '
                'Newline presses Enter and tab presses Tab, so they may submit forms. The user approves '
                'the exact text. Returns a post-input screenshot with a new observation_id.',
                self._type, read_only=False, schema={'type':'object','properties':{
                    'observation_id':observation,
                    'text':{'type':'string','maxLength':2000,'description':'Exact text to type, up to 2000 characters'},
                    'reason':reason},
                    'required':['observation_id','text','reason'],'additionalProperties':False}),
                trusted=True,effect=ToolEffect.DELEGATED_WRITE,requires=('computer',))
            registry.register(ToolSpec(KEY_NAME,
                'Press one key or shortcut in the selected window, e.g. enter, esc, tab, down, ctrl+s, '
                'ctrl+a, shift+tab, alt+d, f5. In a browser: ctrl+l focuses the address bar (then '
                'computer_type the URL and press enter), ctrl+f finds on page, f5 reloads, alt+left goes '
                'back, ctrl+t opens a tab, ctrl+tab switches tabs, pagedown/space scroll. '
                'Not for text (use computer_type). Window switching/closing '
                'keys (alt+tab, alt+f4, ctrl+w, Win) are refused. Needs user approval; returns a '
                'post-input screenshot with a new observation_id.',
                self._key, read_only=False, schema={'type':'object','properties':{
                    'observation_id':observation,
                    'key':{'type':'string','maxLength':40,'description':'Key name or modifier+key combination'},
                    'repeat':{'type':'integer','minimum':1,'maximum':20,'description':'Press count, e.g. down x3 (default 1)'},
                    'reason':reason},
                    'required':['observation_id','key','reason'],'additionalProperties':False}),
                trusted=True,effect=ToolEffect.DELEGATED_WRITE,requires=('computer',))
        # Selection is an explicit read grant. Existing denies/asks/sandbox remain authoritative.
        policy = replace(policy, allow=tuple(policy.allow) + tuple(READ_NAMES))
        self.ctx = create_execution_context(home=home, workspace=workspace, policy=policy,
            registry=registry, cancellation=self.cancel, secret_scope=scope,
            capabilities={'computer': session}, agent='work-computer')
        self.executor = ActionExecutor()

    def check(self):
        if self.cancel.is_set():
            raise PermissionError('窗口授权已撤销或请求已取消，请重新选择目标')

    def capture(self, control=None, inspect=None, *, observe=False):
        self.check()
        if self.background and control:
            raise ValueError('后台模式请使用 computer_observe 和 computer_act 操作控件。')
        with self.session.operation(self.cancel):
            element = self.session.observed_control(self.scope, control['observation_id'], control['control_ref']) if control else None
            driver = self.session.driver
            if inspect is not None:
                if not hasattr(driver, 'inspect_screenshot'):
                    raise ValueError('当前窗口驱动不支持 VisFunc 本地识别。')
                frame = driver.inspect_screenshot(inspect, cancelled=self.cancel)
            elif observe and hasattr(driver, 'observed_screenshot'):
                frame = driver.observed_screenshot(cancelled=self.cancel)
            else:
                frame = driver.screenshot(cancelled=self.cancel)
            if element is not None:
                frame['point'] = self.session.driver.locate_control(frame, element)
            frame = self.session.visual.remember(frame,self.scope)
        self.check()
        return frame

    def _click(self, args, ctx):
        return self._input(self.session.visual.click, args, ctx)

    def _type(self, args, ctx):
        return self._input(self.session.visual.type_text, args, ctx)

    def _key(self, args, ctx):
        return self._input(self.session.visual.press_key, args, ctx)

    def _input(self, method, args, ctx):
        result, frame = method(args,ctx,self.approve)
        if frame is not None: self.last_frame=frame
        return result

    def _screenshot(self, args, ctx):
        keys = set(args)
        if not args:
            frame = self.capture(observe=True)
        elif keys == {'observation_id', 'control_ref'} and all(isinstance(v, str) and v for v in args.values()):
            frame = self.capture(control=args)
        elif keys == {'list_controls'} and args['list_controls'] is True:
            frame = self.capture(inspect=args)
        elif (keys in ({'target'}, {'target', 'role'}) and all(isinstance(v, str) and v.strip()
              and '\0' not in v for v in args.values()) and len(args['target']) <= 256 and len(args.get('role','')) <= 40):
            frame = self.capture(inspect=args)
        else:
            raise ValueError('请使用控件观察 ID 与 ref、list_controls=true，或目标名称和可选角色。')
        self.last_frame = frame
        return ToolResult(True, screenshot_content(frame, ctx))

    def _vision(self, args, ctx):
        if set(args) not in ({'question'},{'question','target'}): raise ValueError('视觉工具接受 question 和可选 target')
        if not isinstance(args['question'], str) or not args['question'].strip() or len(args['question']) > 4000:
            raise ValueError('视觉问题须为 1–4000 字符')
        if self.consent is not True: raise PermissionError('未授权向视觉模型发送截图')
        if 'target' in args and (not isinstance(args['target'],str) or not args['target'].strip() or len(args['target'])>1000):
            raise ValueError('定位目标须为 1–1000 字符')
        client = VisionClient(self.rows, self.selection, self.environment, self.scope, self.cancel)
        frame = self.capture()
        self.check()  # Never upload a capture from a revoked target.
        result = (client.locate(frame,args['target'],consent=True) if 'target' in args else
                  client.analyze(frame, args['question'], consent=True))
        self.check()
        self.last_frame = {**frame, 'point': result.get('point')}
        return ToolResult(True, json.dumps({**result,
            'image': {k:v for k,v in frame.items() if k != 'png'},
            'evidence': 'untrusted_visual_observation'}, ensure_ascii=False))

    def schemas(self):
        return [{'type': 'function', 'function': {'name': spec.name, 'description': spec.description,
                'parameters': spec.schema}} for spec in self.ctx.registry.available_specs(self.ctx.tool_context())
                if spec.name != 'computer_vision' or self.consent]

    def call(self, name, args):
        return self.executor.execute(self.ctx, ActionRequest(name, args)).as_dict()


