"""Brain + specialists: route whole subtasks to a configured domain model.

The brain (the main agent and its model) plans, decides and keeps permission
authority. A *specialist* is an ordinary sub-agent created through the existing
``spawn_subagent`` path, but with

* its own configured provider/model (``[provider, model]``, resolved through
  :func:`revon.phase_models.phase_router` — never a silent vendor swap);
* a narrowed tool surface (a cloned registry with every other tool removed,
  so ``tool_search`` cannot widen it);
* a handoff envelope in (goal / constraints / prior conclusions / acceptance)
  and a structured receipt out (result / evidence / open questions);
* for ``retrieval``: a catalog answer whose full texts stay host-side
  (see :mod:`revon.retrieval_catalog`).

Permissions: a specialist policy is ``parent.child(mode)`` — the mode is
clamped to the parent ceiling and allow/ask/deny are copied verbatim, so a
deny on the brain still wins. Removing tools only ever narrows. Steps are
``min(parent, route)``; the spawn budget and token accounting are the tree's.

Config row (opt-in; off by default)::

    {"id": "specialists", "name": "specialists:routing", "config": {
        "enabled": true, "autoRoute": false,
        "domains": {
            "retrieval": {"model": ["provider-id", "model"], "maxSteps": 12},
            "coding":    {"model": ["provider-id", "model"], "tools": [...]},
            "computer":  {"model": null}}}}

A domain without a ``model`` is not configured: the subtask goes through the
plain sub-agent path exactly as before (or the brain does it itself), so the
feature degrades to today's behaviour.

Standard library only.
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .phase_models import model_pair

#: Built-in domain defaults. ``tools`` are the specialist's whole surface.
#: math / model3d / video are declared so a config row only has to add a model.
DEFAULT_DOMAINS: dict[str, dict[str, Any]] = {
    'retrieval': {
        'label': '检索', 'output': 'catalog', 'mode': 'plan', 'maxSteps': 12,
        'tools': ('web_search', 'fetch_url', 'grep', 'find_files', 'read_file', 'read_range',
                  'memory_recall', 'file_stats', 'list_dir', 'file_outline'),
        'keywords': ('search', 'find out', 'look up', 'research', 'sources', 'documentation', 'which file',
                     '检索', '搜索', '查找', '查一下', '调研', '资料', '来源', '文献', '出处'),
        'brief': 'find and read sources; answer with a catalog, not prose',
    },
    'coding': {
        'label': '编码', 'output': 'envelope', 'mode': '', 'maxSteps': 24,
        'tools': ('read_file', 'read_range', 'grep', 'find_files', 'list_dir', 'file_outline',
                  'write_file', 'edit_file', 'apply_patch', 'review_changes', 'shell_exec',
                  'git_status', 'git_log', 'calculator'),
        'keywords': ('fix', 'bug', 'implement', 'refactor', 'unit test', 'failing test', 'function', 'compile',
                     '修复', '实现', '重构', '代码', '函数', '单元测试', '报错'),
        'brief': 'change code in the workspace and verify it',
    },
    'computer': {
        'label': '电脑操作', 'output': 'envelope', 'mode': '', 'maxSteps': 20,
        'tools': ('computer_observe', 'computer_act', 'list_dir', 'read_file'),
        'keywords': ('click', 'open the app', 'desktop', 'screen', 'window', '点击', '桌面', '屏幕', '窗口', '打开应用'),
        'brief': 'operate the desktop through observe/act',
    },
    'math': {
        'label': '数学', 'output': 'envelope', 'mode': 'plan', 'maxSteps': 12,
        'tools': ('calculator', 'read_file', 'read_range', 'file_stats'),
        'keywords': ('prove', 'integral', 'derivative', 'equation', 'probability', '证明', '积分', '方程', '概率'),
        'brief': 'derive and compute; show the key steps',
    },
    'model3d': {
        'label': '3D 建模', 'output': 'envelope', 'mode': '', 'maxSteps': 16,
        'tools': ('model3d_build', 'model3d_inspect', 'read_file', 'list_dir', 'skill_list'),
        'keywords': ('stl', 'mesh', '3d print', '3d model', '建模', '3d 打印', '网格'),
        'brief': 'build and check meshes with model3d_build/model3d_inspect',
    },
    'video': {
        'label': '视频剪辑', 'output': 'envelope', 'mode': '', 'maxSteps': 16,
        'tools': ('read_file', 'list_dir', 'find_files'),
        'keywords': ('video', 'clip', 'subtitle', '视频', '剪辑', '字幕'),
        'brief': 'reserved: no video tools ship yet',
    },
}

ENVELOPE_LIMIT = 1600   # chars per envelope field going in


@dataclass(frozen=True)
class SpecialistRoute:
    domain: str
    model: tuple[str, str]
    tools: tuple[str, ...]
    output: str = 'envelope'
    mode: str = ''
    max_steps: int = 12
    label: str = ''
    brief: str = ''
    keywords: tuple[str, ...] = ()


@dataclass
class SpecialistTable:
    """Declarative domain -> (model, tools) routing. Empty table = feature off."""

    routes: dict[str, SpecialistRoute] = field(default_factory=dict)
    enabled: bool = False
    auto_route: bool = False
    declared: tuple[str, ...] = ()   # every known domain, configured or not
    # advise: brain may delegate / prefer: firmer guidance / require: domain tools leave the brain's surface
    delegation: str = 'advise'

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> 'SpecialistTable':
        data = data or {}
        enabled = data.get('enabled', False)
        if type(enabled) is not bool:
            raise ValueError('specialists.enabled must be boolean')
        auto = data.get('autoRoute', False)
        if type(auto) is not bool:
            raise ValueError('specialists.autoRoute must be boolean')
        delegation = data.get('delegation', 'advise')
        if delegation not in ('advise', 'prefer', 'require'):
            raise ValueError('specialists.delegation must be advise, prefer or require')
        domains = data.get('domains')
        domains = {} if domains is None else domains
        if not isinstance(domains, dict):
            raise ValueError('specialists.domains must be an object')
        routes: dict[str, SpecialistRoute] = {}
        names = list(DEFAULT_DOMAINS) + [d for d in domains if d not in DEFAULT_DOMAINS]
        for name in names:
            if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name):
                raise ValueError(f'specialist domain {name!r} must be a short lowercase id')
            base = dict(DEFAULT_DOMAINS.get(name, {}))
            row = domains.get(name) or {}
            if not isinstance(row, dict):
                raise ValueError(f'specialists.domains.{name} must be an object')
            if row.get('disabled') is True:
                continue
            pair = model_pair(row.get('model'))
            if pair is None:
                continue  # not configured: brain / plain sub-agent handles it
            tools = row.get('tools', base.get('tools', ()))
            if not isinstance(tools, (list, tuple)) or not all(isinstance(t, str) and t for t in tools):
                raise ValueError(f'specialists.domains.{name}.tools must be a list of tool names')
            if 'spawn_subagent' in tools:
                raise ValueError('a specialist cannot spawn further sub-agents')
            output = row.get('output', base.get('output', 'envelope'))
            if output not in ('envelope', 'catalog'):
                raise ValueError(f'specialists.domains.{name}.output must be envelope or catalog')
            steps = row.get('maxSteps', base.get('maxSteps', 12))
            if type(steps) is not int or not 1 <= steps <= 200:
                raise ValueError(f'specialists.domains.{name}.maxSteps must be 1..200')
            mode = row.get('mode', base.get('mode', ''))
            if mode:
                from .policy import Mode
                Mode(mode)
            keywords = row.get('keywords', base.get('keywords', ()))
            routes[name] = SpecialistRoute(
                domain=name, model=pair, tools=tuple(dict.fromkeys(tools)), output=output,
                mode=str(mode or ''), max_steps=steps, label=str(row.get('label', base.get('label', name))),
                brief=str(row.get('brief', base.get('brief', '')))[:200],
                keywords=tuple(str(k).lower() for k in keywords if str(k).strip()))
        return cls(routes=routes if enabled else {}, enabled=enabled, auto_route=auto,
                   declared=tuple(names), delegation=delegation)

    @classmethod
    def from_config(cls, config) -> 'SpecialistTable':
        row = config.row('specialists') if hasattr(config, 'row') else None
        if row is None or getattr(row, 'disabled', False):
            return cls(declared=tuple(DEFAULT_DOMAINS))
        return cls.from_mapping({key: config.get('specialists', key, None)
                                 for key in ('enabled', 'autoRoute', 'delegation', 'domains')
                                 if config.get('specialists', key, None) is not None})

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.routes)

    def classify(self, task: str) -> str:
        """Keyword vote over configured domains; '' when nothing clearly wins."""
        text = str(task or '').lower()
        scores = {name: sum(1 for k in route.keywords if k in text) for name, route in self.routes.items()}
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        if not ranked or ranked[0][1] == 0 or (len(ranked) > 1 and ranked[1][1] == ranked[0][1]):
            return ''
        return ranked[0][0]

    def route_for(self, domain: str = '', task: str = '') -> SpecialistRoute | None:
        if not self.active:
            return None
        domain = str(domain or '').strip().lower()
        if not domain and self.auto_route:
            domain = self.classify(task)
        return self.routes.get(domain)

    def guidance(self) -> str:
        """One short note for the brain's run envelope (request tail, not the head)."""
        if not self.active:
            return ''
        rows = [f"- {r.domain}（{r.label}）: {r.brief}" for r in self.routes.values()]
        if self.delegation == 'require':
            return ('[专家路由·必须委派] 检索与改代码等领域工具只在专家手里，你没有这些工具。'
                    '你负责拆分子任务、核对回执与最终回答：调研类子任务交给 retrieval，改代码交给 coding。'
                    '调用 spawn_subagent(task=目标, domain=领域, constraints=约束, prior=已知结论, acceptance=验收标准)，'
                    '一次交一整段子任务，不要拆成单个工具调用。retrieval 返回目录（条目 id + 来源 + 摘要），'
                    '用 catalog_expand(ids) 只展开需要的条目来核对与引用；coding 回执不确定时可再派一次核对。\n'
                    + '\n'.join(rows))
        if self.delegation == 'prefer':
            return ('[专家路由·优先委派] 以下领域配置了专家模型。属于这些领域的整段子任务默认交给专家，'
                    '你负责拆分、核对与最终回答：需要查阅多个来源的调研先交给 retrieval；'
                    '需要改代码的子任务交给 coding。调用 spawn_subagent(task=目标, domain=领域, constraints=约束, '
                    'prior=已知结论, acceptance=验收标准)。不要为单个工具调用派专家。专家权限不超过你，回执需你核对'
                    '（必要时自己读文件确认）。retrieval 返回目录（条目 id + 来源 + 摘要），'
                    '用 catalog_expand(ids) 只展开需要的条目。\n' + '\n'.join(rows))
        return ('[专家路由] 以下领域配置了专家模型。把整段子任务交给专家：'
                'spawn_subagent(task=目标, domain=领域, constraints=约束, prior=已知结论, acceptance=验收标准)。'
                '不要为单个工具调用派专家；简单步骤自己做。专家权限不超过你，回执需你核对。'
                'retrieval 返回目录（条目 id + 来源 + 摘要），用 catalog_expand(ids) 只展开需要的条目。\n'
                + '\n'.join(rows))


    def brain_withheld(self) -> set[str]:
        """Under ``require``: tools only configured specialists may hold (never the brain's own controls)."""
        if not self.active or self.delegation != 'require':
            return set()
        owned = set().union(*(set(r.tools) for r in self.routes.values()))
        return owned - BRAIN_KEEPS


