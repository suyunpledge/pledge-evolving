"""A submitted Coding turn's IDE focus, with bounded, policy-checked buffer reads."""
from dataclasses import dataclass
import json
from itertools import islice
from pathlib import Path

from keystone.development.editor import Document, ProjectBoundary
from keystone.policy import Mode, ToolEffect
from keystone.tools import ToolResult, ToolSpec
from keystone.vscode_bridge import MAX_CONTEXT


@dataclass(frozen=True)
class Tab:
    path: Path
    text: str
    dirty: bool


@dataclass
class ProjectContext:
    boundary: ProjectBoundary
    active: str
    tabs: dict
    pinned: tuple
    location: dict
    dirty: bool
    total_tabs: int
    read_only: bool = False

    @classmethod
    def capture(cls, pages, pins, focus=None):
        focus = focus if isinstance(focus, dict) else {}
        active = pages.active_document
        tabs, pinned, active_key, location = {}, [], "", {}
        paths = dict.fromkeys([active, *pins, *pages.documents])
        for path in paths:
            document = pages.documents.get(path)
            if document is None or len(tabs) >= 32:
                continue
            try:
                verified = pages.boundary.path(path)
                key = verified.relative_to(pages.boundary.root).as_posix()
            except (OSError, ValueError):
                continue
            tabs[key] = Tab(verified, document.text, document.dirty)
            if path in pins:
                pinned.append(key)
            if path == active:
                active_key = key
                if focus.get("path") == path and focus.get("version") == pages.revisions.get(path, 0):
                    start, end = focus.get("start"), focus.get("end")
                    if all(isinstance(p, list) and len(p) == 2 and all(type(n) is int and n > 0 for n in p)
                           for p in (start, end)) and start <= end:
                        location = {"start": start, "end": end}
        return cls(pages.boundary, active_key, tabs, tuple(pinned), location, pages.unsaved, len(pages.documents),
                   read_only=pages.unsaved)

    def attachments(self):
        # The explicit paperclip keeps its original semantics. Large buffers are
        # available through ide_context in pages instead of overflowing the turn.
        remaining = MAX_CONTEXT
        items = []
        for key in self.pinned:
            tab = self.tabs[key]
            size = len(tab.text.encode("utf-8"))
            if size <= remaining:
                items.append({"path": str(tab.path), "content": tab.text})
                remaining -= size
        return items

    def readable_tab(self, key, policy):
        if not isinstance(key, str) or key not in self.tabs:
            return False
        path = self.tabs[key].path
        try:
            return self.boundary.path(path) == path and policy.allows_read(path)
        except (OSError, ValueError):
            return False

    def read(self, args, ctx):
        if ctx.workspace.resolve() != self.boundary.root:
            raise PermissionError("Coding 项目已切换。")
        active = self.active if self.readable_tab(self.active, ctx.policy) else None
        view = args.get("view", "project")
        if view == "project":
            result = {"project": self.boundary.root.name, "root": ".", "active": active,
                      "focus": self.location if active else {}, "unsaved_at_send": self.dirty,
                      "read_only": ctx.policy.mode == Mode.READ_ONLY,
                      "open_tabs": [{"path": key, "unsaved": tab.dirty, "pinned": key in self.pinned}
                                    for key, tab in self.tabs.items() if self.readable_tab(key, ctx.policy)],
                      "tabs_truncated": self.total_tabs > len(self.tabs)}
        elif view == "buffer":
            key = args.get("path") or active
            if not self.readable_tab(key, ctx.policy):
                raise ValueError("此标签页不可读取。未打开的项目文件请使用 read_range 或 read_file。")
            result = self.buffer(key, args, ctx.secret_scope)
        else:
            raise ValueError("view 应为 project 或 buffer。")
        return ToolResult(True, content=json.dumps(ctx.secret_scope.protect(result), ensure_ascii=False))

    def buffer(self, key, args, scope):
        tab = self.tabs[key]
        # Drafts belong to the submitted turn. Clean buffers follow successful disk edits.
        text = tab.text if tab.dirty else Document.open(self.boundary, tab.path).text
        safe = scope.document_text(tab.path, text)
        remapped = safe.count("\n") != text.count("\n")
        focus = self.location if key == self.active and text == tab.text and not remapped else {}
        first = args.get("start", focus.get("start", [1])[0])
        count = args.get("count", 24)
        if type(first) is not int or first < 1 or type(count) is not int or not 1 <= count <= 120:
            raise ValueError("行号从 1 开始，每次可读取 1 至 120 行。")
        lines = safe.split("\n")
        if first > len(lines):
            raise ValueError(f"文件共 {len(lines)} 行。")
        shown, budget, clipped = [], 10000, False
        for line in islice(lines, first - 1, first - 1 + count):
            excerpt = line[:min(budget, 2400)]
            shown.append(excerpt)
            clipped |= len(excerpt) != len(line)
            budget -= len(excerpt) + 1
            if budget <= 0:
                break
        end = first + len(shown) - 1
        result = {"path": key, "source": "unsaved_at_send" if tab.dirty else "disk",
                  "focus": focus, "start": first, "lines": shown, "total_lines": len(lines),
                  "next_line": end + 1 if end < len(lines) else None, "clipped": clipped,
                  "redacted_line_map_changed": remapped}
        if focus and focus["start"] != focus["end"]:
            (a, left), (b, right) = focus["start"], focus["end"]
            if b <= len(lines) and b - a < 120:
                selection = lines[a - 1:b]
                # Redaction can shift original columns. Never slice a secret using old offsets.
                if safe == text or selection == text.split("\n")[a - 1:b]:
                    snippet = utf16_selection(selection, left, right)
                    result.update(selected_text=snippet[:2400], selection_clipped=len(snippet) > 2400)
        return result


def utf16_selection(lines, start_column, end_column):
    """Qt offsets are UTF-16 code units. The exclusive end may be on the next line."""
    start, end = 2 * (start_column - 1), 2 * (end_column - 1)
    if len(lines) == 1:
        return lines[0].encode("utf-16-le")[start:end].decode("utf-16-le", "replace")
    lines[0] = lines[0].encode("utf-16-le")[start:].decode("utf-16-le", "replace")
    lines[-1] = lines[-1].encode("utf-16-le")[:end].decode("utf-16-le", "replace")
    return "\n".join(lines)


def install_project_tool(agent, current):
    def inspect(args, ctx):
        project = current()
        if project is None:
            return ToolResult(False, error="当前没有运行中的 Coding 项目任务。")
        return project.read(args, ctx)
    agent.registry.register(ToolSpec(
        "ide_context", "Read the Coding IDE project, open tabs and active selection at submission. "
        "Use view=buffer for current/selected code (unsaved snapshot or current disk); optional open-tab path, "
        "1-based start/count. Focus positions are [line, UTF-16 column], exclusive end. "
        "Use list_dir/find_files/grep for unopened project files. Before editing, read the target with a disk file tool.",
        inspect, schema={"type": "object", "additionalProperties": False, "properties": {
            "view": {"type": "string", "enum": ["project", "buffer"]}, "path": {"type": "string"},
            "start": {"type": "integer", "minimum": 1}, "count": {"type": "integer", "minimum": 1, "maximum": 120}}},
        no_defer=True), trusted=True, effect=ToolEffect.READ)
