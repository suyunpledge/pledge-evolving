"""M8 防回归：GUI 视图层不得裸写中文串（必须走 tr()）。

规则与扫描器一致：text/label/title/summary/detail/placeholder 关键字、
configure/config/set 调用里的裸中文字符串都算违规。
允许例外：tr() 包裹过（AST 上是 Call）；占位符复杂串；单字符；纯符号。
"""
import ast
import re
import sys
from pathlib import Path

GUI = Path(__file__).resolve().parent
FILES = [GUI / "forge_gui_v2.py", GUI / "chat_widgets.py", GUI / "workspace.py",
        GUI / "desktop_features.py", GUI / "phase_client.py", GUI / "config_model.py"]
RE_CJK = re.compile(r"[\u4e00-\u9fff]{2,}")

ALLOW_KEYS = {"text", "label", "title", "summary", "detail", "placeholder",
              "tooltip", "status"}


def violations(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in ALLOW_KEYS:
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                s = node.value.value
                if RE_CJK.search(s) and len(s.strip()) >= 2:
                    bad.append(f"{path.name}:{node.lineno} {node.arg}={s[:40]!r}")
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if name in ("configure", "config", "set"):
                for a in node.keywords:
                    if a.arg in ALLOW_KEYS and isinstance(a.value, ast.Constant) and isinstance(a.value.value, str):
                        s = a.value.value
                        if RE_CJK.search(s) and len(s.strip()) >= 2:
                            bad.append(f"{path.name}:{node.lineno} {name}.{a.arg}={s[:40]!r}")
    return bad


def main() -> int:
    all_bad: list[str] = []
    for f in FILES:
        if f.exists():
            all_bad.extend(violations(f))
    if all_bad:
        print(f"i18n 收口检查失败：{len(all_bad)} 处裸中文（应包 tr()）：")
        for b in all_bad[:20]:
            print("  ", b)
        return 1
    print(f"i18n 收口检查通过：{len(FILES)} 个文件无裸中文")
    return 0


if __name__ == "__main__":
    sys.exit(main())
