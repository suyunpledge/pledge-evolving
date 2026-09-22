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
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FORGE_HOME = Path.home() / ".forge"
KEYS_FILE = FORGE_HOME / "keys.json"
_CANCEL_FLAGS: dict[str, bool] = {}

# ── Constants ──────────────────────────────────────────────────────────────
MAX_PAYLOAD_BYTES    = 1_048_576   # 1 MB POST body cap
STORE_CLEAN_INTERVAL = 600         # seconds between cleanup runs
SSE_DEADLINE_SECS    = 600
SSE_POLL_INTERVAL    = 0.8        # seconds between SSE polls
RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$")  # hex-12 run IDs from uuid4

ROOT.mkdir(parents=True, exist_ok=True)
(ROOT / "workspace").mkdir(parents=True, exist_ok=True)


def load_keys_into_env() -> dict[str, bool]:
    status: dict[str, bool] = {}
    if KEYS_FILE.is_file():
        try:
            data = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
            for env_name, value in data.items():
                if isinstance(value, str) and value:
                    os.environ.setdefault(env_name, value)
                    status[env_name] = True
                else:
                    status[env_name] = False
        except (json.JSONDecodeError, OSError):
            pass
    return status


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
POLICY_MODES = ("auto", "acceptEdits", "full-auto", "readonly")

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


class RunStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict] = {}

    def create(self, task: str) -> dict:
        run_id = uuid.uuid4().hex[:12]
        rec = {"id": run_id, "task": task, "state": "queued",
               "created": time.time(), "started": 0.0, "finished": 0.0,
               "steps": [], "text": "", "delivery": [], "usage": {},
               "error": "", "traceback": "", "model": "",
               "strategy": "", "thinking": "", "bundle": "",
               "policy_mode": "", "cancelled": False, "events": []}
        with self._lock:
            self._runs[run_id] = rec
        return rec

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            return self._runs.get(run_id)

    def list_recent(self, n: int = 50) -> list[dict]:
        with self._lock:
            runs = sorted(self._runs.values(),
                          key=lambda r: r["created"], reverse=True)
        return runs[:n]

    def update(self, run_id: str, **kw) -> None:
        with self._lock:
            rec = self._runs.get(run_id)
            if rec:
                rec.update(kw)

    def push_step(self, run_id: str, step: dict) -> None:
        with self._lock:
            rec = self._runs.get(run_id)
            if rec:
                rec["steps"].append(step)
                rec["events"].append({"type": "step", "ts": time.time(), **step})

    def push_event(self, run_id: str, event: dict) -> None:
        with self._lock:
            rec = self._runs.get(run_id)
            if rec:
                evts = rec["events"]
                if len(evts) > 5000:
                    evts[:] = evts[-2500:]
                evts.append({"ts": time.time(), **event})

    def cleanup(self, max_age: float = 7200) -> None:
        """Remove runs older than max_age seconds (default 2h)."""
        cutoff = time.time() - max_age
        with self._lock:
            stale = [rid for rid, r in self._runs.items()
                     if r["created"] < cutoff and r["state"] in
                     ("done", "error", "cancelled")]
            for rid in stale:
                del self._runs[rid]
                _CANCEL_FLAGS.pop(rid, None)


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


def run_forge(run_id: str, task: str, model_label: str | None,
              strategy: str | None, thinking: str | None,
              bundle: str | None, policy_mode: str | None,
              max_steps: int | None, max_depth: int | None,
              spawn_budget: int | None,
              workspace: Path) -> None:
    from forge.cli import _compose
    from forge.loop import LoopLimits, build_agent
    from forge.model import ModelRouter
    from forge.routing import SmartRouter

    STORE.update(run_id, state="running", started=time.time(),
                 strategy=strategy or "", thinking=thinking or "",
                 bundle=bundle or "base", policy_mode=policy_mode or "")
    STORE.push_event(run_id, {"type": "state", "state": "running"})
    t0 = time.time()
    try:
        ns = argparse.Namespace(
            home=str(FORGE_HOME), workspace=str(workspace), bundle=bundle,
            overlay=[], no_user_layer=False, profile="balanced",
            i_know=True, coding=(bundle == "coding"))
        cfg = _compose(ns)

        patch_rows: list[dict] = []
        if model_label:
            hit = next((m for m in CATALOG if m["label"] == model_label), None)
            if hit:
                patch_rows.append(PROVIDER_ROWS[hit["id"]])
                patch_rows.append({"id": "model", "name": "model:router",
                                   "config": {"primary": [hit["id"], hit["model"]]}})
        if strategy:
            old = cfg.row("model")
            merged = dict(old.config) if old is not None else {}
            rc = dict(merged.get("routing") or {})
            rc["strategy"] = strategy
            merged["routing"] = rc
            patch_rows.append({"id": "model", "name": "model:router",
                               "config": merged})
        if policy_mode:
            old_p = cfg.row("policy")
            pc = dict(old_p.config) if old_p else {}
            pc["mode"] = policy_mode
            patch_rows.append({"id": "policy", "name": "policy:core",
                               "config": pc})
        if patch_rows:
            cfg.apply_patch(patch_rows, label="client:config-select")

        routing_block = cfg.get("model", "routing", None)
        router = (SmartRouter.from_config(cfg) if routing_block is not None
                  else ModelRouter.from_config(cfg))

        limits = LoopLimits(
            max_steps=max_steps or int(cfg.get("loop", "maxSteps", 12)),
            max_depth=max_depth or int(cfg.get("loop", "maxDepth", 2)),
            spawn_budget=spawn_budget or int(cfg.get("loop", "spawnBudget", 8)))

        agent = build_agent(
            home=FORGE_HOME, workspace=workspace, config=cfg, router=router,
            expose=cfg.get("tools", "expose", None), limits=limits)
        actual_model = model_label or "(auto routing)"
        STORE.update(run_id, model=actual_model)
        STORE.push_event(run_id, {"type": "state", "state": "running",
                                   "model": actual_model})

        if _CANCEL_FLAGS.get(run_id):
            STORE.update(run_id, state="cancelled", finished=time.time())
            STORE.push_event(run_id, {"type": "state", "state": "cancelled"})
            return

        with agent.session:
            report = agent.run(task)

        if _CANCEL_FLAGS.get(run_id):
            STORE.update(run_id, state="cancelled", finished=time.time())
            STORE.push_event(run_id, {"type": "state", "state": "cancelled"})
            return

        for s in report.steps:
            STORE.push_step(run_id, {
                "index": s.index, "tool": s.tool, "decision": s.decision,
                "note": (s.note or "")[:200],
                "result": (s.result or "")[:400]})

        delivery = scan_delivery(workspace, t0, report.text)
        STORE.update(run_id, state="done", finished=time.time(),
                     text=report.text, usage=report.usage,
                     delivery=delivery, stopped=report.stopped)
        STORE.push_event(run_id, {"type": "state", "state": "done",
                                   "text": report.text, "usage": report.usage,
                                   "delivery": delivery})

    except Exception as exc:
        tb = traceback.format_exc()[-2000:]
        STORE.update(run_id, state="error", finished=time.time(),
                     error=f"{type(exc).__name__}: {exc}", traceback=tb)
        STORE.push_event(run_id, {"type": "state", "state": "error",
                                   "error": str(exc), "traceback": tb})


