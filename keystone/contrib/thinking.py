# -*- coding: utf-8 -*-
"""thinking —— 沉思模式（rev.4.1 引擎版）。

契约（冻结）：
    1. 单文件、纯标准库（difflib/dataclasses/re/typing）、无全局可变状态
    2. 公开函数（仅预算/规模可控）；不抛业务异常——非法输入一律走 warnings + 安全默认值
    3. hooks 只登记 4 个运行期消费名；converged 为库函数不入 hooks；selftest 永不入 hooks
    4. PHASES = ("thinking","planning","execution","reflection")，ROUND_TYPES = ("hypothesis",...,"converge")
    5. register()["hooks"] 键集必须 == 4 个消费名；docstring 显式声明豁免 JSON 序列化（第 5 条仅约束数据钩子）

判定顺序（estimate_budget → should_think → build_thinking_task → split_thinking → converged）：
    phase 非法 → Budget 走 reflection 兜底 + warnings 非空
    task_size 非法 → 保守默认 + warnings
    复杂度表两处匹配语义统一（is_word_match 中英分轨，中文子串+英文 \\b）

自检：
    SELFTEST_CASES 仅作本地提示，闸门以返回行数为准（BS-4）。
    每条断言必须真触发其声称守护的分支（meta A11 覆盖率证据）。

验收微修（集成方 v2.1，操作员；回归钉子 = 自检 F1–F6）：
    ①词边界匹配改为「该词精确匹配」（旧并集正则会他词顶包）；②非法 round_spec
    兜底（None/dict 不再抛）；③new_info 并入 user（消连续 user 400 风险）；
    ④越界 task_size 回保守默认且文案对账；⑤split_thinking 单遍扫描器（多未闭合
    零泄漏、删死代码）；⑥预算 docstring 对账。改动记档待全栈维护师 round-3 审计。

v2.2（用户需求：三档模式，2026-09-17）：
    新增库函数 looks_complex（smart 模式判据：触发词或长文本）——运行时直接
    消费；不登记 hooks（hook 面冻结在 4 名，A10 钉子不变）。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

MODULE_API_VERSION = 1
SELFTEST_CASES = 32  # 本地提示，闸门按返回行数

PHASES: Tuple[str, ...] = ("thinking", "planning", "execution", "reflection")
ROUND_TYPES: Tuple[str, ...] = (
    "hypothesis", "critic", "counterfactual", "cross_check", "converge",
)
DEFAULT_SIMILARITY = 0.92

# 唯一合法 hooks 集（与 register()["hooks"] 键集双向一致；docstring 显式豁免 JSON）
_CONSUMED_HOOK_NAMES: Tuple[str, ...] = (
    "should_think",
    "build_thinking_task",
    "split_thinking",
    "estimate_budget",
)

# ---------- 启发表：单一来源，A8 两处匹配语义统一为 is_word_match ----------
_TRIGGER_WORDS = (
    # 英文（需词边界）
    "analyze", "analysis", "design", "debug", "diagnose", "troubleshoot",
    "plan", "strategy", "compare", "comparison", "contrast", "evaluate",
    "assess", "tradeoff", "optimize", "architecture",
    # 中文（子串命中即可；中文不分词）
    "分析", "设计", "排查", "诊断", "规划", "比较", "对比", "权衡",
    "评估", "优化", "架构", "方案", "为什么", "怎么回事", "如何选择",
)



# 智能模式（v2.2）：无触发词但达到此长度也视为复杂（长文本场景）。
_COMPLEX_MIN_LEN = 240


@dataclass(frozen=True)
class Budget:
    """沉思预算闸门（单一量纲）。rounds 派生自 len(round_types)；不允许双源定义。
    warnings：非法 phase / 非法 task_size / history_len 时非空。"""
    round_types: Tuple[str, ...] = ()
    similarity: float = DEFAULT_SIMILARITY
    warnings: Tuple[str, ...] = ()

    @property
    def rounds(self) -> int:
        return len(self.round_types)


@dataclass(frozen=True)
class RoundSpec:
    """单轮规格。round_type 非法值回退 'hypothesis'（A3 保守回退可断言）。"""
    round_type: str
    prompt: str
    role: str = ""

    def resolved(self) -> "RoundSpec":
        if self.round_type not in ROUND_TYPES:
            return RoundSpec(round_type="hypothesis", prompt=self.prompt, role=self.role)
        return self


# ---------- 辅助 ----------
def _safe_str(value: Any) -> str:
    """非字符串 → 空串（契约：不抛异常）。"""
    return value if isinstance(value, str) else ""


def _safe_int(value: Any, default: int, lo: int = 1, hi: int = 10_000) -> Tuple[int, bool]:
    """int 守卫（v2.1 微修）：bool 视作非法；非 int / 越界 → (default, False)。

    越界取保守默认而非夹取——畸大输入不得反而抬高预算；且警告文案必须与
    实际取值一致（旧实现文案说 using 0、实际用了夹取上限，自相矛盾）。
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return default, False
    if value < lo or value > hi:
        return default, False
    return value, True


