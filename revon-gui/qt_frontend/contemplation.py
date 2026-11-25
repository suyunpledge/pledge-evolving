"""Qt 对话里的沉思：回答前先跑几轮「假设 → 批判 → 收敛」，笔记注入本轮系统上下文。

沉思套件走与 ``revon run`` 相同的插件席位（``mount_contrib_extensions`` 的
``thinking`` 席位：内置 ``revon/contrib/thinking.py``，``<home>/contrib/thinking.py``
过闸后可接管）——一切皆插件，Qt 不再自带一份沉思逻辑。

档位语义：
* 「沉思」（task_thinking == on）：每条消息都沉思；轮型取套件预算，至少「假设 + 批判」，
  末轮收敛，相邻两轮相似即提前停；
* 其它档：对话不沉思（智能沉思只用于 revon 任务，避免普通聊天平白多出几次调用）。

笔记只进当轮请求的系统上下文，不写入对话历史：续聊不会把旧沉思反复计费。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from revon_client import ChatMessage, GenerationCancelled

ROUND_LABELS = {'hypothesis': '假设', 'critic': '批判', 'counterfactual': '反事实',
                'cross_check': '交叉验证', 'converge': '收敛'}
MAX_NOTES = 4000
_SUITE_CACHE: dict[str, object] = {}


@dataclass
class Contemplation:
    notes: str = ''
    rounds: list[str] = field(default_factory=list)
    stopped: str = ''

    def summary(self):
        if not self.rounds:
            return {}
        return {'rounds': len(self.rounds), 'stopped': self.stopped,
                'label': '沉思 · ' + ' → '.join(ROUND_LABELS.get(r, r) for r in self.rounds)
                         + (' · 已收敛' if self.stopped == 'converged' else ''),
                'notes': self.notes[:MAX_NOTES]}

    def context(self):
        if not self.notes:
            return ''
        return ('<contemplation>\n' + self.notes[:MAX_NOTES] + '\n</contemplation>\n'
                '以上是你回答前的沉思笔记：据此给出最终回答，纠正其中被批判指出的问题，不要逐条复述笔记。')


def thinking_suite(home):
    """The mounted thinking seat (cached per home), or None when no module passes the gate."""
    key = str(home)
    if key not in _SUITE_CACHE:
        try:
            from revon.extensions import mount_contrib_extensions
            from pathlib import Path
            _SUITE_CACHE[key] = mount_contrib_extensions(home=Path(home)).get('thinking')
        except Exception:
            _SUITE_CACHE[key] = None
    return _SUITE_CACHE[key]


def round_types(suite, task, explicit):
    budget = suite.estimate_budget('thinking', task_size=len(task), history_len=0)
    rounds = list(getattr(budget, 'round_types', ()) or ())
    if explicit:
        for needed in ('hypothesis', 'critic'):
            if needed not in rounds:
                rounds.append(needed)
        rounds.append('converge')
    return list(dict.fromkeys(rounds))


def contemplate(client, suite, task, *, model, effort, temperature, cancel, status, explicit=True):
    """Run the rounds through the same gateway/model as the answer. Never raises except on cancel."""
    result = Contemplation()
    if suite is None or not str(task or '').strip():
        return result
    try:
        if suite.should_think('thinking', budget=None, last_result=None) is not True:
            return result
        plan = round_types(suite, task, explicit)
    except Exception:
        return result
    spec_cls = getattr(suite, 'round_spec', None)
    texts, prev = [], ''
    result.stopped = 'budget'
    for index, kind in enumerate(plan, 1):
        if cancel.is_set():
            raise GenerationCancelled('已停止生成')
        status(f'沉思中 · {ROUND_LABELS.get(kind, kind)}（{index}/{len(plan)}）')
        try:
            spec = spec_cls(round_type=kind, prompt='', role='') if callable(spec_cls) else {'round_type': kind}
            row = suite.build_thinking_task('thinking', spec, {'task': task, 'prior_outputs': list(texts)})
            messages = [ChatMessage(m['role'], m['content']) for m in (row or {}).get('messages') or []]
            if not messages:
                break
            reply = client.stream_chat(messages, model=model, reasoning_effort=effort,
                                       temperature=temperature, cancel_event=cancel)
            thinking, answer = suite.split_thinking(getattr(reply, 'text', '') or '')
        except GenerationCancelled:
            raise
        except Exception:
            result.stopped = 'error'
            break
        produced = (answer or thinking or '').strip()
        if produced:
            texts.append(produced)
            result.rounds.append(kind)
        converged = getattr(suite, 'converged', None)
        if prev and produced and callable(converged) and converged(prev, produced) is True:
            result.stopped = 'converged'
            break
        prev = produced or prev
    result.notes = '\n\n'.join(texts).strip()
    return result
