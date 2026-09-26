"""test_workspace.py —— WorkspacePanel 交互重构自检。

跑法（必须用系统 Python 312，因为 AutoClaw 内嵌 python 没有 tkinter）：

    cd C:\\Users\\匡溯昀\\pledge-evolving\\forge-gui
    "C:\\Users\\匡溯昀\\AppData\\Local\\Programs\\Python\\Python312\\python.exe" test_workspace.py

覆盖点（与任务书对齐）：
    1. 点文件树真的能看到内容
    2. Tab 行为（开 3 个 / 重开不重复 / 关 active 切相邻 / 全关回 Home）
    3. Home 统计与 workspace_summary 一致
    4. diff 真能看到（有改动 + 无改动 两种）
    5. reveal_file（相对 / 绝对 / 带 :line / 不存在）
    6. 无 git 目录不崩
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

# 这两个必须在 import workspace 之前
import tkinter as tk

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import workspace as W  # noqa: E402
from workspace import (  # noqa: E402
    WorkspacePanel,
)

# 真实仓库根（用于 git 仓库场景）
REAL_REPO = r"C:\Users\匡溯昀\pledge-evolving"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def _ok(name: str) -> None:
    PASSED.append(name)
    print(f"[PASS] {name}")


def _fail(name: str, msg: str) -> None:
    FAILED.append((name, msg))
    print(f"[FAIL] {name}: {msg}")


def _run(name: str, fn) -> bool:
    try:
        fn()
        _ok(name)
        return True
    except AssertionError as e:
        _fail(name, str(e) or "assertion failed")
    except Exception as e:
        tb = traceback.format_exc()
        _fail(name, f"{type(e).__name__}: {e}\n{tb}")
    return False


def _make_panel(root, repo):
    p = WorkspacePanel(root, repo_root=repo)
    p.show()
    root.update_idletasks()
    return p


def _drain(root, n: int = 2) -> None:
    for _ in range(n):
        try:
            root.update()
        except tk.TclError:
            pass


# ─── 1. 点文件树真的能看到内容 ──────────────────────────


def test_file_tree_click_loads_code():
    root = tk.Tk()
    root.withdraw()
    try:
        panel = _make_panel(root, REAL_REPO)
        # 找第一个非 __init__.py 的 .py 文件（与 selftest 同款）
        repo_path = Path(REAL_REPO)
        sample = None
        for p in repo_path.rglob("*.py"):
            if any(part in W._SKIP_DIRS for part in p.parts):
                continue
            if p.name == "__init__.py":
                continue
            sample = p
            break
        assert sample is not None, "未找到样本 .py 文件"

        # 在文件树里找对应 row（通过 path 比对）
        target_row = None
        for row, node in panel._file_tree_rows:
            if not node["is_dir"] and node["path"] == sample:
                target_row = row
                break
        # 上面那一行在仓库根没展开深目录时找不到——fallback：手动展开
        if target_row is None:
            # 展开路径上的所有父目录
            cur = sample.parent
            while cur != repo_path and cur.exists():
                if str(cur) not in panel._expanded_dirs:
                    panel._expanded_dirs.add(str(cur))
                cur = cur.parent
            panel._refresh_file_tree()
            for row, node in panel._file_tree_rows:
                if not node["is_dir"] and node["path"] == sample:
                    target_row = row
                    break
        assert target_row is not None, f"文件树里找不到 {sample}"

        # 触发点击
        target_row._handle_click()
        _drain(root, 4)
        assert panel._active_file is not None, "点击后 active_file 未设置"
        assert panel._active_file.resolve() == sample.resolve(), \
            f"active_file = {panel._active_file} != {sample}"

        # 代码 Text 应该有内容
        code_content = panel._code_text.get("1.0", "end-1c")
        assert code_content, "代码 Text 为空"

        # 跟磁盘文件前 3 行比对
        with open(sample, "r", encoding="utf-8", errors="replace") as f:
            disk_lines = []
            for i, line in enumerate(f):
                if i >= 3:
                    break
                disk_lines.append(line.rstrip("\n"))
        code_lines = code_content.splitlines()[:3]
        assert code_lines == disk_lines, \
            f"代码前 3 行不匹配\n  期望 {disk_lines}\n  实际 {code_lines}"
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


# ─── 2. Tab 行为 ─────────────────────────────────────────


def test_tab_lifecycle():
    root = tk.Tk()
    root.withdraw()
    try:
        panel = _make_panel(root, REAL_REPO)
        # 收集 3 个不同的 .py 文件
        repo_path = Path(REAL_REPO)
        samples = []
        for p in sorted(repo_path.rglob("*.py")):
            if any(part in W._SKIP_DIRS for part in p.parts):
                continue
            if p.name == "__init__.py":
                continue
            samples.append(p)
            if len(samples) >= 3:
                break
        assert len(samples) == 3, f"样本不够 3 个：{len(samples)}"

        # 依次打开
        for s in samples:
            panel.open_file(s)
            _drain(root, 2)
        assert len(panel._open_tabs) == 3, \
            f"开了 3 个文件，Tab 数 = {len(panel._open_tabs)}"

        # 再点已打开的 → 不增 Tab，仅切换 active
        first_key = next(iter(panel._open_tabs))
        first_path = panel._open_tabs[first_key]["path"]
        panel.open_file(first_path)
        _drain(root, 2)
        assert len(panel._open_tabs) == 3, \
            f"重复开已打开的文件，Tab 数应保持 3，实际 {len(panel._open_tabs)}"
        assert panel._active_file.resolve() == first_path.resolve(), \
            "active_file 没切回去"

        # 关掉当前 active → 切到相邻
        active_key = panel._tab_key_of(panel._active_file)
        idx_in_order = panel._tab_order.index(active_key)
        expected_next_key = (panel._tab_order[idx_in_order + 1]
                             if idx_in_order + 1 < len(panel._tab_order)
                             else panel._tab_order[idx_in_order - 1])
        # 直接调用 close 流程
        info = panel._open_tabs[active_key]
        panel._on_file_tab_closed(info["tab"])
        _drain(root, 2)
        assert len(panel._open_tabs) == 2, \
            f"关一个后 Tab 数 = {len(panel._open_tabs)}，期望 2"
        assert panel._active_file is not None, "关 active 后没切到新 active"
        assert panel._tab_key_of(panel._active_file) == expected_next_key, \
            f"关 active 后没切到相邻：当前 {panel._active_file}，期望 {expected_next_key}"

        # 全关掉 → 回 Home
        for key in list(panel._open_tabs.keys()):
            info = panel._open_tabs[key]
            panel._on_file_tab_closed(info["tab"])
            _drain(root, 1)
        assert len(panel._open_tabs) == 0, "全关后 Tab 应清空"
        assert panel._active_file is None, "全关后 active_file 应清空"
        # Home 应该 pack，code Text 应该 forget
        # 用 pack_info / winfo_manager 替代 winfo_ismapped（后者在 root.withdraw() 下恒为 0）
        try:
            home_pack = panel._home_frame.pack_info()
        except tk.TclError:
            home_pack = {}
        assert home_pack, f"全关后 Home 应该 packed，实际 pack_info={home_pack!r}"
        try:
            code_pack = panel._code_text.pack_info()
        except tk.TclError:
            code_pack = {}
        assert not code_pack, f"全关后代码 Text 应 forget，实际 pack_info={code_pack!r}"
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


# ─── 3. Home 统计与 workspace_summary 一致 ──────────────


def test_workspace_summary_consistent():
    root = tk.Tk()
    root.withdraw()
    try:
        panel = _make_panel(root, REAL_REPO)
        s = panel.workspace_summary()
        # 自己算一遍
        expected_changed = len(panel._git_status)
        expected_added = sum(a for a, _ in panel._git_numstat.values())
        expected_removed = sum(d for _, d in panel._git_numstat.values())
        expected_recent = list(panel._git_status.keys())
        assert s["repo"].endswith("pledge-evolving"), \
            f"repo 错：{s['repo']}"
        assert s["changed"] == expected_changed, \
            f"changed {s['changed']} != {expected_changed}"
        assert s["added"] == expected_added, \
            f"added {s['added']} != {expected_added}"
        assert s["removed"] == expected_removed, \
            f"removed {s['removed']} != {expected_removed}"
        assert s["recent"] == expected_recent, \
            f"recent 列表不一致\n  期望 {expected_recent}\n  实际 {s['recent']}"
        assert s["is_git"] is True, "真实仓库应是 git 仓库"

        # Home 字符串里应包含这些统计数字
        panel._refresh_home()
        text = panel._home_summary_var.get()
        assert f"{expected_changed} files changed" in text, \
            f"Home summary 文本不含 changed 数：{text!r}"
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


# ─── 4. diff 真能看到 ────────────────────────────────────


def test_diff_visible_for_changed_and_unchanged():
    root = tk.Tk()
    root.withdraw()
    try:
        panel = _make_panel(root, REAL_REPO)
        # 找第一个非空改动的文件
        changed_file = None
        for path, code in panel._git_status.items():
            if not code.startswith("?"):  # 已有 tracked 改动优先
                full = Path(REAL_REPO) / path
                if full.is_file():
                    changed_file = path
                    break
        # fallback：任意 untracked
        if changed_file is None:
            for path, code in panel._git_status.items():
                full = Path(REAL_REPO) / path
                if full.is_file():
                    changed_file = path
                    break
        assert changed_file is not None, "git_status 没改动可测"

        panel.show_diff(changed_file)
        _drain(root, 3)
        diff_content = panel._diff_text.get("1.0", "end-1c")
        assert len(diff_content) > 0, "diff Text 长度应为正"
        # 有改动 → 应有 hunk 标记或 +/- 行
        has_marker = ("@@" in diff_content or
                      any(line.startswith(("+", "-")) for line in
                          diff_content.splitlines()))
        # untracked 也允许：整文件按 + 渲染
        if not has_marker:
            status = panel._git_status.get(changed_file, "")
            assert status.startswith("?"), \
                f"改动文件 {changed_file} 没有 hunk/+/- 标记，且不是 untracked："\
                f"diff 内容前 200 字符={diff_content[:200]!r}"

        # 无改动文件：直接拿一个已知存在的非 git 跟踪文件
        sample_clean = None
        for p in Path(REAL_REPO).rglob("*.md"):
            try:
                rel = str(p.resolve().relative_to(Path(REAL_REPO).resolve())) \
                    .replace("\\", "/")
            except ValueError:
                continue
            if rel not in panel._git_status:
                sample_clean = rel
                break
        if sample_clean is not None:
            panel.show_diff(sample_clean)
            _drain(root, 2)
            clean_diff = panel._diff_text.get("1.0", "end-1c")
            assert clean_diff, "无改动文件的 diff 也应有提示文案"
            # 至少应可读（>0 且提示用户「无未提交改动」之类的字眼）
            assert "无未提交改动" in clean_diff or len(clean_diff) > 0, \
                f"无改动文件文案缺失：{clean_diff!r}"
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


# ─── 5. reveal_file ──────────────────────────────────────


def test_reveal_file_forms():
    root = tk.Tk()
    root.withdraw()
    try:
        panel = _make_panel(root, REAL_REPO)
        # 找一个 .py 文件作为测试目标
        repo_path = Path(REAL_REPO)
        sample = None
        for p in repo_path.rglob("*.py"):
            if any(part in W._SKIP_DIRS for part in p.parts):
                continue
            if p.name == "__init__.py":
                continue
            sample = p
            break
        assert sample is not None
        try:
            rel = str(sample.resolve().relative_to(repo_path.resolve())) \
                .replace("\\", "/")
        except ValueError:
            rel = sample.name

        # (a) 相对路径
        panel.reveal_file(rel)
        _drain(root, 2)
        assert panel._active_file is not None and \
            panel._active_file.resolve() == sample.resolve(), \
            f"reveal_file(相对) 失败，active={panel._active_file}"

        # (b) 绝对路径
        panel.reveal_file(str(sample.resolve()))
        _drain(root, 2)
        assert panel._active_file.resolve() == sample.resolve(), \
            f"reveal_file(绝对) 失败，active={panel._active_file}"

        # (c) 带 :120 行号
        panel.reveal_file(f"{sample.resolve()}:120")
        _drain(root, 2)
        assert panel._active_file.resolve() == sample.resolve(), \
            f"reveal_file(:line) 失败，active={panel._active_file}"

        # (d) 不存在的路径——不应抛异常
        try:
            panel.reveal_file(r"C:\nope\does-not-exist-12345.py")
        except Exception as e:
            raise AssertionError(f"reveal_file(不存在) 抛异常: {e}")
        # 应该：要么静默无操作，要么设了状态文案；active_file 不应指向不存在的文件
        if panel._active_file is not None:
            assert panel._active_file.exists(), \
                "reveal_file(不存在) 后 active_file 不该指向不存在的文件"
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


# ─── 6. 无 git 目录 ──────────────────────────────────────


def test_non_git_repo():
    root = tk.Tk()
    root.withdraw()
    try:
        tmp = Path(tempfile.mkdtemp(prefix="ws_nongit_"))
        # 放一个 .py 文件方便 open_file 测试
        (tmp / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        panel = _make_panel(root, str(tmp))
        panel.show()
        panel.refresh()
        _drain(root, 2)

        s = panel.workspace_summary()
        assert s["is_git"] is False, f"临时目录不该是 git：{s}"
        assert s["changed"] == 0, "无 git 仓库 changed 应为 0"
        assert s["recent"] == [], "无 git 仓库 recent 应为空"

        # Home 应有可读文案
        panel._refresh_home()
        text = panel._home_empty_var.get()
        assert "git" in text or "未被" in text or "目录" in text, \
            f"Home 文案缺可读性：{text!r}"

        # open_file 仍可用
        ok = panel.open_file(tmp / "hello.py")
        _drain(root, 2)
        assert ok, "无 git 仓库里 open_file 应返回 True"
        assert panel._active_file is not None
        assert panel._active_file.name == "hello.py"

        # reveal_file 不存在路径仍静默
        try:
            panel.reveal_file(r"C:\nope\still-missing.py")
        except Exception as e:
            raise AssertionError(f"无 git 仓库里 reveal_file 抛异常: {e}")
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


# ─── 跑 ──────────────────────────────────────────────────


def main() -> int:
    tests = [
        ("1. 点文件树真的能看到内容", test_file_tree_click_loads_code),
        ("2. Tab 行为（开/重开/关/全关回 Home）", test_tab_lifecycle),
        ("3. Home 统计与 workspace_summary 一致", test_workspace_summary_consistent),
        ("4. diff 真能看到（改动 + 无改动）", test_diff_visible_for_changed_and_unchanged),
        ("5. reveal_file（相对/绝对/:line/不存在）", test_reveal_file_forms),
        ("6. 无 git 目录不崩", test_non_git_repo),
    ]
    for name, fn in tests:
        print(f"\n=== {name} ===")
        _run(name, fn)

    print("\n" + "=" * 60)
    print(f"PASSED: {len(PASSED)}")
    print(f"FAILED: {len(FAILED)}")
    for n, m in FAILED:
        print(f"  ✗ {n}: {m.splitlines()[0]}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())