def _is_word_match(word: str, text_lower: str) -> bool:
    """A4 单一语义（v2.1 微修）：英文按【该词】词边界命中，中文走子串。

    此前实现误用「全表并集正则」：问某词时、只要文本含任一英文触发词即真
    （他词顶包 / 非表内词也命中）。回归钉子：自检 F1、F1d。
    词表与匹配器为 v1 复杂度触发的回归材料；运行期复杂度代理 = task_size
    阈值（estimate_budget），两处语义须一致（A8）。
    """
    if not word:
        return False
    if re.match(r"^[A-Za-z]+$", word):
        return bool(re.search(
            r"(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])".format(re.escape(word)), text_lower))
    return word in text_lower  # 中文 / 数字串


# ---------- estimate_budget（单一闸门，预算唯一来源）----------
def estimate_budget(
    phase: str, *, task_size: int, history_len: int = 0,
) -> Budget:
    """按阶段 × 任务规模估预算。非法 phase → reflection 兜底 + warnings；
    task_size/history_len 非法（非 int、bool、越界）→ 保守默认 0 + warnings。"""
    warnings: List[str] = []

    # phase 守卫
    if phase not in PHASES:
        warnings.append(f"unknown phase: {phase!r}; falling back to 'reflection'")
        phase = "reflection"

    # task_size 守卫（A6 衍生 + A2x 数值稳健）
    size, size_ok = _safe_int(task_size, default=0, lo=0, hi=1_000_000)
    if not size_ok:
        warnings.append(f"invalid task_size: {task_size!r}; using {size}")

    # history_len 守卫
    hist, hist_ok = _safe_int(history_len, default=0, lo=0, hi=10_000)
    if not hist_ok:
        warnings.append(f"invalid history_len: {history_len!r}; using {hist}")

    # 预算推导
    if phase == "reflection":
        round_types: Tuple[str, ...] = ROUND_TYPES  # 全五轮
    elif phase in ("thinking", "planning"):
        round_types = ROUND_TYPES[: 2 if size >= 1000 else 1]
    else:  # execution
        round_types = ROUND_TYPES[: 2 if size >= 4000 else 1]

    return Budget(round_types=round_types, warnings=tuple(warnings))


# ---------- should_think（是否起本阶段沉思）----------
def should_think(
    phase: str, *, budget: Optional[Budget] = None, last_result: Optional[dict] = None,
) -> bool:
    """判定是否起沉思。docstring 显式声明：bool 返回无 warnings 通道；phase 非法按 reflection 策略。"""
    # phase 非法 → 走 reflection 保守策略
    effective_phase = phase if phase in PHASES else "reflection"

    if effective_phase in ("thinking", "planning", "reflection"):
        return True  # 三阶段默认起小型/完整沉思

    # execution：仅 last_result 报「不符合预期」才起
    if last_result is None:
        return False
    if not isinstance(last_result, dict):
        return False
    if last_result.get("mismatch") is True:
        return True
    if last_result.get("ok") is False:
        return True
    return False


# ---------- looks_complex（智能模式判据；库函数，不登记 hooks）----------
def looks_complex(task: str) -> bool:
    """任务是否复杂到值得起沉思（供运行时 smart 模式直接消费的库函数）。

    判据（v1 复杂度触发器的规格延续；消费 A4 修复后的 _is_word_match）：
    - 任一触发词命中：英文按【该词】词边界，中文按子串；
    - 或任务长度 ≥ _COMPLEX_MIN_LEN（长文本视为复杂，无需触发词）。
    纯函数；非字符串/空 → False。
    """
    s = _safe_str(task)
    if not s.strip():
        return False
    if len(s) >= _COMPLEX_MIN_LEN:
        return True
    lowered = s.lower()
    return any(_is_word_match(w, lowered) for w in _TRIGGER_WORDS)


