# -*- coding: utf-8 -*-
"""Planet evolving 交付客户端 v0.3 — 后端核心。

v0.2: 流式推送 / run 取消 / 文件浏览器 / 诊断
v0.3: 功能开关折叠面板 / bundle 模式切换 / 策略配置暴露 / run 参数面板

接口:
    GET  /api/models        模型目录 + key 状态
    GET  /api/config        当前配置快照 (policy/loop/model/thinking)
    GET  /api/bundles       可用 bundle 模式列表
    GET  /api/run           最近 run 列表
    GET  /api/run/<id>      run 详情
    GET  /api/run/<id>/events  SSE 流式
    POST /api/run           提交任务 (新增 bundle/strategy/thinking/policy 参数)
    POST /api/run/<id>/cancel  取消
    GET  /api/files         文件浏览器
    GET  /api/file?path=    读文件
    GET  /api/diag          环境诊断
    GET  /api/health        存活
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forge.client_runs import RunStore, RUN_ID_RE, TERMINAL_STATES
from forge.execution import RunRequest
from forge.secrets import redact

FORGE_HOME = Path.home() / ".forge"
# W3 P0-02：与内核/GUI/VSCode 桥共用同一凭据存储 ~/.forge/secrets.json
# （此前读 keys.json——内核没有任何代码读它，等价于凭据链路断裂）。
# 命名约定与 vscode_bridge._load_credentials 一致：name → FORGE_<NAME>_KEY。
SECRETS_FILE = FORGE_HOME / "secrets.json"

# ── Constants ──────────────────────────────────────────────────────────────
MAX_PAYLOAD_BYTES    = 1_048_576   # 1 MB POST body cap
STORE_CLEAN_INTERVAL = 600         # seconds between cleanup runs
SSE_DEADLINE_SECS    = 600
SSE_POLL_INTERVAL    = 0.8        # seconds between SSE polls

ROOT.mkdir(parents=True, exist_ok=True)
(ROOT / "workspace").mkdir(parents=True, exist_ok=True)


def load_keys() -> dict[str, str]:
    """读内核凭据存储 secrets.json，返回 {FORGE_<NAME>_KEY: value}。

    读取走内核既有安全机制（protect_store_path + SecretScope.reference +
    有界读取 + 形状校验）；TRAE P0-2 的约束保留——绝不写入 os.environ
    （shell_exec 子进程会继承环境变量，明文密钥可被 `env | grep` 读出），
    值只在 run_forge 组装 Provider 配置时内存内替换。API 响应只出现布尔。
    """
    from forge.secrets import SecretScope, protect_store_path

    keys: dict[str, str] = {}
    if not SECRETS_FILE.is_file():
        return keys
    try:
        protect_store_path(SECRETS_FILE)
        raw = SECRETS_FILE.read_bytes()[:1024 * 1024 + 1]
        if len(raw) > 1024 * 1024:
            return keys
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            return keys
        scope = SecretScope()
        try:
            for name, value in data.items():
                if (isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name)
                        and isinstance(value, str) and value):
                    scope.reference(value)
                    keys[f"FORGE_{name.upper().replace('-', '_')}_KEY"] = value
        finally:
            scope.close()
    except (json.JSONDecodeError, OSError, ValueError):
        return keys
    return keys


CATALOG: list[dict] = [
    {"id": "deepseek", "label": "DeepSeek v4 Flash", "model": "deepseek-flash",
     "env": "FORGE_DEEPSEEK_KEY", "tier": "economy",
     "note": "默认主力档，快且便宜，简单任务首选"},
    {"id": "deepseek", "label": "DeepSeek v4 Pro", "model": "deepseek-v4-pro",
     "env": "FORGE_DEEPSEEK_KEY", "tier": "standard",
     "note": "长推演、复杂推理，需要 32k+ token 预算"},
    {"id": "mimo", "label": "Claude Sonnet 5", "model": "claude-sonnet-5",
     "env": "FORGE_MIMO_KEY", "tier": "standard",
     "note": "中端主力档，代码与综合能力均衡"},
    {"id": "review", "label": "Claude Opus 5", "model": "claude-opus-5",
     "env": "FORGE_CLAUDE_KEY", "tier": "premium",
     "note": "高端裁决档，仅用于审查或复杂决策"},
    {"id": "glm", "label": "GLM 5.3", "model": "glm-5.3",
     "env": "FORGE_GLM_KEY", "tier": "standard",
     "note": "智谱官方接口，中文表现好"},
    {"id": "qwen", "label": "Qwen 3.8 Max", "model": "qwen3.8-max",
     "env": "FORGE_QWEN_KEY", "tier": "standard",
     "note": "阿里 max 档，超长上下文"},
]

STRATEGIES = ("economy", "balanced", "premium")
THINKING_MODES = ("off", "smart", "on")
# TRAE 审查 P0-1：与内核 forge.policy 的合法模式对齐——自定义枚举
# （full-auto/readonly 不存在于内核）会静默降级为 default（可写基线），
# 用户选"readonly"实际拿到写权限，语义误导。
POLICY_MODES = ("default", "acceptEdits", "read-only", "bypassPermissions")

PROVIDER_ROWS = {
    "deepseek": {"id": "deepseek", "name": "provider:deepseek", "config": {
        "wire": "openai", "baseURL": "https://api.deepseek.com",
        "apiKey": {"$expr": "get('env.FORGE_DEEPSEEK_KEY', '')"},
        "model": "deepseek-flash", "smallModel": "deepseek-flash"}},
    "mimo": {"id": "mimo", "name": "provider:mimo", "config": {
        "wire": "openai", "baseURL": "https://api.qnaigc.com",
        "apiKey": {"$expr": "get('env.FORGE_MIMO_KEY', '')"},
        "model": "claude-sonnet-5"}},
    "review": {"id": "review", "name": "provider:review", "config": {
        "wire": "openai", "baseURL": "https://api.qnaigc.com/v1",
        "apiKey": {"$expr": "get('env.FORGE_CLAUDE_KEY', '')"},
        "model": "claude-opus-5"}},
    "glm": {"id": "glm", "name": "provider:glm", "config": {
        "wire": "openai", "baseURL": "https://open.bigmodel.cn/api/coding/paas/v4",
        "apiKey": {"$expr": "get('env.FORGE_GLM_KEY', '')"},
        "model": "glm-5.3"}},
    "qwen": {"id": "qwen", "name": "provider:qwen", "config": {
        "wire": "openai", "baseURL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "apiKey": {"$expr": "get('env.FORGE_QWEN_KEY', '')"},
        "model": "qwen3.8-max"}},
}

# bundle 模式: 与 modes/ 目录对齐
BUNDLES = {
    "base": {"label": "默认模式", "desc": "只读，写操作需确认", "file": None},
    "coding": {"label": "编码模式", "desc": "工作区内直写，越界仍拦", "file": "bundles/modes/coding.json"},
}


def _resolved_provider_row(hit: dict) -> dict:
    """CATALOG 条目 → Provider 行，apiKey 的 $expr 在内存内解析为真实值。

    此前 Provider 只带 {"$expr": "get('env.FORGE_*_KEY', '')"}，而本进程
    从不向 os.environ 注入密钥（TRAE P0-2），表达式恒解析为空——链路断裂。
    现在：secrets.json 有对应键 → 直接替换为值（仅存在于本次 run 的内存
    Config，不落 env、不回 API）；没有 → 保留 $expr（照旧解析为空，诚实
    反映“未配置密钥”）。别名兜底：CATALOG 的 env 名（如 FORGE_CLAUDE_KEY）
    与按 provider id 推导的名字（如 FORGE_REVIEW_KEY）都会尝试。
    """
    import copy

    keys = load_keys()
    row = copy.deepcopy(PROVIDER_ROWS[hit["id"]])
    expr = row["config"].get("apiKey")
    if not (isinstance(expr, dict) and "$expr" in expr):
        return row
    env_names = [hit.get("env", ""),
                 f"FORGE_{hit['id'].upper().replace('-', '_')}_KEY"]
    for env_name in env_names:
        value = keys.get(env_name)
        if value:
            row["config"]["apiKey"] = value
            break
    return row


# Imports/programmatic embedders are side-effect free; the CLI opts into durable history.
STORE = RunStore()

DELIVERY_DIRS = ("outputs", "deliveries", "reports", "dist", "build")
DELIVERY_EXT = {".md", ".html", ".txt", ".py", ".js", ".ts", ".json", ".csv",
                ".png", ".jpg", ".svg", ".pdf", ".docx", ".xlsx", ".pptx", ".zip"}
FENCE_RE = re.compile(r"```(\w+)?\n(.*?)```", re.S)
# 7fe13ed 误删唯一定义（当作重复常量），导致 import 期 NameError，客户端无法启动
TEXT_FILE_MAX = 500_000
TREE_DEPTH = 3

def scan_delivery(workspace: Path, since: float, report_text: str) -> list[dict]:
    items: list[dict] = []
    for sub in DELIVERY_DIRS:
        d = workspace / sub
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if (p.is_file() and p.stat().st_mtime >= since
                    and p.suffix.lower() in DELIVERY_EXT):
                items.append({
                    "kind": "file",
                    "path": str(p.relative_to(workspace)).replace("\\", "/"),
                    "abs": str(p),
                    "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime,
                    "ext": p.suffix.lower(),
                })
    for m in FENCE_RE.finditer(report_text or ""):
        lang = (m.group(1) or "text").lower()
        body = m.group(2)
        if len(body.strip()) > 40:
            items.append({"kind": "code", "lang": lang, "preview": body.strip()[:500]})
    items.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return items


def list_files(workspace: Path, subdir: str = "", depth: int = TREE_DEPTH) -> list[dict]:
    target = (workspace / subdir).resolve()
    if not target.is_relative_to(workspace.resolve()):
        return [{"error": "path traversal blocked"}]
    if not target.is_dir():
        return [{"error": "not a directory"}]
    result: list[dict] = []
    _walk_files(target, workspace, result, depth, 0)
    return result


def _walk_files(path: Path, root: Path, out: list[dict], max_depth: int, depth: int) -> None:
    if depth > max_depth:
        return
    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return
    for p in entries:
        if p.name.startswith(".") or p.name in ("__pycache__", "node_modules", ".git"):
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        if p.is_dir():
            out.append({"type": "dir", "name": p.name, "path": rel})
            _walk_files(p, root, out, max_depth, depth + 1)
        elif p.is_file():
            if p.stat().st_size < 10_000_000:
                out.append({"type": "file", "name": p.name, "path": rel,
                            "size": p.stat().st_size, "ext": p.suffix.lower()})


def read_file(workspace: Path, rel_path: str) -> dict:
    target = (workspace / rel_path).resolve()
    if not target.is_relative_to(workspace.resolve()):
        return {"error": "path traversal blocked"}
    if not target.is_file():
        return {"error": "file not found"}
    if target.stat().st_size > TEXT_FILE_MAX:
        return {"error": f"file too large ({target.stat().st_size} bytes)"}
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"binary": True, "size": target.stat().st_size}
    return {"text": text, "size": target.stat().st_size, "ext": target.suffix.lower(),
            "name": target.name, "path": rel_path}


def get_config_snapshot() -> dict:
    """读取当前 base bundle 的配置，暴露给前端做开关面板。"""
    try:
        from forge.cli import _compose
        ns = argparse.Namespace(
            home=str(FORGE_HOME), workspace=str(ROOT / "workspace"),
            bundle=None, overlay=[], no_user_layer=False,
            profile=None, i_know=None, coding=False)
        cfg = _compose(ns)
        policy_row = cfg.row("policy")
        loop_row = cfg.row("loop")
        model_row = cfg.row("model")
        think_row = cfg.row("thinking")
        tools_row = cfg.row("tools")
        return {
            "policy": dict(policy_row.config) if policy_row else {},
            "loop": dict(loop_row.config) if loop_row else {},
            "model": dict(model_row.config) if model_row else {},
            "thinking": dict(think_row.config) if think_row else {},
            "tools": dict(tools_row.config) if tools_row else {},
        }
    except Exception:
        import traceback
        traceback.print_exc()
        return {"error": "config load failed"}


def validate_run_payload(payload):
    """Validate before creating a task or composing any permission-bearing config."""
    if not isinstance(payload, dict):
        raise ValueError('request must be an object')
    task = payload.get('task')
    if not isinstance(task, str) or not task.strip():
        raise ValueError('task required')
    choices = {'model': tuple(m['label'] for m in CATALOG), 'strategy': STRATEGIES,
               'thinking': THINKING_MODES, 'bundle': tuple(BUNDLES), 'policy_mode': POLICY_MODES}
    for key, values in choices.items():
        value = payload.get(key)
        if value is not None and value not in values:
            raise ValueError('invalid ' + key)
    for key, maximum in [('max_steps', 1000), ('max_depth', 16), ('spawn_budget', 1000)]:
        value = payload.get(key)
        if value is not None and (type(value) is not int or not 1 <= value <= maximum):
            raise ValueError('invalid ' + key)
    if payload.get('workspace') is not None and not isinstance(payload['workspace'], str):
        raise ValueError('invalid workspace')
    return {**payload, 'task': task.strip()}


def compose_run_config(workspace, model_label=None, strategy=None, thinking=None,
                       bundle=None, policy_mode=None):
    from forge.cli import _compose
    ns = argparse.Namespace(home=str(FORGE_HOME), workspace=str(workspace), bundle=None,
        overlay=[], no_user_layer=False, profile=None, i_know=True, coding=(bundle == 'coding'))
    # UI bundle names are selectors, not arbitrary paths supplied to _compose.
    cfg = _compose(ns)
    patches = []
    old = cfg.row('model')
    model = dict(old.config) if old else {}
    if model_label:
        hit = next(m for m in CATALOG if m['label'] == model_label)
        row = _resolved_provider_row(hit)
        row['config']['model'] = hit['model']
        patches.append(row)
        model['primary'] = [hit['id'], hit['model']]
    if strategy:
        model['routing'] = {**(model.get('routing') or {}), 'strategy': strategy}
    if model_label or strategy:
        patches.append({'id': 'model', 'name': 'model:router', 'config': model})
    if thinking:
        old = cfg.row('thinking')
        patches.append({'id': 'thinking', 'name': 'thinking:mode',
                        'config': {**(dict(old.config) if old else {}), 'mode': thinking}})
    if policy_mode:
        old = cfg.row('policy')
        patches.append({'id': 'policy', 'name': 'policy:core',
                        'config': {**(dict(old.config) if old else {}), 'mode': policy_mode}})
    if patches:
        cfg.apply_patch(patches, label='client:config-select')
    return cfg


def run_forge(run_id: str, task: str, model_label: str | None,
              strategy: str | None, thinking: str | None,
              bundle: str | None, policy_mode: str | None,
              max_steps: int | None, max_depth: int | None,
              spawn_budget: int | None,
              workspace: Path, *, store=None) -> None:
    from forge.loop import LoopLimits, build_agent
    from forge.model import ModelRouter
    from forge.routing import SmartRouter
    from forge.phase_models import phase_router

    store = store if store is not None else STORE
    cancel = store.claim(run_id)
    if cancel is None:
        return  # A completed, detached or already-running task cannot be replayed.
    t0 = time.time()
    agent = None
    try:
        validate_run_payload({'task': task, 'model': model_label, 'strategy': strategy,
            'thinking': thinking, 'bundle': bundle, 'policy_mode': policy_mode,
            'max_steps': max_steps, 'max_depth': max_depth, 'spawn_budget': spawn_budget})
        if cancel.is_set():
            store.transition(run_id, state='cancelled', outcome='cancelled',
                execution_state='not_started', error_code='cancelled', cancelled=True, finished=time.time())
            return
        store.transition(run_id, state='running', started=t0, execution_state='running',
            strategy=strategy or '', thinking=thinking or '', bundle=bundle or 'base',
            policy_mode=policy_mode or '', workspace=str(workspace))
        cfg = compose_run_config(workspace, model_label, strategy, thinking, bundle, policy_mode)

        routing_block = cfg.get("model", "routing", None)
        router = (SmartRouter.from_config(cfg) if routing_block is not None
                  else ModelRouter.from_config(cfg))
        if model_label:
            # A named model wins over automatic routing, just as phase-model
            # selection does elsewhere. Preserve config, transport and secrets.
            router = phase_router(router, cfg.get('model', 'primary'), router)

        limits = LoopLimits(
            max_steps=max_steps or int(cfg.get("loop", "maxSteps", 12)),
            max_depth=max_depth or int(cfg.get("loop", "maxDepth", 2)),
            spawn_budget=spawn_budget or int(cfg.get("loop", "spawnBudget", 8)))

        def observe(event):
            store.push_event(run_id, event)
            if event.get('type') == 'tool_call':
                store.push_step(run_id, {**event, 'event_id': event['event_id'] + '-step',
                    'index': event.get('step'), 'note': '', 'result': event.get('result', '')})

        run_session = FORGE_HOME / "sessions" / f"s-{run_id}.jsonl"
        agent = build_agent(
            home=FORGE_HOME, workspace=workspace, config=cfg, router=router,
            expose=cfg.get("tools", "expose", None), limits=limits,
            session_path=run_session, observer=observe)
        agent.cancellation_event = cancel
        agent.child_observer = observe
        actual_model = model_label or "(auto routing)"
        store.update(run_id, model=actual_model, sessionPath=str(run_session),
                     routing_mode='explicit' if model_label else 'automatic')
        rec = store.get(run_id)
        request = RunRequest(run_id=run_id, task_id=rec['task_id'], entry='client')

        with agent.session:
            report = agent.run(task, run_request=request)

        result = report.as_result().as_dict()
        state = {'succeeded': 'done', 'cancelled': 'cancelled'}.get(result['outcome'], 'error')
        delivery = scan_delivery(workspace, t0, report.text)
        store.transition(run_id, **result, state=state, finished=time.time(),
            usage=report.usage, delivery=delivery, stopped=report.stopped,
            cancelled=result['outcome'] == 'cancelled',
            requires_inspection=result['outcome'] == 'unknown',
            error='' if state == 'done' else result['error_code'])
    except BaseException as exc:
        tb = traceback.format_exc()[-2000:]
        # A thrown handler or process interruption can leave side effects.
        # Inspect durable actions; do not convert an exception into safe retry.
        unknown = agent is not None
        store.transition(run_id, state='error', finished=time.time(),
            outcome='unknown' if unknown else 'failed',
            execution_state='unknown' if unknown else 'not_started',
            error_code='execution_interrupted' if unknown else 'configuration_error',
            requires_inspection=unknown, error=f'{type(exc).__name__}: {exc}', traceback=tb)
        if not isinstance(exc, Exception):
            raise
    finally:
        if agent is not None:
            agent.session.close()
            agent.secret_scope.close()
        store.release(run_id)


INDEX_PAGE = ROOT / "client" / "index.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "PlanetEvolving/0.3"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(redact(payload), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, run_id: str, after=0) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header('connection', 'close')
        self.close_connection = True
        self.end_headers()
        deadline = time.monotonic() + SSE_DEADLINE_SECS
        try:
            while time.monotonic() < deadline:
                rec = self.server.store.get(run_id)
                if not rec:
                    self._sse_event({"type": "error", "msg": "run not found"})
                    break
                page = self.server.store.event_page(run_id, after)
                if page['reset_required']:
                    self._sse_event({'type': 'reset', 'run_id': run_id,
                                     'message': 'Reload the task snapshot before consuming retained events.'})
                for event in page['events']:
                    self._sse_event(event)
                after = page['next_after']
                if page['has_more']:
                    continue
                # Read state after taking the event snapshot. A terminal update
                # racing this page is drained on the next iteration, not lost.
                latest = self.server.store.get(run_id)
                if latest and latest['cursor'] > rec['cursor']:
                    continue
                if rec['state'] in TERMINAL_STATES:
                    if rec.get('recovered'):
                        self._sse_event({'type': 'state', **{k: v for k, v in rec.items() if k != 'events'}})
                    self._sse_event({"type": "terminal", "state": rec["state"],
                                     'run_id': run_id, 'outcome': rec['outcome']})
                    break
                self.wfile.write(b': heartbeat\n\n')
                self.wfile.flush()
                time.sleep(SSE_POLL_INTERVAL)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _sse_event(self, data: dict) -> None:
        payload = json.dumps(redact(data), ensure_ascii=False, default=str)
        identity = f"id: {data['cursor']}\n" if 'cursor' in data else ''
        self.wfile.write(f"{identity}data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()


    @staticmethod
    def _extract_run_id(raw_path: str) -> str | None:
        parts = raw_path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "run":
            candidate = parts[2]
            if RUN_ID_RE.match(candidate):
                return candidate
        return None

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = dict(urllib.parse.parse_qsl(parsed.query))

        if path == "/":
            if INDEX_PAGE.is_file():
                self._send_html(INDEX_PAGE.read_text(encoding="utf-8"))
            else:
                self._send_json({"error": "client/index.html missing"}, 500)
        elif path == "/api/models":
            # W3 P0-02：变量名修正（此前 key_keys_status 定义、key_status 使用 → NameError，
            # 且 boot() 首请求即 /api/models，整个 Web UI 启动即 500）。只回布尔，不回键值。
            key_status = {k: bool(v) for k, v in load_keys().items()}
            models = [{**m, "keyReady": key_status.get(m["env"], False)}
                      for m in CATALOG]
            self._send_json({"models": models,
                             "strategies": list(STRATEGIES),
                             "thinking": list(THINKING_MODES),
                             "policies": list(POLICY_MODES)})
        elif path == "/api/config":
            self._send_json(get_config_snapshot())
        elif path == "/api/bundles":
            self._send_json(BUNDLES)
        elif path == "/api/run":
            self._send_json({"runs": self.server.store.list_recent(50)})
        elif path.startswith("/api/run/") and path.endswith("/events"):
            run_id = self._extract_run_id(path)
            if not run_id:
                self._send_json({"error": "invalid run id"}, 400)
                return
            if self.server.store.get(run_id) is None:
                self._send_json({'error': 'run not found'}, 404)
                return
            try:
                after = int(self.headers.get('Last-Event-ID', qs.get('after', '0')))
                if after < 0:
                    raise ValueError()
            except ValueError:
                self._send_json({'error': 'invalid event cursor'}, 400)
                return
            if qs.get('format') == 'json':
                self._send_json(self.server.store.event_page(run_id, after))
            else:
                self._send_sse(run_id, after)
        elif path.startswith("/api/run/"):
            run_id = self._extract_run_id(path)
            if not run_id:
                self._send_json({"error": "invalid run id"}, 400)
                return
            rec = self.server.store.get(run_id)
            self._send_json(rec if rec else {"error": "run not found"},
                           200 if rec else 404)
        elif path == "/api/files":
            workspace = self.server.workspace
            workspace.mkdir(parents=True, exist_ok=True)
            files = list_files(workspace, subdir=qs.get("path", ""))
            self._send_json({"files": files, "root": str(workspace)})
        elif path == "/api/file":
            rel = qs.get("path", "")
            if not rel:
                self._send_json({"error": "path required"}, 400)
                return
            workspace = self.server.workspace
            self._send_json(read_file(workspace, rel))
        elif path == "/api/diag":
            key_status = {k: bool(v) for k, v in load_keys().items()}
            self._send_json({
                "forge_home": str(FORGE_HOME),
                "keys_file": str(SECRETS_FILE),
                "keys_exists": SECRETS_FILE.is_file(),
                "env_status": key_status,
                "forge_version": "0.3",
                "workspace": str(self.server.workspace)})
        elif path == "/api/health":
            self._send_json({"ok": True, "version": "0.3", "home": str(FORGE_HOME)})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/run":
            self._handle_start_run()
        elif path.startswith("/api/run/") and path.endswith("/cancel"):
            rid = self._extract_run_id(path)
            if not rid:
                self._send_json({"error": "invalid run id"}, 400)
                return
            self._handle_cancel(rid)
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_start_run(self) -> None:
        try:
            length = int(self.headers.get('content-length', 0))
            if length > MAX_PAYLOAD_BYTES:
                self._send_json({'error': 'request too large'}, 413)
                return
            if length <= 0:
                raise ValueError('body required')
            payload = validate_run_payload(json.loads(self.rfile.read(length).decode('utf-8')))
            raw_ws = payload.get('workspace') or str(self.server.workspace)
            workspace = Path(raw_ws).resolve()
            if not workspace.is_relative_to(self.server.workspace):
                raise ValueError('workspace outside configured project')
            workspace.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as exc:
            self._send_json({'error': str(exc)}, 400)
            return
        try:
            rec = self.server.store.create(payload['task'])
        except OSError:
            self._send_json({'error': 'could not persist task'}, 503)
            return
        try:
            self.server.submit(rec['id'],
                  (rec["id"], payload['task'], payload.get("model"),
                  payload.get("strategy"), payload.get("thinking"),
                  payload.get("bundle"), payload.get("policy_mode"),
                  payload.get("max_steps"), payload.get("max_depth"),
                   payload.get("spawn_budget"), workspace))
        except RuntimeError:
            self.server.store.transition(rec['id'], state='error', outcome='failed',
                execution_state='not_started', error_code='worker_unavailable')
            self._send_json({'error': 'worker unavailable'}, 503)
            return
        self._send_json({'run_id': rec['id'], 'task_id': rec['task_id'], 'protocol_version': 1}, 202)

    def _handle_cancel(self, run_id: str) -> None:
        try:
            length = int(self.headers.get('content-length', 0))
            if not 0 <= length <= MAX_PAYLOAD_BYTES:
                self._send_json({'error': 'invalid request size'}, 400)
                return
            if length:
                self.rfile.read(length)  # Drain before closing, including POST {}.
        except ValueError:
            self._send_json({'error': 'invalid request size'}, 400)
            return
        try:
            self.server.store.request_cancel(run_id)
        except KeyError:
            self._send_json({"error": "run not found"}, 404)
            return
        except ValueError as exc:
            self._send_json({'error': str(exc)}, 409)
            return
        self._send_json({'ok': True, 'state': 'cancelling', 'cancel_requested': True, 'run_id': run_id})


class ClientServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler=Handler, *, workspace=None, store=None):
        self.workspace = Path(workspace or ROOT / 'workspace').resolve()
        self.store = store if store is not None else STORE
        self._workers = {}
        self._worker_lock = threading.Lock()
        self._closing = False
        super().__init__(address, handler)

    def submit(self, rid, args):
        def invoke():
            try:
                run_forge(*args, store=self.store)
            finally:
                with self._worker_lock:
                    self._workers.pop(threading.current_thread(), None)
        with self._worker_lock:
            if self._closing:
                raise RuntimeError('server is closing')
            worker = threading.Thread(target=invoke, daemon=True, name='forge-client-' + rid[:8])
            self._workers[worker] = rid
            try:
                worker.start()
            except BaseException:
                del self._workers[worker]
                raise

    def server_close(self):
        super().server_close()
        with self._worker_lock:
            self._closing = True
            workers = list(self._workers.items())
        for worker, rid in workers:
            try:
                self.store.request_cancel(rid)
            except (KeyError, ValueError, OSError):
                pass
        deadline = time.monotonic() + 5
        for worker, _ in workers:
            worker.join(max(0, deadline - time.monotonic()))


def main(argv: list[str] | None = None) -> int:
    global STORE
    ap = argparse.ArgumentParser(description="Planet evolving delivery client")
    ap.add_argument("--port", type=int, default=7712)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--workspace", default=str(ROOT / "workspace"))
    args = ap.parse_args(argv)
    STORE = RunStore(FORGE_HOME / 'sessions' / 'client')
    srv = ClientServer((args.host, args.port), Handler, workspace=args.workspace)
    print(f"[planet-evolving] v0.3 on http://{args.host}:{args.port}"
          f"  (home={FORGE_HOME})")
    # Periodic cleanup of stale runs and cancel flags
    import sched
    _scheduler = sched.scheduler(time.time, time.sleep)
    def _periodic_cleanup():
        STORE.cleanup()
        _scheduler.enter(STORE_CLEAN_INTERVAL, 1, _periodic_cleanup)
    _scheduler.enter(600, 1, _periodic_cleanup)
    threading.Thread(target=_scheduler.run, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        STORE.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
