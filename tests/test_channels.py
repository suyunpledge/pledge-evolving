# -*- coding: utf-8 -*-
"""
测试目标：channel 层的纯逻辑 + 微信协议的解析行为（NOT 网络行为）
样本来源：
  - 协议字段：从 OpenClaw weixin 插件的类型定义与实测 blob 对照
  - 消息样本：按 iLink bot API 的 WeixinMessage 结构手工构造
期望行为：
  - 纯函数（session_for / chunk_text）可离线验证
  - Cursor / SeenSet 持久化正确，损坏时降级为空而不是崩
  - 微信通道解析：过滤机器人回声、无发送者、空文本；提取文本与语音转写
  - 凭据解析优先级：env > file > inline
边界说明：本文件不发起任何网络请求；`WeixinChannel.__init__` 只做配置解析，
          真实的 getupdates/sendmessage 由 `channel weixin serve` 承担。
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"C:\Users\匡溯昀\pledge-evolving")

from forge.channels import (
    Cursor,
    InboundMessage,
    SeenSet,
    chunk_text,
    load_channel,
    available_channels,
    session_for,
)
from forge.channels.weixin import (
    WeixinChannel,
    WeixinConfig,
    _client_version,
    import_openclaw_account,
)

# ── 1. session mapping ──────────────────────────────────────────────────

dm = InboundMessage(channel="weixin", account="acct", peer="user1", text="hi")
grp = InboundMessage(channel="weixin", account="acct", peer="user1", text="hi", group="g1")

assert session_for(dm, "shared") == "channel:weixin"
assert session_for(dm, "per-peer") == "channel:weixin:acct:user1"
assert session_for(grp, "per-peer") == "channel:weixin:acct:g1"
assert session_for(grp, "per-account-channel-peer") == "channel:weixin:acct:g1:user1"
# unknown scope falls back to per-peer rather than raising
assert session_for(dm, "nonsense") == "channel:weixin:acct:user1"
print("1. session_for scopes: OK")

# ── 2. chunking ─────────────────────────────────────────────────────────

assert chunk_text("", 100) == []
assert chunk_text("short", 100) == ["short"]
assert chunk_text("x" * 100, 100) == ["x" * 100]

# paragraph-preferred split
body = ("a" * 60) + "\n\n" + ("b" * 60)
parts = chunk_text(body, 70)
assert len(parts) == 2, parts
assert parts[0].startswith("a") and parts[1].startswith("b")

# hard split when no break exists
hard = chunk_text("z" * 250, 100)
assert len(hard) == 3
assert all(len(p) <= 100 for p in hard)
assert "".join(hard) == "z" * 250

# newline preferred over space
nl = chunk_text(("a" * 40) + "\n" + ("b" * 40), 50)
assert len(nl) == 2

try:
    chunk_text("x", 0)
    raise AssertionError("limit=0 must raise")
except ValueError:
    pass
print("2. chunk_text: OK")

# ── 3. Cursor persistence + corrupt recovery ────────────────────────────

with tempfile.TemporaryDirectory() as td:
    cpath = Path(td) / "c.json"
    c = Cursor(cpath)
    assert c.get("k") == ""
    c.set("k", "v1")
    assert Cursor(cpath).get("k") == "v1", "cursor must persist"

    cpath.write_text("{not json", encoding="utf-8")
    assert Cursor(cpath).get("k") == "", "corrupt cursor must degrade to empty"
print("3. Cursor: OK")

# ── 4. SeenSet dedup + eviction ─────────────────────────────────────────

with tempfile.TemporaryDirectory() as td:
    spath = Path(td) / "s.json"
    s = SeenSet(spath, max_items=3)
    assert not s.seen("m1")
    s.add("m1")
    assert s.seen("m1")
    s.add("m1")  # idempotent
    s.add("m2"); s.add("m3"); s.add("m4")  # evicts m1
    assert not s.seen("m1"), "oldest must be evicted at cap"
    assert s.seen("m4")
    assert SeenSet(spath, max_items=3).seen("m4"), "seen-set must persist"

    spath.write_text("broken", encoding="utf-8")
    assert not SeenSet(spath).seen("m4"), "corrupt seen-set must degrade"
print("4. SeenSet: OK")

# ── 5. credential resolution precedence ─────────────────────────────────

with tempfile.TemporaryDirectory() as td:
    cred = Path(td) / "cred.json"
    cred.write_text(json.dumps({"token": "from-file"}), encoding="utf-8")
    import os

    os.environ["FORGE_TEST_WX_TOKEN"] = "from-env"
    try:
        # env wins
        cfg = WeixinConfig(token_env="FORGE_TEST_WX_TOKEN", token_file=str(cred), token="inline")
        assert cfg.resolve_token() == "from-env"
        # file wins over inline when env is empty
        cfg2 = WeixinConfig(token_env="FORGE_TEST_WX_TOKEN_UNSET", token_file=str(cred), token="inline")
        assert cfg2.resolve_token() == "from-file"
        # inline is the last resort
        cfg3 = WeixinConfig(token="inline")
        assert cfg3.resolve_token() == "inline"
    finally:
        os.environ.pop("FORGE_TEST_WX_TOKEN", None)

    # no source at all must raise, not silently yield ""
    try:
        WeixinConfig().resolve_token()
        raise AssertionError("missing token must raise")
    except RuntimeError:
        pass
print("5. token resolution: OK")

# ── 6. client version encoding ──────────────────────────────────────────

assert _client_version("2.4.3") == (2 << 16) | (4 << 8) | 3
assert _client_version("0.0.0") == 0
assert _client_version("1.0.11") == (1 << 16) | 11
assert _client_version("bad") == 0
assert _client_version("3") == 3 << 16
print("6. _client_version: OK")

# ── 7. channel construction + registry ──────────────────────────────────

assert "weixin" in available_channels()
ch = load_channel("weixin", {"enabled": True, "token": "t", "stateDir": tempfile.mkdtemp()})
assert isinstance(ch, WeixinChannel)
assert ch.name == "weixin"

try:
    load_channel("nope", {})
    raise AssertionError("unknown channel must raise KeyError")
except KeyError:
    pass
print("7. registry: OK")

# ── 8. inbound normalisation ────────────────────────────────────────────

state = tempfile.mkdtemp()
ch = WeixinChannel({"enabled": True, "token": "t", "stateDir": state})

# user text message
text_msg = {
    "message_type": 1,
    "from_user_id": "u1",
    "message_id": 101,
    "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
}
m = ch._normalise(text_msg)
assert m is not None and m.text == "hello" and m.peer == "u1" and m.msg_id == "101"

# bot echo must be dropped
assert ch._normalise({**text_msg, "message_type": 2}) is None

# no sender must be dropped
assert ch._normalise({**text_msg, "from_user_id": ""}) is None

# textless must be dropped
assert ch._normalise({**text_msg, "item_list": []}) is None

# voice uses the server transcription
voice = {
    "message_type": 1,
    "from_user_id": "u1",
    "message_id": 102,
    "item_list": [{"type": 3, "voice_item": {"text": "语音转写"}}],
}
assert ch._normalise(voice).text == "语音转写"

# media-only becomes a placeholder so the loop can answer instead of hanging
img = {
    "message_type": 1,
    "from_user_id": "u1",
    "message_id": 103,
    "item_list": [{"type": 2, "image_item": {}}],
}
assert ch._normalise(img).text == "[图片]"

# group id is carried through
g = ch._normalise({**text_msg, "group_id": "g9"})
assert g.group == "g9" and g.is_group

# mixed items concatenate in order
mixed = {
    "message_type": 1,
    "from_user_id": "u1",
    "message_id": 104,
    "item_list": [
        {"type": 1, "text_item": {"text": "看这个"}},
        {"type": 2, "image_item": {}},
    ],
}
assert ch._normalise(mixed).text == "看这个[图片]"
print("8. inbound normalisation: OK")

# ── 9. outbound body shape ──────────────────────────────────────────────

sent: list[tuple[str, dict]] = []
ch2 = WeixinChannel({"enabled": True, "token": "t", "stateDir": tempfile.mkdtemp()})
ch2._post = lambda endpoint, body, timeout_ms: (sent.append((endpoint, body)) or {})  # type: ignore

ch2._send_one("u1", "hello")
assert len(sent) == 1
endpoint, body = sent[0]
assert endpoint == "ilink/bot/sendmessage"
msg = body["msg"]
assert msg["to_user_id"] == "u1"
assert msg["message_type"] == 2 and msg["message_state"] == 2
assert msg["item_list"] == [{"type": 1, "text_item": {"text": "hello"}}]
assert msg["client_id"], "client_id must be generated"

# base_info is merged by _payload() (the mock above bypasses it)
wire = ch2._payload(body)
assert wire["base_info"]["bot_agent"] == ch2.cfg.bot_agent
assert wire["msg"] is body["msg"]

# send() chunks long text into multiple calls
sent.clear()
ch2.send("u1", "y" * 250)
assert len(sent) == 1, "250 chars fits the default 2000 limit"

sent.clear()
ch2.cfg.chunk_limit = 100
ch2.send("u1", "y" * 250)
assert len(sent) == 3, f"expected 3 chunks, got {len(sent)}"

try:
    ch2.send("", "x")
    raise AssertionError("empty recipient must raise")
except ValueError:
    pass
print("9. outbound body: OK")

# ── 10. credential import round-trip ────────────────────────────────────

with tempfile.TemporaryDirectory() as td:
    src = Path(td) / "src-im-bot.json"
    src.write_text(
        json.dumps(
            {
                "token": "tok-abc",
                "baseUrl": "https://ilinkai.weixin.qq.com",
                "userId": "u@im.wechat",
                "savedAt": "2026-09-13T04:56:27.806Z",
            }
        ),
        encoding="utf-8",
    )
    dst = Path(td) / "out" / "default.json"
    result = import_openclaw_account(src, dst)
    assert result["account"] == "src"
    assert result["userId"] == "u@im.wechat"
    written = json.loads(dst.read_text(encoding="utf-8"))
    assert written["token"] == "tok-abc"
    assert written["importedFrom"] == str(src)

    # a credential without a token is rejected rather than imported blank
    bad = Path(td) / "bad-im-bot.json"
    bad.write_text(json.dumps({"baseUrl": "x"}), encoding="utf-8")
    try:
        import_openclaw_account(bad, Path(td) / "x.json")
        raise AssertionError("tokenless credential must raise")
    except ValueError:
        pass
print("10. credential import: OK")

print("\nAll 10 channel tests passed")
