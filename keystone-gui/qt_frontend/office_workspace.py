"""Local Office documents. UI edits and Work tools share versioned transactions."""

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from keystone.runtime_lock import named_resource_lock
from .local_tool_runtime import workspace_path
from .office_charts import CHART_FIELD, parse_chart
from .office_slides import SLIDE, arrange, slide_order, validate_design

EXTENSIONS = {"document": ".kdoc", "spreadsheet": ".ksheet", "presentation": ".kslides"}
FIELDS = {
    "document": r"P[1-9][0-9]{0,3}(?:\.style)?",
    "spreadsheet": r"(?:T[1-9][0-9]?\.)?(?:[A-Z]{1,2}[1-9][0-9]{0,3})|T[1-9][0-9]?\.(?:name|chart)",
    "presentation": rf"(?:{SLIDE.pattern}|slideOrder|slideTheme)",
}
MAX_BYTES = 6 * 1024 * 1024


def validate(kind, field, value):
    if not isinstance(field, str) or (
        field != "title" and not re.fullmatch(FIELDS[kind], field)
    ):
        raise ValueError("字段地址无效。")
    if kind == "spreadsheet" and CHART_FIELD.fullmatch(field):
        if value is not None:
            parse_chart(value)
        return
    if kind == "spreadsheet" and field.startswith("T1.") and field != "T1.name":
        raise ValueError("首张工作表请直接使用 A1 等单元格地址。")
    if value is None and field != "title":
        return
    if (
        not isinstance(value, str)
        or len(value) > 32000
        or any(ord(c) < 32 and c not in "\r\n\t" for c in value)
    ):
        raise ValueError("字段内容无效或过长。")
    if field == "title" and (not value.strip() or len(value) > 120):
        raise ValueError("文件标题应为 1 至 120 个字符。")
    if kind == "presentation":
        validate_design(field, value)
    if field.endswith(".style") and value not in ("paragraph", "heading", "bullet"):
        raise ValueError("请选择正文、标题或列表。")
    if field.endswith(".name") and (
        not value.strip() or len(value) > 31 or re.search(r"[\\/*?:\[\]]", value)
    ):
        raise ValueError("工作表名称无效。")


def apply_values(fields, changes):
    for item in changes:
        if item["value"] is None:
            fields.pop(item["field"], None)
        else:
            fields[item["field"]] = item["value"]


