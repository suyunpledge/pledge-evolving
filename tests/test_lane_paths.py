# -*- coding: utf-8 -*-
"""
测试目标：serve 的 session 落盘映射（NOT 网络行为）
样本来源：session key 由 session_for() 产出，形如
          channel:weixin:<account>:<peer>——含冒号，不能直接当文件名
期望行为：
  - key 里的非法文件名字符被替换，不抛异常
  - 不同 key 落到不同文件（群聊与私聊同 peer 也不撞车）
  - 超长 key 被截断
  - 映射是确定性的（同一 key 永远同一文件）
边界说明：直接调用生产代码 lane_session_path，不复制其逻辑。
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"C:\Users\匡溯昀\pledge-evolving")

from forge.channels import InboundMessage, session_for
from forge.channels.cli import lane_session_path

with tempfile.TemporaryDirectory() as td:
    sd = Path(td)

    # 冒号（Windows 非法字符）被替换
    key = "channel:weixin:acct:user1"
    p = lane_session_path(sd, key)
    assert ":" not in p.name, p.name
    assert p.name.endswith(".jsonl")
    assert p.parent == sd

    # 不同会话 -> 不同文件
    k1 = session_for(InboundMessage("weixin", "acct", "u1", "x"), "per-peer")
    k2 = session_for(InboundMessage("weixin", "acct", "u2", "x"), "per-peer")
    k3 = session_for(InboundMessage("weixin", "acct", "u1", "x", group="g"), "per-peer")
    names = {lane_session_path(sd, k).name for k in (k1, k2, k3)}
    assert len(names) == 3, f"distinct keys must map to distinct files: {names}"

    # 群聊与私聊即使 peer 相同也不撞车
    assert lane_session_path(sd, k1).name != lane_session_path(sd, k3).name

    # 确定性：同一 key 两次调用一致
    assert lane_session_path(sd, k1) == lane_session_path(sd, k1)

    # 超长 key 被截断
    long_key = "channel:weixin:acct:" + ("z" * 500)
    p2 = lane_session_path(sd, long_key)
    assert len(p2.name) <= 126, len(p2.name)
    assert p2.name.endswith(".jsonl")

    # 空 key 不崩
    assert lane_session_path(sd, "").name == ".jsonl"

print("lane path mapping: OK")

# Windows 保留名不会被当成裸文件名（前缀保证）
for reserved in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT1"):
    name = lane_session_path(Path("."), f"channel:weixin:acct:{reserved}").name
    assert name != reserved, name
    assert name.endswith(".jsonl")
print("windows reserved names: OK")

print("\nAll lane-path checks passed")