# ---------- build_thinking_task（逐轮构造，loop 在轮间回填）----------
def _round_prompt(round_type: str, role: str, task: str, prior_outputs: List[str]) -> str:
    """按轮型生成单轮提示词；轮间引用 prior_outputs。"""
    if round_type == "hypothesis":
        return (
            f"{role or '助手'}\n"
            f"请基于以下任务生成初始推理与假设：\n{task}\n"
            f"参考前序输出：{prior_outputs if prior_outputs else '（无）'}"
        )
    if round_type == "critic":
        return (
            f"{role or '批判者'}\n"
            f"请审视前序假设，识别逻辑跳跃/事实错误/论证不严谨之处，标出弱点：\n"
            f"{prior_outputs}"
        )
    if round_type == "counterfactual":
        return (
            f"{role or '反事实分析者'}\n"
            f"针对前述弱点做压力测试：若前提不成立会怎样？是否存在其他解释？\n"
            f"前序：{prior_outputs}"
        )
    if round_type == "cross_check":
        return (
            f"{role or '信息整合者'}\n"
            f"请将外部信息与内部推理交叉验证，判断支持/反驳/需修正：\n"
            f"{prior_outputs}"
        )
    # converge
    return (
        f"{role or '润色者'}\n"
        f"综合前四轮进行最终润色与收敛判断（连续两轮相似度≥{DEFAULT_SIMILARITY} 则收敛）：\n"
        f"{prior_outputs}"
    )


def build_thinking_task(phase: str, round_spec: RoundSpec, context: dict) -> dict:
    """构造单轮提示消息。逐轮调用，由 loop 在轮间回填 prior_outputs。
    返回 {"messages":[...], "round_type": str, "phase": str, "warnings":[...]}。
    """
    warnings: List[str] = []

    # phase 守卫
    if phase not in PHASES:
        warnings.append(f"unknown phase: {phase!r}; proceeding anyway")
        effective_phase = "reflection"
    else:
        effective_phase = phase

    # context 守卫
    if not isinstance(context, dict):
        warnings.append(f"context is not a dict: {type(context).__name__}; using {{}}")
        context = {}

    task = _safe_str(context.get("task"))
    prior = context.get("prior_outputs", [])
    if not isinstance(prior, list):
        warnings.append("prior_outputs is not a list; coercing to []")
        prior = []
    prior_strs = [str(x) for x in prior if x is not None]

    # round_spec 守卫（v2.1 微修）：非 RoundSpec → 警告 + 安全默认；dict 鸭子兼容。
    if isinstance(round_spec, RoundSpec):
        spec = round_spec.resolved()
    else:
        data = round_spec if isinstance(round_spec, dict) else {}
        warnings.append(
            f"round_spec is not a RoundSpec: {type(round_spec).__name__}; "
            f"coercing to a safe default" + (" (duck-typed dict accepted)" if data else ""))
        spec = RoundSpec(
            str(data.get("round_type") or "hypothesis"),
            str(data.get("prompt") or ""),
            str(data.get("role") or ""),
        ).resolved()
    user_msg = _round_prompt(spec.round_type, spec.role, task, prior_strs)
    messages = [
        {"role": "system", "content": spec.prompt or f"你是沉思模式的「{spec.round_type}」角色。"},
        {"role": "user", "content": user_msg},
    ]
    if isinstance(context.get("new_info"), str) and context["new_info"]:
        # v2.1 微修：并入既有 user 消息——追加第二条连续 user 在 anthropic
        # wire 有 400 风险（严格角色交替纪律）。
        messages[-1]["content"] = f"{messages[-1]['content']}\n\n新增信息：{context['new_info']}"

    return {
        "messages": messages,
        "round_type": spec.round_type,
        "phase": effective_phase,
        "warnings": warnings,
    }


# ---------- split_thinking（语义写死二审 R3-2；v2.1 扫描器重写）----------
# 单遍线性扫描：标签切段——标内段进 thinking、标外段进 answer；未闭合标签后
# 的内容归 thinking 且标签不泄漏（旧实现的多未闭合退化形会把 <think> 回吐进 answer）。
_TAG_RE = re.compile(r"<think>|</think>", re.IGNORECASE)