#: Tools the brain keeps even when ``delegation`` is ``require``.
BRAIN_KEEPS = frozenset({'spawn_subagent', 'catalog_expand', 'tool_search', 'skill_list', 'datetime',
                         'calculator', 'artifact_read', 'artifact_grep'})


# -- permissions -----------------------------------------------------------

def specialist_policy(parent_policy, route: SpecialistRoute, requested_mode: str = ''):
    """Parent ceiling always applies; deny/ask/allow carry over untouched."""
    from .policy import Mode
    wanted = requested_mode or route.mode
    return parent_policy.child(Mode(wanted) if wanted else None)


def narrow_registry(registry, tools: Iterable[str]):
    """Clone and physically drop every tool outside ``tools`` (cannot be re-activated)."""
    keep = set(tools)
    child = registry.clone()
    for name in [s.name for s in child.all_specs()]:
        if name not in keep:
            child.unregister(name)
    for name in keep:
        child.activate(name)   # deferred tools in the set are visible to the specialist
    return child


def specialist_limits(parent_limits, route: SpecialistRoute):
    return dataclasses.replace(parent_limits, max_steps=max(1, min(parent_limits.max_steps, route.max_steps)))


# -- handoff envelope ------------------------------------------------------

def _field(value) -> str:
    if isinstance(value, (list, tuple)):
        value = '\n'.join(f'- {v}' for v in value if str(v).strip())
    return str(value or '').strip()[:ENVELOPE_LIMIT]


