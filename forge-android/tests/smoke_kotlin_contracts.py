# -*- coding: utf-8 -*-
"""smoke_kotlin_contracts — 不依赖 Android 编译环境的 Kotlin 侧契约检查。

1. 鉴权头：app 里所有 ``.header("Authorization", …)`` 必须以 ``"Bearer "`` 开头
   （桥端 forge_bridge.py 逐字节比对 'Bearer ' + TOKEN）。v0.7 合并时
   AssistantScreen.kt 的共享 HTTP 工具被误改成 ``"***"``，目录 / 遥控 / 引导
   全部 401 而静默失败。
2. 引导判断：mainactivity.kt 必须等桥 ready 后再查 /onboarding/status——
   首次启动解包期间一开 App 就查必然失败，曾被当成「不需要引导」而跳过。
3. 括号配对：没有编译器时最基本的语法自检（忽略字符串与注释）。
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "app" / "src" / "main" / "java"

FAILS = []


def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c:
        FAILS.append(m)


def strip_strings_and_comments(text):
    text = re.sub(r'"""[\s\S]*?"""', '""', text)
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', text)
    text = re.sub(r"'(?:\\.|[^'\\\n])'", "''", text)
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    text = re.sub(r"//[^\n]*", "", text)
    return text


kotlin = sorted(SRC.rglob("*.kt"))
check(bool(kotlin), f"找到 Kotlin 源文件 {len(kotlin)} 个")

# 1) 鉴权头
headers = []
for path in kotlin:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if '"Authorization"' in line and ".header(" in line:
            headers.append((path.name, number, line.strip()))
check(len(headers) >= 3, f"鉴权头调用点 {len(headers)} 处（共享 GET/POST + 状态查询）")
for name, number, line in headers:
    value = line.split('"Authorization",', 1)[1].strip()
    check(value.startswith('"Bearer "'), f"{name}:{number} 鉴权头以 \"Bearer \" 开头")

# 2) 引导判断等桥就绪
main = (SRC / "com" / "forge" / "android" / "mainactivity.kt").read_text(encoding="utf-8")
check("LaunchedEffect(bridge)" in main, "引导状态查询随桥状态重新触发（LaunchedEffect(bridge)）")
check('bridge != "ready"' in main, "桥未 ready 时不查引导状态")
check("LaunchedEffect(Unit) {\n            val st = withContext(Dispatchers.IO) { bridgeGetStatus(ctx) }" not in main,
      "不再在启动瞬间一次性查询引导状态")
check("BootScreen(bridge)" in main, "等待期间显示按状态说明的启动页")

# 3) 括号配对
for path in kotlin:
    body = strip_strings_and_comments(path.read_text(encoding="utf-8"))
    for open_, close in ("{}", "()", "[]"):
        check(body.count(open_) == body.count(close),
              f"{path.name} 括号 {open_}{close} 配对（{body.count(open_)}/{body.count(close)}）")

print()
print(f"{'FAILED' if FAILS else 'OK'} · {len(FAILS)} 项失败")
sys.exit(1 if FAILS else 0)
