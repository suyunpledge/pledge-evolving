"""子 Agent 分工 + Agent 集群（移植自 always / AI Platform 的 sub-agent.ts 设计）。

概念：主对话把复杂问题拆成 N 个子任务，每个子任务交给一个 worker 独立调用
（可不同模型/不同 provider），结果汇总后注入主上下文，由主模型生成最终答复。

移植时保持的 AI Platform 契约（sub-agent.ts / route.ts）：
  - 每个 worker 温度固定 0.3（分工输出要确定、简短，不跟随主对话温度）
  - 默认并行执行（ThreadPoolExecutor），结果顺序与传入顺序一致
  - 单路超时（默认 30s）与失败不阻塞其他（error 记进 result）
  - 汇总注入：每路最多 1500 字符、总块最多 6000 字符（集群放宽 2000/8000）
  - 集群与分工互斥（同开会双份子调用 + 双份注入，token 翻倍且互相干扰）
  - 降级模型：每路可指定自己的 provider/模型，不传回落主对话的

与 AI Platform 的差异（forge 侧落地）：
  - 每路直连 provider 的 /chat/completions（OpenAI 协议），不走本地 gateway
    （gateway 是单上游，多路集群必须每路独立 url/key）
  - 密钥走 forge 的 $expr env 引用体系（复用 interaction_model.gateway_settings）
  - 传输用标准库 urllib（零第三方依赖）
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from interaction_model import gateway_settings
from http_transport import open_response, RequestCancelled

# 集群的同构编程 agent 系统提示词（照抄 AI Platform CLUSTER_AGENT_PROMPT 的语义）
CLUSTER_AGENT_PROMPT = (
    "You are a senior software engineer. Given the user's programming request, "
    "produce a complete, runnable, self-contained solution. Mark each file with a "
    "fenced code block. Be concise; skip prose and explanations."
)

DEFAULT_MAX_TOKENS = 800
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_TEMPERATURE = 0.3          # 分工输出固定温度（AI Platform 契约）
MAX_SUB_AGENTS = 6                 # 分工上限（AI Platform slice(0,6)）
MAX_CLUSTER_LANES = 4              # 集群 1-4 路

# 汇总注入的字符预算
SUB_FORMAT_LIMITS = {"max_chars_per_agent": 1500, "max_total_chars": 6000}
CLUSTER_FORMAT_LIMITS = {"max_chars_per_agent": 2000, "max_total_chars": 8000}


@dataclass
class SubAgent:
    """一个子任务 worker。"""
    id: str
    role: str
    system_prompt: str
    user_message: str
    model_name: str | None = None        # 降级模型：不传用主对话的
    provider: dict | None = None         # forge.patch.json 的 provider config；None 用默认
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout_s: float = DEFAULT_TIMEOUT_S
    context_text: str | None = None      # unified 记忆模式注入（isolated 不传）


@dataclass
class SubAgentResult:
    id: str
    role: str
    output: str = ""
    model_used: str = ""
    duration_ms: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.output.strip())


def provider_chat(provider: dict, env: dict, messages: list[dict], *,
                  model: str, temperature: float, max_tokens: int,
                  timeout_s: float, cancel_event=None) -> str:
    """直连一个 provider 的 OpenAI 兼容 /chat/completions（非流式）。

    provider 来自 forge.patch.json 的行 config（baseURL/model/apiKey env 引用）。
    密钥解析复用 gateway_settings；wire != openai 的 provider 不支持（与
    GUI 对话同口径）。
    """
    url, key, _default_model = gateway_settings(provider, env)
    if not model:
        model = _default_model
    endpoint = url.rstrip("/") + "/chat/completions"
    from forge.secrets import SecretScope, VendorCredential, redact
    credential = VendorCredential(key, url)
    scope = SecretScope()
    payload = json.dumps(scope.protect({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 **credential._header(endpoint, wire='openai')})
    try:
        with open_response(request, timeout=timeout_s, cancel_event=cancel_event) as resp:
            data = redact(json.loads(resp.read().decode("utf-8", "replace")))
    except RequestCancelled:
        raise
    except urllib.error.HTTPError as exc:
        body = redact(exc.read().decode("utf-8", "replace"))[:200]
        raise RuntimeError(f"HTTP {exc.code}: {body}") from None
    except Exception as exc:  # URLError / timeout / JSON
        raise RuntimeError(redact(str(exc) or type(exc).__name__)) from None
    try:
        content = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"上游响应结构异常: {str(data)[:160]}") from None
    if not isinstance(content, str):
        raise RuntimeError("上游回复 content 必须为文本")
    return content


def _run_one(agent: SubAgent, default_provider: dict | None, env: dict,
             default_model: str, temperature: float, cancel_event=None) -> SubAgentResult:
    """执行单个 worker（在工作线程里被调用）。"""
    model = agent.model_name or default_model
    provider = agent.provider or default_provider
    start = time.monotonic()
    result = SubAgentResult(id=agent.id, role=agent.role, model_used=model)
    if cancel_event is not None and cancel_event.is_set():
        result.error = "已停止请求"
        return result
    if provider is None:
        result.error = "未配置可用 Provider"
        result.duration_ms = int((time.monotonic() - start) * 1000)
        return result
    messages = [{"role": "system",
                 "content": f"{agent.system_prompt}\n\n"
                            "输出要求：直接给出结论要点，不要复述问题、不要解释"
                            "推理过程，保持简洁。"}]
    if agent.context_text:
        # 统一记忆模式：先注入共享上下文，再给分工任务
        messages.append({"role": "user", "content": agent.context_text})
    messages.append({"role": "user", "content": agent.user_message})
    try:
        output = provider_chat(provider, env, messages, model=model,
                               temperature=temperature,
                               max_tokens=agent.max_tokens,
                               timeout_s=agent.timeout_s,
                               **({"cancel_event": cancel_event} if cancel_event is not None else {}))
        if not output or not output.strip():
            result.error = "子 Agent 返回空内容"
        else:
            result.output = output
    except Exception as exc:
        result.error = str(exc) or type(exc).__name__
    result.duration_ms = int((time.monotonic() - start) * 1000)
    return result


def run_sub_agents(agents: list[SubAgent], env: dict, *,
                   default_provider: dict | None,
                   default_model: str,
                   temperature: float = DEFAULT_TEMPERATURE,
                   max_workers: int | None = None, cancel_event=None) -> list[SubAgentResult]:
    """并行执行所有子 Agent；任一失败不影响其他；结果顺序与传入顺序一致。"""
    agents = agents[:MAX_SUB_AGENTS]
    if not agents:
        return []
    workers = max_workers or min(8, max(1, len(agents)))
    results: list[SubAgentResult | None] = [None] * len(agents)

    def task(index: int, agent: SubAgent):
        # BaseException 也吞：一路 worker 的任何异常都不能炸掉整批
        # （KeyboardInterrupt 在子线程里只会作为 error 记录，主流程继续）。
        try:
            results[index] = _run_one(agent, default_provider, env,
                                      default_model, temperature, cancel_event)
        except BaseException as exc:
            if isinstance(exc, SystemExit):
                raise
            results[index] = SubAgentResult(
                id=agent.id, role=agent.role, model_used=agent.model_name or "",
                error=str(exc) or type(exc).__name__)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="subagent") as pool:
        futures = [pool.submit(task, i, a) for i, a in enumerate(agents)]
        for fut in as_completed(futures):
            try:
                fut.result()
            except BaseException:
                pass  # task 内已兜底；这里只防 BaseException 穿透主流程
    # 单路超时由 provider_chat 的 urllib timeout 保证；兜底替换仍未产出的槽位
    out: list[SubAgentResult] = []
    for i, agent in enumerate(agents):
        r = results[i]
        if r is None:
            r = SubAgentResult(id=agent.id, role=agent.role,
                               model_used=agent.model_name or "",
                               error=f"执行未完成(>{agent.timeout_s:.0f}s)")
        out.append(r)
    return out


def run_cluster(user_text: str, count: int, lanes: list[dict],
                env: dict, *, default_provider: dict | None,
                default_model: str,
                context_text: str | None = None,
                temperature: float = DEFAULT_TEMPERATURE, cancel_event=None) -> list[SubAgentResult]:
    """Agent 集群：N 路同构编程 agent 各自独立产出方案。

    lanes: [{provider: <provider config>, model: <模型名>}, ...]，不足处
    回落 default_provider/default_model。count 强制夹在 1..MAX_CLUSTER_LANES。
    """
    count = max(1, min(MAX_CLUSTER_LANES, int(count or 2)))
    lanes = list(lanes or [])[:count]
    agents: list[SubAgent] = []
    for i in range(count):
        lane = lanes[i] if i < len(lanes) else {}
        agents.append(SubAgent(
            id=f"cluster-{i + 1}",
            role=f"方案 {i + 1}",
            system_prompt=CLUSTER_AGENT_PROMPT,
            user_message=user_text,
            model_name=lane.get("model"),
            provider=lane.get("provider"),
            context_text=context_text,
            timeout_s=DEFAULT_TIMEOUT_S,
        ))
    return run_sub_agents(agents, env, default_provider=default_provider,
                          default_model=default_model, temperature=temperature,
                          cancel_event=cancel_event)


def format_sub_agent_results(results: list[SubAgentResult], *,
                             max_chars_per_agent: int = SUB_FORMAT_LIMITS["max_chars_per_agent"],
                             max_total_chars: int = SUB_FORMAT_LIMITS["max_total_chars"],
                             header: str = "") -> str:
    """紧凑汇总（AI Platform formatSubAgentResults 的 Python 移植）：
    每路输出截断到 max_chars_per_agent；失败标 · 失败；总长超 max_total_chars 截断。
    """
    if not results:
        return ""
    lines: list[str] = []
    total = 0
    for i, r in enumerate(results):
        head = f"[{r.role}{' · 失败' if r.error else ''}]"
        body = f"(error) {r.error}" if r.error else r.output[:max_chars_per_agent]
        line = f"{head}\n{body}"
        if total + len(line) > max_total_chars:
            lines.append(f"... (剩余 {len(results) - i} 个结果被截断以省 token)")
            break
        lines.append(line)
        total += len(line) + 2
    body_text = "\n\n".join(lines)
    return f"{header}\n{body_text}" if header else body_text


def sub_agent_injection(results: list[SubAgentResult]) -> str:
    """子 Agent 分工结果的注入文本（照抄 AI Platform 的注入协议）。"""
    return format_sub_agent_results(
        results,
        header=f"[子 Agent 分工结果 — 共 {len(results)} 个；仅供参考，"
               "引用时按 [role] 标记]",
    )


def cluster_injection(results: list[SubAgentResult]) -> str:
    """集群方案的注入文本（每路放宽到 2000/8000）。"""
    return format_sub_agent_results(
        results, max_chars_per_agent=CLUSTER_FORMAT_LIMITS["max_chars_per_agent"],
        max_total_chars=CLUSTER_FORMAT_LIMITS["max_total_chars"],
        header=f"[Agent 集群方案 — 共 {len(results)} 路；每路独立模型生成，"
               "请综合对比后给出最佳实现]",
    )


# ─── 持久化：~/.forge/agent-cluster.json ────────────────────────────

import os as _os
from pathlib import Path as _Path


def config_path() -> _Path:
    return _Path.home() / ".forge" / "agent-cluster.json"


DEFAULT_CONFIG = {
    "memory_mode": "isolated",       # isolated(默认省 token) / unified
    "cluster": {"enabled": False, "count": 2, "lanes": []},
    "sub_agents": {"enabled": False, "presets": []},
    # 预设模板：用户可直接启用或在此基础上改
    "templates": [
        {"id": "tpl-researcher", "role": "researcher",
         "system_prompt": "你是调研员。给出关键事实、来源线索与结论要点，"
                          "分点陈述，不解释推理。"},
        {"id": "tpl-summarizer", "role": "summarizer",
         "system_prompt": "你是摘要器。把输入压缩为 5 条以内的要点，"
                          "每条不超过 30 字。"},
        {"id": "tpl-critic", "role": "critic",
         "system_prompt": "你是评审员。只列风险与漏洞（按严重度排序），"
                          "每条给出触发条件，不给建议。"},
        {"id": "tpl-coder", "role": "coder",
         "system_prompt": "你是实现工程师。直接给出可运行的完整代码，"
                          "用围栏代码块，不写解释。"},
    ],
}


def load_config() -> dict:
    path = config_path()
    if not path.is_file():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    if not isinstance(data, dict):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    # 默认值补全
    for key, default in DEFAULT_CONFIG.items():
        data.setdefault(key, json.loads(json.dumps(default)))
    for key in ("cluster", "sub_agents"):
        if not isinstance(data[key], dict):
            data[key] = json.loads(json.dumps(DEFAULT_CONFIG[key]))
        for field, default in DEFAULT_CONFIG[key].items():
            data[key].setdefault(field, json.loads(json.dumps(default)))
    try:
        data["cluster"]["count"] = max(1, min(4, int(data["cluster"]["count"])))
    except (ValueError, TypeError, OverflowError):
        data["cluster"]["count"] = 2
    for section, field in (("cluster", "lanes"), ("sub_agents", "presets")):
        items = data[section].get(field)
        data[section][field] = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    if not isinstance(data["templates"], list):
        data["templates"] = json.loads(json.dumps(DEFAULT_CONFIG["templates"]))
    else:
        data["templates"] = [item for item in data["templates"] if isinstance(item, dict) and item.get("role")]
    if data["memory_mode"] not in ("isolated", "unified"):
        data["memory_mode"] = "isolated"
    return data


def save_config(config: dict) -> None:
    path = config_path()
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".agent-cluster-", suffix=".tmp", delete=False) as stream:
        tmp = _Path(stream.name)
    try:
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        _os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def enabled_sub_agent_presets(config: dict) -> list[dict]:
    """启用的分工预设（带 provider/model 解析所需字段）。"""
    sa = config.get("sub_agents") or {}
    if not sa.get("enabled"):
        return []
    return [p for p in (sa.get("presets") or []) if p.get("enabled", True)]


def build_sub_agents(presets: list[dict], user_message: str, *,
                     context_text: str | None = None) -> list[SubAgent]:
    """把预设配置变成本轮要跑的 SubAgent 列表。"""
    out: list[SubAgent] = []
    for i, p in enumerate(presets[:MAX_SUB_AGENTS]):
        role = str(p.get("role") or f"worker-{i + 1}")
        out.append(SubAgent(
            id=str(p.get("id") or f"sub-{i + 1}"),
            role=role,
            system_prompt=str(p.get("system_prompt") or "直接给出要点，保持简洁。"),
            user_message=user_message,
            model_name=p.get("model") or None,
            provider=p.get("provider") or None,
            max_tokens=int(p.get("max_tokens") or DEFAULT_MAX_TOKENS),
            timeout_s=float(p.get("timeout_s") or DEFAULT_TIMEOUT_S),
            context_text=context_text,
        ))
    return out
