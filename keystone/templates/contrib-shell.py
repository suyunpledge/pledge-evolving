"""契约壳模板 —— 贴给贡献者用。

把你已经写好的函数**原样**粘贴到下面标注的位置。不要改写逻辑、不要搬进壳里、
不要为了适配而重命名你的内部辅助函数。壳只负责三件事：声明 API 版本、把 hook
挂进清单、把你的自检结果转成契约要求的格式。

唯一需要你动的两处：
  1. <模块名>       —— 必须与文件名一致
  2. hooks 里的映射 —— 让钩子名指向你的函数
"""

from __future__ import annotations

from typing import Any

MODULE_API_VERSION = 1


# ==== 你的代码从这里开始（原样粘贴，不要改） ==============================

def your_public_function_a(...):
    ...


def your_public_function_b(...):
    ...


def _your_private_helpers(...):
    ...


def _your_own_selftest(...):
    ...


# ==== 你的代码到这里结束 ================================================


def register(api) -> dict[str, Any]:
    """加载时调用一次，声明能力并把钩子挂上去。"""
    return {
        "name": "<模块名>",                      # 必须等于文件名
        "version": "1.0.0",
        "capabilities": ["<域>.<能力>"],
        "hooks": {
            # 钩子名 -> 你的函数（函数签名按任务书写的来）
            "your_hook_name": your_public_function_a,
        },
        "author": "<你的名字>",
        "summary": "<一句话>",
    }


def selftest() -> list[tuple[str, bool, str]]:
    """把你的自检结果转成 (断言名, 是否通过, 失败说明) 列表，至少 8 条。

    如果你原本的 _your_own_selftest() 是「失败就抛异常」或「打印结果」的写法，
    在这里转成返回列表；逻辑不用重写，把每条判定的结论收进来即可。
    """
    results: list[tuple[str, bool, str]] = []
    cases = [
        ("<断言名 1>", lambda: True, "期望说明"),
        ("<断言名 2>", lambda: True, "期望说明"),
        # ……至少 8 条
    ]
    for name, probe, detail in cases:
        try:
            passed = bool(probe())
            results.append((name, passed, "" if passed else detail))
        except Exception as exc:
            results.append((name, False, f"{type(exc).__name__}: {exc}"))
    return results


__all__ = ["MODULE_API_VERSION", "register", "selftest"]