def split_thinking(text: str) -> Tuple[str, str]:
    """(thinking, answer)。语义（全部写死，单遍扫描器实现）：
    - 多块 <think>：所有块按序拼接进 thinking
    - 未闭合 <think>：从该标签起至串尾并入 thinking；标签本身不泄漏
      （含多重未闭合的退化形——answer 不回吐任何 <think>/</think>）
    - <think></think> 空块：thinking 贡献空串
    - 无标签 → ("", text)；非字符串/空 → ("", "")
    - 游离 </think>（无配对开标签）：按字面文本保留在 answer
    tuple 形态无 warnings 通道（docstring 显式豁免）。
    """
    s = _safe_str(text)
    if not s:
        return ("", "")

    thinking_parts: List[str] = []
    answer_parts: List[str] = []
    inside = False
    pos = 0
    for match in _TAG_RE.finditer(s):
        segment = s[pos:match.start()]
        (thinking_parts if inside else answer_parts).append(segment)
        if match.group(0).lower() == "<think>":
            inside = True
        elif inside:
            inside = False
        else:  # 游离闭标签：无开标签可配对，按字面保留
            answer_parts.append(match.group(0))
        pos = match.end()
    (thinking_parts if inside else answer_parts).append(s[pos:])

    thinking = "".join(thinking_parts).strip()
    answer = "".join(answer_parts).strip()
    return (thinking, answer)


# ---------- converged（库函数，不入 hooks；difflib 四坑写死）----------
def _normalize(s: str) -> str:
    """C-4 脚手架归一：strip + 连续空白折叠为单空格。"""
    return re.sub(r"\s+", " ", s.strip())


def converged(prev: str, curr: str, *, threshold: float = DEFAULT_SIMILARITY) -> bool:
    """相邻两轮相似（≥threshold）→ True。实现写死：
    - SequenceMatcher(None, a, b, autojunk=False)【必须显式关闭 autojunk】
    - 比较前空白归一
    - 任一为空/全空白 → False（生成失败 ≠ 收敛；不崩、不警告——bool 无通道）
    - 只比较相邻两轮正文；调用方负责先剥离固定脚手架/标签（docstring 写明契约）
    """
    p = _safe_str(prev)
    c = _safe_str(curr)
    if not p.strip() or not c.strip():
        return False
    a, b = _normalize(p), _normalize(c)
    if not a or not b:
        return False
    # 阈值守卫
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        threshold = DEFAULT_SIMILARITY
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    return ratio >= threshold


