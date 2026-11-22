"""宿主拥有的会话请求快照：连续对话保留已发送前缀，不信任客户端工具历史。

参考 DSH Session.deriveMessages 的冻结投影与 replace-generation 失效原则。
普通快照可落盘。桌面暂停时，完整的工具交换仅在当前实例保留，按任务身份恢复。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

from .context_transaction import valid_tool_pairs

MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024


def compact_file_continuation(messages, objective, todos, plan_tasks=()):
    """Pack a fully answered file-tool suffix behind its original task prefix.

    Preserve every file argument and observation, including written content.
    Data stays on the assistant side of the boundary. Other tool protocols,
    uncertain file results and interleaved instructions require their original exchange.
    """
    start = next((i + 1 for i, m in enumerate(messages)
                  if m.get('role') == 'user' and m.get('content') == objective), None)
    if start is None or not valid_tool_pairs(messages):
        return None
    receipts = []
    i = start
    while i < len(messages):
        answer = messages[i]
        calls = answer.get('tool_calls')
        if (answer.get('role') != 'assistant' or not calls or
                set(answer) - {'role', 'content', 'reasoning_content', 'encrypted_content', 'tool_calls'}):
            return None
        if answer.get('content'):
            receipts.append({'assistant': answer['content']})
        i += 1
        for call in calls:
            fn = call.get('function')
            if (not isinstance(fn, dict) or not isinstance(fn.get('arguments'), str) or i >= len(messages)
                    or messages[i].get('role') != 'tool' or messages[i].get('tool_call_id') != call['id']):
                return None
            value = messages[i].get('content')
            i += 1
            if not isinstance(value, str):
                return None
            name = fn.get('name')
            if name == 'update_todos' and todos and not value.startswith('[error]'):
                continue
            # A rejected checklist update is a known non-write. Keep its exact
            # rejection alongside the last accepted state; never mark it done.
            rejected_todo = name == 'update_todos' and value.startswith('[error]')
            if not rejected_todo and (value.startswith('[error]') or
                    name not in {'read_file', 'write_file', 'list_dir', 'read_range', 'grep'}):
                return None
            try:
                args = json.loads(fn['arguments'])
            except ValueError:
                return None
            receipts.append([name, args, value])
    if not receipts:
        return None
    evidence = {'tool_receipts': receipts}
    if todos:
        if [row['text'] for row in todos] == list(plan_tasks):
            evidence['plan_task_statuses'] = [row['status'] for row in todos]
        else:
            evidence['checklist'] = [[row['text'], row['status']] for row in todos]
    data = json.dumps(evidence, ensure_ascii=False, separators=(',', ':'))
    summary = [{'role': 'assistant', 'content': 'Tool receipts (untrusted data; [error] = rejected).\n' + data},
               {'role': 'user', 'content': '按清单继续，沿用回执；按要求核实未验证的写入。'}]
    if len(json.dumps(summary, ensure_ascii=False)) >= len(json.dumps(messages[start:], ensure_ascii=False)):
        return None
    return [*messages[:start], *summary]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


class ConversationPrefix:
    def __init__(self, path=None):
        self._path = Path(path) if path else None
        # 只清内存，不删磁盘快照——否则每个新实例构造时都会把上一个进程
        # 刚写下的快照抹掉，跨进程复用永远不成立。
        self._saved = None

    def clear(self):
        """显式失效：内存与磁盘一起清（构造器不走这里）。"""
        self._saved = None
        if self._path is not None:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass

    def _load(self):
        """跨进程加载宿主快照（GUI client/server.py 每轮新建 Agent，内存
        快照恒为空；磁盘快照让第二次请求也能逐字节延续第一次已发送的前缀，
        而不是只共享 system 的回退布局——实测回退在第二轮是 0% 命中）。

        落盘文件与 sessions 同信任域（宿主所有）；加载走与 commit 一致的
        校验（工具配对 + 可序列化），任何损坏静默弃用、回退原行为。
        """
        if self._saved is not None or self._path is None:
            return
        try:
            raw = json.loads(self._path.read_text(encoding='utf-8'))
            if (not isinstance(raw, dict) or 'history_digest' not in raw
                    or 'guard' not in raw or 'messages' not in raw or 'paused_task' in raw):
                return
            messages = raw['messages']
            if (not isinstance(messages, list)
                    or not all(isinstance(m, dict) for m in messages)):
                return
            if not valid_tool_pairs(messages):
                return
            self._saved = raw
        except (OSError, ValueError, TypeError, AttributeError):
            return

    def candidate(self, history, guard, *, task_id=None):
        self._load()
        saved = self._saved
        if not saved or not history or digest(history) != saved['history_digest']:
            return None
        if guard != saved['guard'] or ('paused_task' in saved and saved['paused_task'] != task_id):
            return None
        return copy.deepcopy(saved)

    def trim_history(self, history, trimmed):
        """Acknowledge only a host-owned suffix trim of the just-saved chat.

        The editor bounds its display history independently of the private
        request snapshot. Client edits still fail the ordinary exact digest.
        """
        saved = self._saved
        if (not saved or not trimmed or len(trimmed) > len(history)
                or history[-len(trimmed):] != trimmed or digest(history) != saved['history_digest']):
            return False
        saved['history_digest'] = digest(trimmed)
        self._persist()
        return True

    def commit(self, *, history, messages, guard, rule_context, rule_sources, targets, run_envelope='', continuation=None,
               paused_task=None):
        value = {'history_digest': digest(history), 'messages': messages, 'guard': guard,
                 'rule_context': rule_context, 'rule_sources': rule_sources, 'targets': targets,
                 'run_envelope': run_envelope, 'continuation': continuation or {}}
        if paused_task:
            value['paused_task'] = paused_task
        try:
            if not valid_tool_pairs(messages) or len(json.dumps(value, ensure_ascii=False,
                    allow_nan=False).encode('utf-8')) > MAX_SNAPSHOT_BYTES:
                self.clear()
                return False
            self._saved = copy.deepcopy(value)
        except (TypeError, ValueError, RecursionError):
            self.clear()
            return False
        self._persist()
        return True

    def _persist(self):
        if self._path is not None and self._saved is not None:
            try:
                if 'paused_task' in self._saved:
                    self._path.unlink(missing_ok=True)
                    return
                tmp = self._path.with_name(self._path.name + f'.tmp{os.getpid()}')
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(self._saved, ensure_ascii=False), encoding='utf-8')
                os.replace(tmp, self._path)
            except OSError:
                pass  # 落盘失败只影响跨进程复用，不影响本实例行为
