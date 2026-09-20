# -*- coding: utf-8 -*-
"""
测试目标：channel serve/transport 层的优化行为（NOT 网络行为）
样本来源：
  - PollTimeout：来自真实 API 探测——长轮询窗口到期会 socket read timeout，
    这是正常"没消息"而非故障
  - `ret` 检查：iLink 响应封套同时带 ret 与 errcode，两者都要看
  - 自适应轮询窗口：服务端在响应里回传 longpolling_timeout_ms
期望行为：
  - 超时返回空批次，不抛异常（旧实现会抛，导致空闲时刷错误日志）
  - 非零 ret 必须报错，不能当成"空轮询"静默吞掉消息
  - 服务端建议的窗口被采纳并被钳制在合理范围内
  - send() 返回实际投递的分片数
  - 锁文件：同账号二次启动被拒绝；进程死后锁可回收
边界说明：不发起网络请求，全部用替身 _post。
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"C:\Users\匡溯昀\pledge-evolving")

from forge.channels.cli import _acquire_lock, _release_lock, _pid_alive, _ServeStats
from forge.channels.weixin import PollTimeout, WeixinChannel

# ── 1. a poll timeout is "nothing yet", not an error ────────────────────

state = tempfile.mkdtemp()
ch = WeixinChannel({"enabled": True, "token": "***", "stateDir": state})


def raise_timeout(endpoint, body, timeout_ms):
    raise PollTimeout("read timeout")


ch._post = raise_timeout  # type: ignore
assert ch.poll() == [], "timeout must yield an empty batch"
print("1. PollTimeout -> empty batch: OK")

# ── 2. a nonzero ret must be surfaced, not swallowed ────────────────────

ch._post = lambda e, b, t: {"ret": 0, "msgs": []}  # type: ignore
assert ch.poll() == [], "ret=0 with no messages is a clean empty poll"

ch._post = lambda e, b, t: {"ret": 1002, "errmsg": "rate limited"}  # type: ignore
try:
    ch.poll()
    raise AssertionError("nonzero ret must raise")
except RuntimeError as exc:
    assert "1002" in str(exc), exc
print("2. nonzero ret raises: OK")

# errcode alone (no ret) is also caught
ch._post = lambda e, b, t: {"errcode": -14, "errmsg": "session timeout"}  # type: ignore
try:
    ch.poll()
    raise AssertionError("errcode must raise")
except RuntimeError as exc:
    assert "-14" in str(exc), exc
print("3. nonzero errcode raises: OK")

# ── 3. adaptive long-poll window, clamped ───────────────────────────────

ch2 = WeixinChannel({"enabled": True, "token": "***", "stateDir": tempfile.mkdtemp()})
ch2._post = lambda e, b, t: {  # type: ignore
    "ret": 0, "msgs": [], "get_updates_buf": "buf1", "longpolling_timeout_ms": 45_000,
}
ch2.poll()
assert ch2.cfg.poll_timeout_ms == 45_000, ch2.cfg.poll_timeout_ms

# absurd suggestions are clamped
for suggested, expect in ((1, ch2.MIN_POLL_MS), (10**9, ch2.MAX_POLL_MS)):
    ch3 = WeixinChannel({"enabled": True, "token": "***", "stateDir": tempfile.mkdtemp()})
    ch3._post = lambda e, b, t, s=suggested: {  # type: ignore
        "ret": 0, "msgs": [], "longpolling_timeout_ms": s,
    }
    ch3.poll()
    assert ch3.cfg.poll_timeout_ms == expect, (suggested, ch3.cfg.poll_timeout_ms)
print("4. adaptive poll window (clamped): OK")

# ── 4. cursor advances, and only on a good batch ────────────────────────

ch4 = WeixinChannel({"enabled": True, "token": "***", "stateDir": tempfile.mkdtemp()})
ch4._post = lambda e, b, t: {"ret": 0, "msgs": [], "get_updates_buf": "cursor-X"}  # type: ignore
ch4.poll()
assert ch4._cursor.get("weixin:default") == "cursor-X"

ch5 = WeixinChannel({"enabled": True, "token": "***", "stateDir": tempfile.mkdtemp()})
ch5._post = lambda e, b, t: {"ret": 500, "errmsg": "boom"}  # type: ignore
try:
    ch5.poll()
except RuntimeError:
    pass
assert ch5._cursor.get("weixin:default") == "", "cursor must not advance on failure"
print("5. cursor advances only on success: OK")

# ── 5. dedup across polls ───────────────────────────────────────────────

msg = {
    "message_type": 1,
    "from_user_id": "u1",
    "message_id": 777,
    "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
}
ch6 = WeixinChannel({"enabled": True, "token": "***", "stateDir": tempfile.mkdtemp()})
ch6._post = lambda e, b, t: {"ret": 0, "msgs": [msg]}  # type: ignore
first = ch6.poll()
second = ch6.poll()
assert len(first) == 1, first
assert second == [], "the same msg_id must not be delivered twice"
print("6. msg_id dedup across polls: OK")

# ── 6. send() reports the chunk count ───────────────────────────────────

sent = []
ch7 = WeixinChannel({"enabled": True, "token": "***", "stateDir": tempfile.mkdtemp()})
ch7._post = lambda e, b, t: (sent.append(b) or {})  # type: ignore

assert ch7.send("u1", "short") == 1
assert ch7.send("u1", "") == 0, "empty text yields no chunks"

ch7.cfg.chunk_limit = 50
n = ch7.send("u1", "z" * 120)
assert n == 3, f"expected 3 chunks, got {n}"
print("7. send() returns chunk count: OK")

# ── 7. serve lock ───────────────────────────────────────────────────────

with tempfile.TemporaryDirectory() as td:
    lock_path = Path(td) / "acct.serve.lock"

    held = _acquire_lock(lock_path)
    assert held is not None
    assert _pid_alive(os.getpid())

    # second acquire in the same (live) process is refused
    assert _acquire_lock(lock_path) is None, "live holder must block a second serve"

    _release_lock(held)
    assert not lock_path.exists()

    # a stale lock from a dead pid is reclaimable
    lock_path.write_text("999999999", encoding="utf-8")
    assert _acquire_lock(lock_path) is not None, "dead holder's lock must be reclaimable"
    _release_lock(lock_path)

    # a corrupt lock file is treated as stale, not fatal
    lock_path.write_text("not-a-pid", encoding="utf-8")
    assert _acquire_lock(lock_path) is not None
    _release_lock(lock_path)
print("8. serve lock: OK")

# ── 8. stats bookkeeping ────────────────────────────────────────────────

st = _ServeStats()
st.polls = 3
st.handled = 2
st.sent = 5
summary = st.summary()
assert "2 message(s) handled" in summary
assert "5 chunk(s) sent" in summary
assert st.idle_seconds() >= 0
print("9. serve stats: OK")

# ── 9b. seen-set batching: one write per batch, not per message ─────────

with tempfile.TemporaryDirectory() as td:
    spath = Path(td) / "seen.json"
    from forge.channels import SeenSet

    s = SeenSet(spath, max_items=100)
    writes = []
    real_save = s.save

    def counting_save():
        writes.append(1)
        real_save()

    s.save = counting_save  # type: ignore

    # deferred adds do not touch the disk
    for i in range(5):
        s.add(f"b{i}", save=False)
    assert writes == [], f"save=False must not write: {len(writes)} write(s)"
    assert s.seen("b4"), "deferred adds are still visible in memory"

    # an explicit flush persists the whole batch in one write
    s.save()
    assert len(writes) == 1, f"expected 1 write, got {len(writes)}"
    assert SeenSet(spath, max_items=100).seen("b4"), "batch must persist"

    # default add() still writes immediately (no behaviour change for callers)
    writes.clear()
    s.add("single")
    assert len(writes) == 1, "default add must still persist immediately"
print("10. seen-set batching: OK")

print("\nAll 10 optimisation tests passed")