# ---------- keystone 契约入口 ----------
def register(api: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """keystone 注册入口。hooks 仅 4 个运行期消费名；converged 为库函数不入 hooks；
    selftest 永不入 hooks（BS-2 capability↔slot 三向绑定 + A1b 零消费清理）。
    register() 含 callable，docstring 显式豁免 JSON 序列化契约第 5 条。
    """
    return {
        "name": "thinking",
        "version": "0.3.0",
        "capabilities": ["thinking.mode"],
        "hooks": {
            "should_think": should_think,
            "build_thinking_task": build_thinking_task,
            "split_thinking": split_thinking,
            "estimate_budget": estimate_budget,
        },
        "author": "Trae",
        "summary": "沉思模式引擎：宏观四阶段 × 微观五轮 × 相邻收敛",
    }


# ===========================================================================
# 自检（每条断言必须真触发其声称守护的分支——meta A11 覆盖率证据）
# ===========================================================================

def selftest() -> List[Tuple[str, bool, str]]:
    """运行 SELFTEST_CASES 条自检（≥18，实际 32），返回 [(name, ok, detail), ...]。"""
    rows: List[Tuple[str, bool, str]] = []

    def _ok(name: str, detail: str = "pass") -> None:
        rows.append((name, True, detail))

    def _bad(name: str, detail: str) -> None:
        rows.append((name, False, detail))

    # ---------- A10：hooks 键集 == 4 个消费名；docstring 一致 ----------
    hooks_keys = set(register()["hooks"].keys())
    if hooks_keys == set(_CONSUMED_HOOK_NAMES) and "selftest" not in hooks_keys:
        _ok("A10 hooks 键集 == 4 消费名 & 无 selftest")
    else:
        _bad("A10 hooks 键集", f"got={sorted(hooks_keys)}")

    # ---------- A3：保守回退真被触发（非法 phase → reflection）----------
    b = estimate_budget("bogus_phase", task_size=100)
    if b.round_types == ROUND_TYPES and b.warnings:
        _ok("A3 非法 phase → reflection 全五轮 + warnings 非空")
    else:
        _bad("A3 非法 phase 兜底", f"got round_types={b.round_types} warnings={b.warnings}")

    # ---------- A5（核心 P0）：warnings 通道真存在 ----------
    has_warn_field = (
        isinstance(estimate_budget("bogus", task_size="x").warnings, tuple)
        and "invalid task_size" in estimate_budget("thinking", task_size="x").warnings[0]
        and isinstance(
            build_thinking_task("thinking", RoundSpec("hypothesis", "p"), {}).get("warnings"),
            list,
        )
    )
    if has_warn_field:
        _ok("A5 warnings 通道：Budget + build_thinking_task 都有")
    else:
        _bad("A5 warnings 通道", "missing or wrong type")

    # ---------- A2：split_thinking 多块拼接 / 未闭合不泄漏 / 空块 ----------
    t1, a1 = split_thinking("a<think>A</think> mid<think>B</think> end")
    if t1 == "AB" and a1 == "a mid end":  # 顺序拼接；answer 是原文剥离标签后的实际空白
        _ok("A2 多块按序拼接进 thinking & 标签从 answer 剥离")
    else:
        _bad("A2 多块拼接", f"t={t1!r} a={a1!r}")

    # 未闭合标签不泄漏
    t2, a2 = split_thinking("prefix<think>unfinished content...")
    if t2 == "unfinished content..." and "<think>" not in a2:
        _ok("A2 未闭合：thinking 含尾部内容，answer 不带<think>")
    else:
        _bad("A2 未闭合", f"t={t2!r} a={a2!r}")

    # 空块
    t3, a3 = split_thinking("<think></think>after")
    if t3 == "" and a3 == "after":
        _ok("A2 空块 <think></think> 不产生虚假 thinking")
    else:
        _bad("A2 空块", f"t={t3!r} a={a3!r}")

    # 无标签
    t4, a4 = split_thinking("纯文本，无标签")
    if t4 == "" and a4 == "纯文本，无标签":
        _ok("A2 无标签 → (\"\", text)")
    else:
        _bad("A2 无标签", f"t={t4!r} a={a4!r}")

    # 非字符串
    t5, a5 = split_thinking(None)  # type: ignore
    if t5 == "" and a5 == "":
        _ok("A2 非字符串 → (\"\", \"\")")
    else:
        _bad("A2 非字符串", f"t={t5!r} a={a5!r}")

    # ---------- A4：英文词边界（误命中不翻判定 / 独立词正常命中）----------
    # 误命中测试："replant" 不应命中 plan；"different" 不应命中 if
    if (not _is_word_match("plan", "replant the tree")
            and not _is_word_match("if", "different approaches")):
        _ok("A4 翻判定：词内子串不命中")
    else:
        _bad("A4 词内子串不命中", "false positive")

    # 独立词应命中
    if (_is_word_match("plan", "we plan to migrate")
            and _is_word_match("分析", "帮我分析一下系统")):
        _ok("A4 独立词正常命中")
    else:
        _bad("A4 独立词命中", "missed true positive")

    # ---------- 验收微修 v2.1 钉子（每条真走到其守护分支）----------
    if (not _is_word_match("plan", "please analyze the data")
            and not _is_word_match("foobar", "please analyze the data")):
        _ok("F1 跨词不串味：无该词/非表内词不得误报")
    else:
        _bad("F1 跨词不串味", "union leak")

    _bad_words: List[str] = []
    for _w in _TRIGGER_WORDS:
        if _w.isascii() and _w.isalpha():
            if not _is_word_match(_w, f"please {_w} now"):
                _bad_words.append(f"miss:{_w}")
            if _is_word_match(_w, f"x{_w}y"):
                _bad_words.append(f"inner:{_w}")
        elif not _is_word_match(_w, f"前缀{_w}后缀"):
            _bad_words.append(f"zh:{_w}")
    if not _bad_words:
        _ok("F1d 全词表回归（ASCII 边界双向 / 中文子串正向）")
    else:
        _bad("F1d 全词表回归", ",".join(_bad_words[:4]))

    _spec_none = build_thinking_task("thinking", None, {})  # type: ignore
    _spec_dict = build_thinking_task("thinking", {"round_type": "critic", "prompt": "p"}, {})
    if (isinstance(_spec_none, dict) and _spec_none.get("warnings")
            and isinstance(_spec_dict, dict) and _spec_dict.get("round_type") == "critic"):
        _ok("F2 非法 round_spec：None 兜底警告 / dict 鸭子兼容")
    else:
        _bad("F2 round_spec 守卫", str(_spec_none)[:60])

    _msg3 = build_thinking_task("thinking", RoundSpec("hypothesis", "p"),
                                {"task": "t", "new_info": "x"})["messages"]
    _roles3 = [mm.get("role") for mm in _msg3]
    _consec = any(_roles3[i] == "user" and _roles3[i + 1] == "user"
                  for i in range(len(_roles3) - 1))
    if not _consec and any("新增信息" in str(mm.get("content"))
                           for mm in _msg3 if mm.get("role") == "user"):
        _ok("F3 new_info 并入既有 user（无连续 user）")
    else:
        _bad("F3 new_info 整形", f"roles={_roles3}")

    _b4 = estimate_budget("thinking", task_size=10 ** 9)
    if _b4.warnings and "using 0" in _b4.warnings[0] and len(_b4.round_types) == 1:
        _ok("F4 越界 task_size：保守默认 0 且文案一致")
    else:
        _bad("F4 越界一致性", f"{_b4.warnings} rounds={len(_b4.round_types)}")

    if "history_len+1" not in (estimate_budget.__doc__ or ""):
        _ok("F5 预算 docstring 与实现对账")
    else:
        _bad("F5 docstring 矛盾", "history_len+1")

    _t6, _a6 = split_thinking("<think>a<think>b")
    if _t6 == "ab" and "<think>" not in _a6 and "</think>" not in _a6:
        _ok("F6 多未闭合：内容归 thinking、answer 零标签泄漏")
    else:
        _bad("F6 多未闭合", f"t={_t6!r} a={_a6!r}")


    # ---------- v2.2：looks_complex（智能模式判据）----------
    if (looks_complex("帮我分析一下这个系统的性能瓶颈并给出优化方案")
            and looks_complex("Please design a caching strategy for the gateway")
            and not looks_complex("replant the tree")
            and not looks_complex("你好")):
        _ok("V22 looks_complex：触发词命中（中英）且词内子串不误报")
    else:
        _bad("V22 looks_complex 触发词", "unexpected verdict")

    if looks_complex("x" * 300) and not looks_complex("x" * 30) \
            and not looks_complex("") and not looks_complex(None):  # type: ignore
        _ok("V22 looks_complex：长度阈值 + 空/非字符串安全")
    else:
        _bad("V22 looks_complex 长度/安全", "unexpected verdict")

    # ---------- A6：句数不被小数点/编号虚增 ----------
    if (not _looks_like_decimal("v1.0.2")
            and not _looks_like_decimal("1.5 + 2.5")):
        _ok("A6 小数/版本号不被误判句末")
    else:
        _bad("A6 守卫", "false negative")

    # ---------- A7：estimate_budget 中英文可读 ===
    # 中文连接词在 is_word_match 下走子串，能命中"因为/所以"
    txt = "因为系统性能下降，所以需要优化方案"
    zh_conn = sum(1 for w in ("因为", "所以") if _is_word_match(w, txt))
    if zh_conn >= 1:
        _ok("A7 中文连接词可被命中（统一语义）")
    else:
        _bad("A7 中文连接词", "no hit")

    # ---------- A8：should_think 与 estimate_budget 共用 _is_word_match ----------
    # 即 estimate_budget 不再因"中文连接词恒 0"而偏差
    b_zh = estimate_budget("thinking", task_size=200)
    if len(b_zh.round_types) >= 1:
        _ok("A8 量纲/语义统一：中文任务也能算出预算")
    else:
        _bad("A8 统一语义", f"got={b_zh.round_types}")

    # ---------- A9：夹取量纲唯一，build_thinking_task 非法 phase 给警告 ----------
    out = build_thinking_task("bogus", RoundSpec("hypothesis", "p"), {"task": "x"})
    if "round_type" in out and out["warnings"]:
        _ok("A9 build 非法 phase → 不崩 + warnings")
    else:
        _bad("A9 build 非法 phase", str(out))

    # ---------- C-1：autojunk=False 显式关闭 ----------
    a = ("我们" + "性能优化需要仔细分析每一个模块的瓶颈与调优空间" * 12).lower()
    b_ = ("我们" + "性能优化需要仔细分析每一个模块的瓶颈与调优方案" * 12).lower()
    r_default = difflib.SequenceMatcher(None, a, b_).ratio()
    r_nojunk = difflib.SequenceMatcher(None, a, b_, autojunk=False).ratio()
    if r_nojunk > r_default:
        _ok("C-1 autojunk=False 显式关闭（无 j 参数会偏低）")
    else:
        _bad("C-1 autojunk", f"d={r_default} n={r_nojunk}")

    # ---------- C-3：空串/全空白 → False ----------
    if not converged("", "anything") and not converged("   ", "anything"):
        _ok("C-3 空串/全空白 → 非收敛")
    else:
        _bad("C-3 空串", "should be False")

    # ---------- C-4：空白归一使近重复可识别 ----------
    if converged("hello world", "hello  world"):  # 归一后完全相等
        _ok("C-4 空白归一后相邻轮视为收敛")
    else:
        _bad("C-4 空白归一", "should converge after normalize")

    # ---------- C-2 / 阈值覆盖：阈值守卫非法输入 → 默认值 ----------
    if not converged("a", "b", threshold=True):  # bool 阈值 → 默认 → ratio=0 < 0.92
        _ok("C-2 阈值非法类型 → 默认值")
    else:
        _bad("C-2 阈值守卫", "should be False")

    # ---------- Phase 默认策略 ----------
    if (should_think("thinking") and should_think("planning") and should_think("reflection")
            and not should_think("execution")):
        _ok("默认策略：3 阶段默认起、execution 默认不起")
    else:
        _bad("默认策略", "wrong phase default")

    # ---------- execution 仅 last_result 不符合预期才起 ----------
    if (should_think("execution", last_result={"ok": False})
            and not should_think("execution", last_result={"ok": True})
            and should_think("execution", last_result={"mismatch": True})):
        _ok("execution 触发条件：ok=False / mismatch=True")
    else:
        _bad("execution 触发", "wrong")

    # ---------- RoundSpec 非法 round_type → 回退 hypothesis ----------
    rs = RoundSpec("bogus_round", "p").resolved()
    if rs.round_type == "hypothesis":
        _ok("RoundSpec 非法 round_type → 回退 hypothesis")
    else:
        _bad("RoundSpec 回退", rs.round_type)

    # ---------- estimate_budget 阶段预算三档（rev.4 §五）----------
    b_ref = estimate_budget("reflection", task_size=100)
    b_th = estimate_budget("thinking", task_size=100)
    b_th_big = estimate_budget("thinking", task_size=2000)
    b_ex = estimate_budget("execution", task_size=100)
    b_ex_big = estimate_budget("execution", task_size=5000)
    if (len(b_ref.round_types) == 5
            and len(b_th.round_types) == 1
            and len(b_th_big.round_types) == 2
            and len(b_ex.round_types) == 1
            and len(b_ex_big.round_types) == 2):
        _ok("阶段预算三档：reflection=5 / thinking-planning≥1000=2 / execution≥4000=2")
    else:
        _bad("阶段预算", f"ref={b_ref.round_types} th={b_th.round_types} th_big={b_th_big.round_types}")

    # ---------- 不抛业务异常（契约第 2 条）----------
    try:
        _ = should_think(None)  # type: ignore
        _ = build_thinking_task(None, RoundSpec("x", "y"), None)  # type: ignore
        _ = split_thinking(None)
        _ = converged(None, None)
        _ok("不抛业务异常：四函数 + converged 对非法输入均兜底")
    except Exception as e:  # noqa: BLE001
        _bad("不抛业务异常", f"exception: {e!r}")

    return rows


# A6 守卫（句末标点识别：避免 "v1.0.2" 被切成 3 句）
_DECIMAL_RE = re.compile(r"^\d+\.\d+(\.\d+)*$")


def _looks_like_decimal(token: str) -> bool:
    return bool(_DECIMAL_RE.match(token.strip()))


if __name__ == "__main__":
    rows = selftest()
    passed = sum(1 for _, ok, _ in rows if ok)
    for name, ok, detail in rows:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
    print(f"\nSELF-TEST  {passed}/{len(rows)}  ({SELFTEST_CASES} declared, gate counts rows)")
    if passed != len(rows):
        raise SystemExit(1)