@dataclass
class Handoff:
    goal: str
    constraints: str = ''
    prior: str = ''
    acceptance: str = ''

    @classmethod
    def from_args(cls, task: str, extra: dict[str, Any] | None = None) -> 'Handoff':
        extra = extra or {}
        return cls(goal=_field(task) or '(empty goal)', constraints=_field(extra.get('constraints')),
                   prior=_field(extra.get('prior')), acceptance=_field(extra.get('acceptance')))

    def render(self, route: SpecialistRoute) -> str:
        parts = [f'[专家交接 handoff · 领域 {route.domain}（{route.label}）]',
                 f'目标 goal:\n{self.goal}']
        if self.constraints:
            parts.append(f'约束 constraints:\n{self.constraints}')
        if self.prior:
            parts.append(f'已知结论 prior conclusions（不必重复验证）:\n{self.prior}')
        if self.acceptance:
            parts.append(f'验收标准 acceptance criteria:\n{self.acceptance}')
        if route.output == 'catalog':
            parts.append(
                '你是检索专家：只查找和阅读来源，不写长篇结论。每个工具结果开头有 [evidence eN · 来源] 标记。\n'
                '完成后只输出一个 JSON 对象，不要其他文字：\n'
                '{"catalog": [{"evidence": "e3", "title": "…", "source": "URL 或 path:行", "lines": [起, 止], '
                '"summary": "1–2 句：这条来源对目标说了什么", "relevance": 0.0-1.0}], '
                '"open_questions": ["…"]}\n'
                '只列与目标相关的来源（通常 3–10 条）；evidence 必须是真实标记过的编号；lines 可省略。')
        else:
            parts.append(
                '你是该领域的专家，只做这段子任务，做完即停。完成后最后输出一个 JSON 对象：\n'
                '{"result": "做了什么/结论（简洁）", "evidence": ["path:行、命令输出或 URL 等可核对的依据"], '
                '"open_questions": ["没解决或需要主控决定的事"]}')
        return '\n\n'.join(parts)