class OfficeWorkspace:
    def __init__(self, root):
        self.root = Path(root)

    def folder(self):
        return workspace_path(self.root, "Office")

    def location(self, doc_id):
        if not isinstance(doc_id, str) or not re.fullmatch(
            r"[a-f0-9]{32}\.(?:kdoc|ksheet|kslides)", doc_id
        ):
            raise ValueError("办公文件编号无效。")
        return workspace_path(self.root, "Office/" + doc_id)

    def load(self, doc_id):
        try:
            with ZipFile(self.location(doc_id)) as archive:
                item = archive.getinfo("model.json")
                if item.file_size > MAX_BYTES:
                    raise ValueError("办公文件过大。")
                d = json.loads(archive.read(item))
            if (
                d["id"] != doc_id
                or EXTENSIONS[d["kind"]] != Path(doc_id).suffix
                or d.get("format") != 1
            ):
                raise ValueError("不支持的办公文件格式。")
            if (
                not isinstance(d["fields"], dict)
                or len(d["fields"]) > 20000
                or type(d["version"]) is not int
                or d["version"] < 1
                or d["mode"] not in ("direct", "suggest")
                or not isinstance(d["modified"], (float, int))
                or not isinstance(d["history"], list)
                or not isinstance(d["pending"], list)
                or not isinstance(d.get("suggestion_receipts", []), list)
                or len(d.get("suggestion_receipts", [])) > 64
            ):
                raise ValueError("办公文件的数据结构无效。")
            validate(d["kind"], "title", d["fields"]["title"])
            for key, value in d["fields"].items():
                if value is None:
                    raise ValueError("办公字段缺少内容。")
                validate(d["kind"], key, value)
            return d
        except (
            BadZipFile,
            KeyError,
            TypeError,
            UnicodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ValueError("办公文件损坏，无法打开。") from exc

    def read_for_tool(self, doc_id):
        # Models sometimes copy the full UUID but omit its native file extension.
        if isinstance(doc_id, str) and re.fullmatch(r"[a-fA-F0-9]{32}", doc_id):
            names = [doc_id.lower() + ext for ext in EXTENSIONS.values()]
            matches = [name for name in names if self.location(name).is_file()]
            if len(matches) != 1:
                raise ValueError("无法唯一确定办公文件，请使用完整文件编号。")
            doc_id = matches[0]
        return self.load(doc_id)

    def save(self, doc):
        path = self.location(doc["id"])
        path.parent.mkdir(exist_ok=True)
        data = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf8")
        if len(data) > MAX_BYTES or len(doc["fields"]) > 20000:
            raise ValueError("此文件超出本地编辑容量。")
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".office-")
        try:
            with os.fdopen(fd, "w+b") as stream:
                with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
                    archive.writestr("model.json", data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.location(doc["id"]))
        finally:
            Path(tmp).unlink(missing_ok=True)

    def create(self, kind, title, fields=None, author="user"):
        if kind not in EXTENSIONS:
            raise ValueError("请选择文档、表格或演示。")
        validate(kind, "title", title)
        fields = {} if fields is None else fields
        if not isinstance(fields, dict) or len(fields) > 20000 or "title" in fields:
            raise ValueError("初始字段无效。")
        initial = {
            "document": {"P1": ""},
            "spreadsheet": {},
            "presentation": {"S1": "", "S1.body": ""},
        }[kind]
        for key, value in fields.items():
            validate(kind, key, value)
            if value is not None:
                initial[key] = value
        doc_id = uuid.uuid4().hex + EXTENSIONS[kind]
        d = dict(
            format=1,
            id=doc_id,
            kind=kind,
            fields={"title": title, **initial},
            version=1,
            mode="suggest",
            history=[],
            pending=[],
            modified=time.time(),
            author=author,
        )
        self.save(d)
        return d

    def recent(self):
        folder = self.folder()
        if not folder.exists():
            return []
        rows = []
        for p in folder.iterdir():
            if p.suffix not in EXTENSIONS.values():
                continue
            try:
                d = self.load(p.name)
                rows.append(
                    dict(
                        id=d["id"],
                        kind=d["kind"],
                        title=d["fields"]["title"],
                        modified=d["modified"],
                        version=d["version"],
                        pending=len(d["pending"]),
                    )
                )
            except (ValueError, RuntimeError, OSError, KeyError, TypeError):
                continue
        return sorted(rows, key=lambda row: row["modified"], reverse=True)

    def change(self, doc_id, action, args, author="user", cancel=None):
        path = self.location(doc_id)
        with named_resource_lock("office-document", str(path), cancelled=cancel):
            d = self.load(doc_id)
            fields = d["fields"]
            if (
                action == "mode"
                and author == "user"
                and args.get("value") in ("suggest", "direct")
            ):
                d["mode"] = args["value"]
            elif action == "arrange" and author == "user":
                changes = arrange(d, args)
                if not changes:
                    return d
                label = {"insert": "插入幻灯片", "duplicate": "复制幻灯片",
                         "move": "移动幻灯片", "delete": "删除幻灯片"}[args["command"]]
                self.record(d, changes, author, label)
            elif action == "apply" and "suggestions" in args:
                if author != "ai" or "changes" in args:
                    raise ValueError("请单独提交逐项审核的 AI 建议。")
                if not self._suggest_fields(d, args):
                    return d
            elif action == "apply":
                changes = args.get("changes")
                version = args.get("version")
                if (
                    type(version) is not int
                    or not 1 <= version <= d["version"]
                    or not isinstance(changes, list)
                    or not 1 <= len(changes) <= 1200
                ):
                    raise ValueError("请读取当前版本并提交有效的编辑批次。")
                seen = set()
                for edit in changes:
                    if not isinstance(edit, dict) or set(edit) != {
                        "field",
                        "before",
                        "value",
                    }:
                        raise ValueError("每项编辑必须包含 field、before 和 value。")
                    key = edit["field"]
                    validate(d["kind"], key, edit["value"])
                    if key in seen or fields.get(key) != edit["before"]:
                        raise ValueError("字段已变化，请保留草稿并重新读取。")
                    seen.add(key)
                changes = [e for e in changes if e["before"] != e["value"]]
                if not changes:
                    return d
                label = str(args.get("label") or "编辑")[:120]
                if author == "ai" and d["mode"] == "suggest":
                    existing = next(
                        (p for p in d["pending"] if p["changes"] == changes), None
                    )
                    if existing:
                        return d
                    if len(d["pending"]) >= 64:
                        raise ValueError("请先处理待审建议。")
                    d["pending"].append(
                        dict(id=uuid.uuid4().hex, label=label, changes=changes)
                    )
                else:
                    self.record(d, changes, author, label)
            elif action in ("accept", "reject") and author == "user":
                item = next(
                    (x for x in d["pending"] if x["id"] == args.get("id")), None
                )
                if item is None:
                    raise ValueError("建议已处理。")
                if action == "accept":
                    if any(
                        fields.get(e["field"]) != e["before"] for e in item["changes"]
                    ):
                        raise ValueError("建议涉及的字段已变化，请拒绝后重新生成。")
                    self.record(d, item["changes"], "ai", item["label"])
                d["pending"].remove(item)
            elif action == "undo" and author == "user":
                who = args.get("author")
                version = args.get("version")
                entry = next(
                    (
                        x
                        for x in reversed(d["history"])
                        if x["author"] == who and not x.get("undone")
                        and (version is None or x["version"] == version)
                    ),
                    None,
                )
                if entry is None:
                    raise ValueError("没有可撤销的修改。")
                touched = {e["field"] for e in entry["changes"]}
                membership = set(entry.get("slideMembership", ()))
                def overlaps(edit):
                    key = edit["field"]
                    return key in touched or key.split(".")[0] in membership

                if any(fields.get(e["field"]) != e["value"] for e in entry["changes"]) or any(
                    later["version"] > entry["version"] and not later.get("undone")
                    and any(overlaps(e) for e in later["changes"])
                    for later in d["history"]
                ):
                    raise ValueError("后续编辑使用了相同字段，已保留当前内容。")
                if membership and any(any(overlaps(e) for e in p["changes"]) for p in d["pending"]):
                    raise ValueError("请先处理该页的待审建议，再撤销页面操作。")
                apply_values(
                    fields,
                    [
                        dict(field=e["field"], value=e["before"])
                        for e in entry["changes"]
                    ],
                )
                entry["undone"] = True
            else:
                raise ValueError("不支持的办公操作。")
            d.update(version=d["version"] + 1, modified=time.time(), author=author)
            if cancel is not None and cancel.is_set():
                raise ValueError("操作已取消。")
            self.save(d)
            return d

    @staticmethod
    def _suggest_fields(d, args):
        values, version = args["suggestions"], args.get("version")
        if not isinstance(values, dict) or not 1 <= len(values) <= 64 or type(version) is not int:
            raise ValueError("请提供文档版本和 1 至 64 项字段建议。")
        for field, value in values.items():
            validate(d["kind"], field, value)
        # A delayed retry can arrive after the user reviewed some of the cards.
        # Acknowledge the original batch without recreating a rejected suggestion.
        payload = json.dumps([version, values], sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        receipts = d.setdefault("suggestion_receipts", [])
        if digest in receipts:
            return False
        if d["mode"] != "suggest" or version != d["version"]:
            raise ValueError("写入模式或文档版本已变化，请重新读取后再编辑。")
        pending_count = len(d["pending"])
        for field, value in values.items():
            before = d["fields"].get(field)
            changes = [dict(field=field, before=before, value=value)]
            if before != value and not any(p["changes"] == changes for p in d["pending"]):
                d["pending"].append(dict(id=uuid.uuid4().hex, label=field, changes=changes))
        if len(d["pending"]) > 64:
            raise ValueError("请先处理待审建议。")
        if len(d["pending"]) == pending_count:
            return False
        receipts.append(digest)
        del receipts[:-64]
        return True

    @staticmethod
    def record(d, changes, author, label):
        track_pages = d["kind"] == "presentation" and any(
            (e["before"] is None or e["value"] is None) and SLIDE.fullmatch(e["field"]) for e in changes)
        prior_pages = set(slide_order(d["fields"])) if track_pages else set()
        apply_values(d["fields"], changes)
        d["history"].append(
            dict(version=d["version"] + 1, label=label, author=author, changes=changes)
        )
        if track_pages:
            membership = prior_pages.symmetric_difference(slide_order(d["fields"]))
            if membership:
                d["history"][-1]["slideMembership"] = sorted(membership)
        del d["history"][:-60]
