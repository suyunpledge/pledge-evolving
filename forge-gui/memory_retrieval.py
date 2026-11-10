"""分级检索（长期记忆）接入：只服务 Work Mode 对话，不依赖 Tk。

对接内置目录里的 ``integration-hierarchical-retrieval``——本机分级检索服务
（默认 127.0.0.1:8000）的 ``GET /health``、``POST /retrieve``、``POST /ingest``。

边界约定：
- **默认关闭**：插件未安装 / 未启用时 :func:`memory_bridge_for` 返回 None，
  调用方据此零网络调用，行为与未接入前完全一致。
- **静默降级**：所有网络 / 解析异常都被吞掉，返回空结果；对话流程永远不会
  因记忆服务而失败。服务从可用变不可用时只给一次大白话提示。
- **Secret 不出门**：送出的查询与入库文本先过 ``forge.secrets.redact``；
  召回的记忆块注入前同样再过一遍。token 只放 Authorization 头，不进日志、
  不进错误信息。
- 只连本机：host 固定 127.0.0.1，清单里只取端口，避免被改写成外部地址。
- Coding Mode 有意不接：代码任务需要精确上下文，历史记忆注入易干扰，
  且会把项目代码写进记忆库。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from forge.secrets import redact

PLUGIN_ID = "integration-hierarchical-retrieval"
# connector.* 前缀：secret_store.env_for 永不把它注入模型 / gateway 环境。
SECRET_NAME = "connector.hierarchical-retrieval"
DEFAULT_PORT = 8000
DEFAULT_TIMEOUT = 2.0
CONTEXT_CHAR_BUDGET = 1500

# ---- 第四轮（2026-11-04）：命中优化 + 模型适配 ----
# 查询增富：短句/指代问句的嵌入质量差（"继续"/"那个预算超了怎么办"检索出噪声
# 或空手而归），拼上上一轮话题锚（话题名+用户发言头部）给嵌入一个语义立足点。
# 纯函数：同输入恒同字节输出，不破坏前缀缓存确定性。
_ANAPHORA_MARKERS = ("继续", "刚才", "上面", "那个", "这个方案", "它怎么", "再说",
                    "接着说", "详细说说", "为什么这样", "然后呢")
_SHORT_QUERY_CHARS = 40
_ANCHOR_HEAD_CHARS = 48


def retrieval_profile(model: str):
    """按模型解析厂商画像（forge.vendors 单表识别；空模型=generic 默认档）。"""
    from forge.vendors import profile_for
    return profile_for(model=str(model or ""))


def build_query(user_text: str, *, topic_anchor: str = "",
                last_user_head: str = "") -> str:
    """确定性查询增富：需要时把话题锚拼进查询（命中效果主抓手）。

    触发条件（二者其一）：查询短于 40 字符；含指代/延续标记词。
    锚 = 上一轮检出话题名 ｜ 上一轮用户发言头 48 字符，总长封顶 80。
    """
    text = " ".join(str(user_text or "").split())
    if not text:
        return text
    anchor = "｜".join(p for p in (" ".join(str(topic_anchor or "").split()),
                                   " ".join(str(last_user_head or "").split())) if p)
    if not anchor:
        return text
    needs = (len(text) < _SHORT_QUERY_CHARS
             or any(marker in text for marker in _ANAPHORA_MARKERS))
    if not needs:
        return text
    return f"{text}（相关话题：{anchor[:80]}）"
INGEST_CHAR_LIMIT = 8000
MEMORY_HEADER = ("[长期记忆·分级检索] 以下内容由本机分级检索服务按话题自动召回，"
                 "来自过往对话，仅供参考；若与当前问题无关请忽略：")
DOWN_NOTICE = "长期记忆服务没连上，这次先不带记忆回答，对话不受影响"
AUTH_NOTICE = "长期记忆服务拒绝访问（口令不对），这次先不带记忆回答"

Opener = Callable[..., Any]


class RetrievalClient:
    """分级检索 HTTP 客户端。任何失败都返回空结果，并在 last_error 留一个
    不含敏感内容的简短原因（'unreachable' / 'timeout' / 'http-401' …）。"""

    def __init__(self, port: int = DEFAULT_PORT, *, token: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT, opener: Opener | None = None):
        self.base_url = f"http://127.0.0.1:{int(port)}"
        self._token = token or None
        self.timeout = float(timeout)
        self._opener = opener or urllib.request.urlopen
        self.last_error: str = ""

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict | None:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers=headers, method=method)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                body = resp.read(2 * 1024 * 1024)
            parsed = json.loads(body.decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            self.last_error = f"http-{exc.code}"
            return None
        except TimeoutError:
            self.last_error = "timeout"
            return None
        except urllib.error.URLError as exc:
            self.last_error = "timeout" if isinstance(exc.reason, TimeoutError) else "unreachable"
            return None
        except Exception:  # 解析失败、连接重置等：一律静默降级
            self.last_error = "unreachable"
            return None
        if not isinstance(parsed, dict):
            self.last_error = "bad-response"
            return None
        self.last_error = ""
        return parsed

    def health(self) -> bool:
        result = self._request("GET", "/health")
        return bool(result) and result.get("status") == "ok"

    def retrieve(self, query: str, *, top_k_sentences: int | None = None,
                 top_k_context: int | None = None) -> dict:
        query = str(redact(str(query or ""))).strip()
        if not query:
            return {}
        payload: dict[str, Any] = {"query": query}
        if top_k_sentences is not None:
            payload["top_k_sentences"] = int(top_k_sentences)
        if top_k_context is not None:
            payload["top_k_context"] = int(top_k_context)
        return self._request("POST", "/retrieve", payload) or {}

    def ingest(self, text: str, metadata: dict | None = None) -> dict:
        text = str(redact(str(text or ""))).strip()
        if not text:
            return {}
        payload: dict[str, Any] = {"text": text[:INGEST_CHAR_LIMIT]}
        if metadata:
            payload["metadata"] = redact(dict(metadata))
        return self._request("POST", "/ingest", payload) or {}


def format_memory_block(result: dict, char_budget: int = CONTEXT_CHAR_BUDGET,
                        min_score: float = 0.0) -> str:
    """把 /retrieve 结果装配成有界、标注来源的记忆块；无内容返回 ''。

    优先级与服务端 assemble_context 一致：话题名 > 关键句 > 原文片段，
    同级按分数降序，超预算从最不重要的条目开始丢。"""
    if not isinstance(result, dict):
        return ""
    entries: list[tuple[int, float, str]] = []
    domain = result.get("detected_domain")
    if isinstance(domain, dict) and domain.get("name"):
        entries.append((0, 1.0, f"当前话题：{domain['name']}"))
    for ks in result.get("key_sentences") or []:
        if isinstance(ks, dict) and str(ks.get("text") or "").strip():
            if _score(ks) < min_score:
                continue
            entries.append((1, _score(ks), f"- {str(ks['text']).strip()}"))
    for ctx in result.get("full_contexts") or []:
        if not isinstance(ctx, dict):
            continue
        if _score(ctx) < min_score:
            continue
        body = str(ctx.get("chunk_text") or (ctx.get("full_text") or "")[:512]).strip()
        if body:
            entries.append((2, _score(ctx), f"· 原文片段：{body}"))
    if not any(pri > 0 for pri, _s, _t in entries):
        return ""  # 只有话题名、没有实际记忆，不值得占上下文
    entries.sort(key=lambda e: (e[0], -e[1]))
    used, kept = len(MEMORY_HEADER), []
    for _pri, _score_value, text in entries:
        if used + len(text) + 1 > char_budget:
            continue
        kept.append(text)
        used += len(text) + 1
    if not any(not t.startswith("当前话题：") for t in kept):
        return ""
    return str(redact(MEMORY_HEADER + "\n" + "\n".join(kept)))


def _score(item: dict) -> float:
    try:
        return float(item.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


class MemoryBridge:
    """对话路径用的门面：检索 → 记忆块、后台入库、一次性降级提示。"""

    def __init__(self, client: RetrievalClient, model: str = ""):
        self.client = client
        self.model = str(model or "")
        # 话题锚（上一轮检出话题/用户头）：下一轮短句/指代查询的增富素材。
        # 顺序确定性：同一对话序列恒产生同一锚 → 同一字节查询。
        self._last_topic = ""
        self._last_user_head = ""
        self._lock = threading.Lock()
        self._notified = False
        self._pending_notice = ""
        self.available = False  # 最近一次 context_for 时服务是否健康；不健康就不入库

    def _mark(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self._notified = False  # 服务恢复后，下次再挂还能再提示一次
                return
            if not self._notified:
                self._notified = True
                self._pending_notice = (AUTH_NOTICE if self.client.last_error == "http-401"
                                        else DOWN_NOTICE)

    def consume_notice(self) -> str:
        with self._lock:
            notice, self._pending_notice = self._pending_notice, ""
            return notice

    def context_for(self, query: str, model: str | None = None) -> str:
        """发送前调用（必须在工作线程）：健康 → 增富查询 → 画像化检索 → 记忆块。

        第四轮起按模型适配：预算/门槛/检索宽度取自厂商画像
        （forge.vendors.VendorProfile.memory_*，选模型即生效）；
        model=None 时沿用桥构造时的模型。任何失败返回 ''。
        """
        try:
            self.available = self.client.health()
            if not self.available:
                self._mark(False)
                return ""
            model_name = self.model if model is None else str(model or "")
            profile = retrieval_profile(model_name)
            budget = int(getattr(profile, "memory_char_budget", 0) or CONTEXT_CHAR_BUDGET)
            min_score = float(getattr(profile, "memory_min_score", 0.0) or 0.0)
            top_k = int(getattr(profile, "memory_top_k", 0) or 0)
            enriched = build_query(query, topic_anchor=self._last_topic,
                                   last_user_head=self._last_user_head)
            result = self.client.retrieve(enriched, top_k_sentences=top_k or None,
                                           top_k_context=top_k or None)
            if not result and self.client.last_error:
                self._mark(False)
                return ""
            self._mark(True)
            domain = result.get("detected_domain") if isinstance(result, dict) else None
            if isinstance(domain, dict) and domain.get("name"):
                self._last_topic = str(domain["name"])
            head = " ".join(str(query or "").split())[:_ANCHOR_HEAD_CHARS]
            if head:
                self._last_user_head = head
            return format_memory_block(result, char_budget=budget, min_score=min_score)
        except Exception:
            self.available = False
            self._mark(False)
            return ""

    def ingest_turn(self, user_text: str, reply: str, *, session: str | None = None) -> bool:
        try:
            user_text, reply = str(user_text or "").strip(), str(reply or "").strip()
            if not self.available or not user_text or not reply:
                return False
            text = f"用户：{user_text}\n助手：{reply}"
            metadata = {"source": "forge-work-mode"}
            if session:
                metadata["session"] = str(session)
            ok = bool(self.client.ingest(text, metadata))
            self._mark(ok)
            return ok
        except Exception:
            return False

    def ingest_turn_async(self, user_text: str, reply: str, *,
                          session: str | None = None) -> threading.Thread:
        thread = threading.Thread(target=self.ingest_turn, args=(user_text, reply),
                                  kwargs={"session": session}, daemon=True,
                                  name="Forge-memory-ingest")
        thread.start()
        return thread


def plugin_port(home: str | Path) -> int | None:
    """插件已安装且已启用时返回端口，否则 None。只读一个小状态文件 + 清单，
    不扫描整个目录（未安装用户每次发送不应多付代价）。"""
    import plugin_market as pm
    home = Path(home)
    try:
        state = json.loads((home / pm.MARKET_DIRNAME / pm.STATE_NAME).read_text(encoding="utf-8"))
        if PLUGIN_ID not in (state.get("enabled") or []):
            return None
        if PLUGIN_ID not in (state.get("installed") or {}):
            return None
        manifest = json.loads((home / pm.PLUGINS_DIRNAME / PLUGIN_ID / pm.MANIFEST_NAME)
                              .read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    try:
        port = int(((manifest.get("provides") or {}).get("port")) or DEFAULT_PORT)
    except (TypeError, ValueError, AttributeError):
        port = DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def _stored_token() -> str | None:
    try:
        import secret_store
        return secret_store.get_provider(SECRET_NAME) or None
    except Exception:
        return None


def memory_bridge_for(home: str | Path, *, opener: Opener | None = None,
                      token: str | None = None) -> MemoryBridge | None:
    """插件启用时返回桥；未安装 / 未启用返回 None（调用方零网络调用）。"""
    port = plugin_port(home)
    if port is None:
        return None
    return MemoryBridge(RetrievalClient(port, token=token if token is not None else _stored_token(),
                                        opener=opener))