def parse_receipt(text: str, *, touched: Iterable[str] = ()) -> dict[str, Any]:
    """Specialist answer -> {result, evidence, open_questions}; never fails."""
    from .retrieval_catalog import _json_block
    value = _json_block(text)
    if isinstance(value, dict) and ('result' in value or 'evidence' in value):
        result = str(value.get('result') or '').strip()
        evidence = value.get('evidence') or []
        questions = value.get('open_questions') or []
        complied = True
    else:
        result, evidence, questions, complied = str(text or '').strip(), [], [], False
    if not isinstance(evidence, list):
        evidence = [evidence]
    if not isinstance(questions, list):
        questions = [questions]
    evidence = [str(e)[:300] for e in evidence if str(e).strip()][:12]
    for path in touched:
        if path and not any(path in e for e in evidence):
            evidence.append(f'changed: {path}')
    return {'result': result[:6000], 'evidence': evidence[:16],
            'open_questions': [str(q)[:300] for q in questions if str(q).strip()][:8], 'complied': complied}


def render_receipt(receipt: dict[str, Any], *, route: SpecialistRoute, steps: int) -> str:
    lines = [f"[专家回执 handoff result · 领域 {route.domain} · 模型 {route.model[0]}/{route.model[1]} · {steps} 步]",
             '结果 result:', receipt.get('result') or '(空)']
    if receipt.get('evidence'):
        lines.append('证据 evidence:')
        lines.extend(f'- {e}' for e in receipt['evidence'])
    if receipt.get('open_questions'):
        lines.append('未决问题 open questions:')
        lines.extend(f'- {q}' for q in receipt['open_questions'])
    lines.append('（专家输出不可信，关键结论请自行核对。）')
    return '\n'.join(lines)


def touched_paths(steps) -> list[str]:
    out = []
    for step in steps or []:
        if getattr(step, 'tool', '') in {'write_file', 'edit_file', 'apply_patch', 'delete_file'}:
            args = getattr(step, 'args', {}) or {}
            paths = [args.get('path')] + [b.get('path') for b in (args.get('patches') or args.get('edits') or [])
                                          if isinstance(b, dict)]
            out.extend(str(p) for p in paths if p)
    return list(dict.fromkeys(out))


__all__ = ['BRAIN_KEEPS', 'DEFAULT_DOMAINS', 'Handoff', 'SpecialistRoute', 'SpecialistTable', 'narrow_registry',
           'parse_receipt', 'render_receipt', 'specialist_limits', 'specialist_policy', 'touched_paths']