INDEX_PAGE = ROOT / "client" / "index.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "PlanetEvolving/0.3"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
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

    def _send_sse(self, run_id: str) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.end_headers()
        idx = 0
        deadline = time.time() + SSE_DEADLINE_SECS
        try:
            while time.time() < deadline:
                rec = STORE.get(run_id)
                if not rec:
                    self._sse_event({"type": "error", "msg": "run not found"})
                    break
                evts = rec["events"]
                while idx < len(evts):
                    self._sse_event(evts[idx])
                    idx += 1
                if rec["state"] in ("done", "error", "cancelled"):
                    self._sse_event({"type": "terminal", "state": rec["state"]})
                    break
                time.sleep(SSE_POLL_INTERVAL)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse_event(self, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False, default=str)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
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
            key_status = load_keys_into_env()
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
            self._send_json({"runs": STORE.list_recent(50)})
        elif path.startswith("/api/run/") and path.endswith("/events"):
            run_id = self._extract_run_id(path)
            if not run_id:
                self._send_json({"error": "invalid run id"}, 400)
                return
            self._send_sse(run_id)
        elif path.startswith("/api/run/"):
            run_id = self._extract_run_id(path)
            if not run_id:
                self._send_json({"error": "invalid run id"}, 400)
                return
            rec = STORE.get(run_id)
            self._send_json(rec if rec else {"error": "run not found"},
                           200 if rec else 404)
        elif path == "/api/files":
            workspace = ROOT / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            files = list_files(workspace, subdir=qs.get("path", ""))
            self._send_json({"files": files, "root": str(workspace)})
        elif path == "/api/file":
            rel = qs.get("path", "")
            if not rel:
                self._send_json({"error": "path required"}, 400)
                return
            workspace = ROOT / "workspace"
            self._send_json(read_file(workspace, rel))
        elif path == "/api/diag":
            key_status = load_keys_into_env()
            self._send_json({
                "forge_home": str(FORGE_HOME),
                "keys_file": str(KEYS_FILE),
                "keys_exists": KEYS_FILE.is_file(),
                "env_status": key_status,
                "forge_version": "0.3",
                "workspace": str(ROOT / "workspace")})
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
            length = min(int(self.headers.get("content-length", 0)), MAX_PAYLOAD_BYTES)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "bad json"}, 400)
            return
        task = str(payload.get("task", "")).strip()
        if not task:
            self._send_json({"error": "task required"}, 400)
            return
        load_keys_into_env()
        rec = STORE.create(task)
        raw_ws = payload.get("workspace") or str(ROOT / "workspace")
        workspace = Path(raw_ws).resolve()
        if not workspace.is_relative_to(ROOT.resolve()):
            self._send_json({"error": "workspace outside project root"}, 400)
            return
        workspace.mkdir(parents=True, exist_ok=True)
        _CANCEL_FLAGS[rec["id"]] = False
        threading.Thread(
            target=run_forge, daemon=True,
            args=(rec["id"], task, payload.get("model"),
                  payload.get("strategy"), payload.get("thinking"),
                  payload.get("bundle"), payload.get("policy_mode"),
                  payload.get("max_steps"), payload.get("max_depth"),
                  payload.get("spawn_budget"), workspace),
        ).start()
        self._send_json({"run_id": rec["id"]}, 202)

    def _handle_cancel(self, run_id: str) -> None:
        rec = STORE.get(run_id)
        if not rec:
            self._send_json({"error": "run not found"}, 404)
            return
        if rec["state"] in ("done", "error", "cancelled"):
            self._send_json({"error": f"run already {rec['state']}"}, 400)
            return
        _CANCEL_FLAGS[run_id] = True
        STORE.update(run_id, state="cancelling")
        STORE.push_event(run_id, {"type": "state", "state": "cancelling"})
        self._send_json({"ok": True, "state": "cancelling"})


class ClientServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Planet evolving delivery client")
    ap.add_argument("--port", type=int, default=7712)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--workspace", default=str(ROOT / "workspace"))
    args = ap.parse_args(argv)
    load_keys_into_env()
    srv = ClientServer((args.host, args.port), Handler)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
