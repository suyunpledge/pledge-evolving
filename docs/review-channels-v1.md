# WeChat IM Channel 代码审查报告 v1

**提交：** `edf649f`  
**范围：** `forge/channels/__init__.py`、`forge/channels/weixin.py`、`forge/channels/cli.py`、`tests/test_channels_opt.py`  
**重点问题：A – E**

---

## A. `_redact` 覆盖完整性

### 结论：有第 5 条路径，是 🟡 建议项

`_redact` 当前被调用的 4 处：`weixin.py:264`（HTTPError）、`:272`（URLError/ConnectionError）、`:278`（JSONDecodeError）、`:484`（status 异常）。

**第 5 条路径：`weixin.py:317` — `RuntimeError` 里的 `detail`**

```python
# weixin.py:315-318
ret = resp.get("ret")
errcode = resp.get("errcode")
if ret not in (None, 0) or errcode not in (None, 0):
    detail = resp.get("errmsg") or ""          # ← 未经过 _redact
    raise RuntimeError(
        f"getupdates failed ret={ret} errcode={errcode}: {detail}"
    )
```

`errmsg` 来自 API 响应体，实际概率极低包含 token，属于设计一致性问题，不影响安全。

**修改：**
```python
detail = _redact(resp.get("errmsg") or "", self._token)
```

---

## B. `agents` 字典 — 是否加 LRU

### 结论：**应该加，上限 128，🟡 P1**

```python
# cli.py:~line 195
agents: dict[str, object] = {}
```

每个 lane 一个 agent，存在字典里永不驱逐。

**每个 agent 内存量级：**
- `LoopLimits`（3 个 int）≈ 72 B
- Agent 内部：最近 N 轮对话上下文（每轮 1–8k token）

**建议：**
```python
from collections import OrderedDict

class _LaneCache:
    def __init__(self, max_size: int = 128):
        self._max = max_size
        self._od: OrderedDict[str, object] = OrderedDict()
    def get(self, key):
        if key in self._od:
            self._od.move_to_end(key)
            return self._od[key]
        return None
    def put(self, key, value):
        if key in self._od: self._od.move_to_end(key)
        self._od[key] = value
        if len(self._od) > self._max:
            self._od.popitem(last=False)

agents = _LaneCache(max_size=128)
```

---

## C. `chunk_text` UTF-8 边界

### 结论：**无切坏风险，🟢 安全**

Python `str[:limit]` 按 Unicode codepoint 索引，不按 UTF-8 字节。emoji 和中文不会被截断到半个码点。不需要改。

---

## D. 锁文件 `_pid_alive` 误判场景

### 结论：**三种误判，🟡 P1 不阻塞**

```python
def _pid_alive(pid: int) -> bool:
    # Windows: tasklist 查 PID
    # POSIX: os.kill(pid, 0)
    except PermissionError: return True   # ← 误判
    except Exception: return True          # ← tasklist 失败时默认 alive
```

**误判 1：PID 快速回收**  
进程崩溃 → 新进程复用同一 PID → 锁文件误判"旧持有者还活着"→ 合法启动被拒绝。

**误判 2：跨用户 PID**  
Unix 上 `os.kill(pid, 0)` 对他人 PID 抛 `PermissionError`，保守返回 True → 锁永远不可回收。

**误判 3：`tasklist` 不可用（WSL/精简环境）**  
`subprocess.run` 超时或命令不存在 → 默认 alive → 锁死。

**建议：** 在锁文件中写入 PID + 进程启动时间戳；检查时同时对比两者，从根本上解决 PID 复用误判。或改用 `ctypes.windll.kernel32.OpenProcess` + `GetExitCodeProcess` 判断 Windows 进程存活。

---

## E. `bundles/base.json` 档位命名

### 结论：**`mimo`→`lite` 替换正确，但有 2 处遗漏需处理，🔴 P0（需验证）**

当前 `base.json` 路由层已无旧名称：

```json
"routing": {
  "tiers": [["lite","claude-sonnet-5"], ["medium","deepseek-flash"]],
  "premium": [["premium","claude-opus-5"]]
}
```

**遗漏 1（最高风险）：`forge/routing.py` 或 `model.py` 硬编码旧名称**

如果路由器 `class Router`、`looks_complex_heuristic` 或 tier 解析逻辑中有 `"mimo"` 或 `"deepseek"` 字符串，`tier not found` 会被抛出，路由降级失败。

**需要执行：**
```
grep -rn "mimo\|deepseek" forge/ --include="*.py"
```

**遗漏 2（低风险）：`notes` 字段未同步更新**  
`base.json` 两处 `notes` 仍描述旧档位语义，文档已过时。

---

## 汇总

| 编号 | 问题 | 阻塞 | 位置 |
|------|------|------|------|
| **A** | `_redact` 未覆盖 `errmsg` | 🟡 建议 | `weixin.py:317` |
| **B** | `agents` 无 LRU 上限 | 🟡 P1 | `cli.py:~195` |
| **C** | `chunk_text` 逐字符切分 | 🟢 安全 | `__init__.py:115` |
| **D** | `_pid_alive` 三种误判 | 🟡 P1 | `cli.py:260-279` |
| **E** | `bundles/base.json` 改名后 `routing.py`/`model.py` 可能残留旧名称硬编码 | 🔴 P0（待 grep 验证） | `forge/` |

**最优先操作：**
1. 验证 E：`grep -rn "mimo\|deepseek" forge/ --include="*.py"`
2. 修复 A：`weixin.py:317` 加 `_redact` 包裹 `errmsg`
3. 修复 B：`agents: dict` → `_LaneCache(max_size=128)`
