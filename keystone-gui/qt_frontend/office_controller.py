"""Built-in Office model shared by the local editor and the Work conversation."""

import json
from pathlib import Path
from zipfile import BadZipFile

from PySide6.QtCore import QObject, QUrl, Signal, Slot

from .local_tool_runtime import ToolFailure
from .office_workspace import EXTENSIONS, OfficeWorkspace
from .office_formats import calculated, export_file, import_file
from .productivity_tools import STRING, array, obj

MAYBE_TEXT = {"anyOf": [STRING, {"type": "null"}]}
CHANGE = obj(
    {"field": STRING, "before": MAYBE_TEXT, "value": MAYBE_TEXT},
    "field",
    "before",
    "value",
)
KIND = {"type": "string", "enum": list(EXTENSIONS)}
CATALOG = {
    "office_list": ("List native Office files in this workspace.", obj({})),
    "office_create": (
        "Create an editable local Office document, spreadsheet or presentation. "
        "Supply optional initial fields. Document P1, P2 (P1.style heading/paragraph/bullet); sheet A1, B2, T2.A1; slides S1 title, S1.body text, S2, S2.body.",
        obj(
            {
                "kind": KIND,
                "title": STRING,
                "fields": {"type": "object", "additionalProperties": STRING},
            },
            "kind",
            "title",
        ),
    ),
    "office_read": (
        "Read a local Office file and its version. Paginate fields with offset.",
        obj({"id": STRING, "offset": {"type": "integer", "minimum": 0}}, "id"),
    ),
    "office_apply": (
        "After office_read, submit independent edits in suggestion mode in ONE call: "
        'suggestions maps each field to its new value, e.g. {"B7":"42","D8":"confirmed"}. '
        "Each field gets its own review and undo. Use changes instead for a single grouped edit or direct mode, "
        "with exact prior values (null if missing). Finish after a successful submission.",
        obj(
            {
                "id": STRING,
                "version": {"type": "integer"},
                "suggestions": {"type": "object", "additionalProperties": MAYBE_TEXT,
                                "description": "Independent suggestions at the exact version just read. Use with id and version only."},
                "changes": array(CHANGE),
                "label": STRING,
            },
            "id",
            "version",
        ),
    ),
    "office_export": (
        "Export a fresh Office Open XML copy to Office/Exports in the workspace.",
        obj({"id": STRING}, "id"),
    ),
}
OFFICE_WRITES = {"office_create", "office_apply", "office_export"}


def summary(d):
    return dict(
        id=d["id"],
        kind=d["kind"],
        title=d["fields"]["title"],
        version=d["version"],
        mode=d["mode"],
        pending=len(d["pending"]),
    )


def editor_state(d):
    return {
        **summary(d),
        "fields": d["fields"],
        "pending": d["pending"],
        "history": [
            {k: v for k, v in entry.items() if k != "changes"}
            for entry in reversed(d["history"])
        ],
        "computed": calculated(d["fields"]) if d["kind"] == "spreadsheet" else {},
    }


class OfficeController(QObject):
    changed = Signal()

    def __init__(self, workspace, parent=None):
        super().__init__(parent)
        self.workspace = workspace

    @Slot(str, result=str)
    def request(self, payload):
        try:
            a = json.loads(payload)
            store = OfficeWorkspace(self.workspace())
            action = a["action"]
            if action == "list":
                data = store.recent()
            elif action == "open":
                data = editor_state(store.load(a["id"]))
            elif action == "create":
                data = editor_state(store.create(a["kind"], a["title"]))
            elif action == "export":
                data = str(export_file(store.root, store.load(a["id"])))
            elif action in ("apply", "mode", "accept", "reject", "undo"):
                data = editor_state(store.change(a["id"], action, a.get("args", {})))
            else:
                raise ValueError("未知的办公操作。")
            return json.dumps({"ok": True, "data": data}, ensure_ascii=False)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            BadZipFile,
            ToolFailure,
        ) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(QUrl, result=str)
    def importSelected(self, url):
        try:
            if not url.isLocalFile():
                raise ValueError("请选择本地文件。")
            p = Path(url.toLocalFile())
            kind, fields = import_file(p)
            d = OfficeWorkspace(self.workspace()).create(
                kind, p.stem[:120] or "Office", fields
            )
            return json.dumps({"ok": True, "data": editor_state(d)}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    def tools(self, workspace):
        return {
            "keystone_" + key: (
                {
                    "type": "function",
                    "function": {
                        "name": "keystone_" + key,
                        "description": description,
                        "parameters": schema,
                    },
                },
                ("office", key, str(workspace)),
            )
            for key, (description, schema) in CATALOG.items()
        }

    def call(self, key, workspace, args, cancel):
        try:
            if str(self.workspace()) != str(workspace) or cancel.is_set():
                raise ValueError("办公工作区已切换或操作已取消。")
            store = OfficeWorkspace(workspace)
            if key == "office_list":
                data = store.recent()
            elif key == "office_create":
                data = summary(
                    store.create(args["kind"], args["title"], args.get("fields"), "ai")
                )
            elif key == "office_read":
                d = store.load(args["id"])
                start = args.get("offset", 0)
                if type(start) is not int or start < 0:
                    raise ValueError("页偏移无效。")
                pairs = list(d["fields"].items())
                data = {
                    **summary(d),
                    "fields": dict(pairs[start : start + 80]),
                    "next_offset": start + 80 if start + 80 < len(pairs) else None,
                }
            elif key == "office_apply":
                data = summary(store.change(args["id"], "apply", args, "ai", cancel))
            elif key == "office_export":
                data = {"path": str(export_file(workspace, store.load(args["id"])))}
            else:
                raise ValueError("未知的办公工具。")
            if key in OFFICE_WRITES:
                self.changed.emit()
            return {
                "ok": True,
                "content": json.dumps(data, ensure_ascii=False),
                "meta": {"execution_state": "completed"},
            }
        except (
            KeyError,
            TypeError,
            ValueError,
            OSError,
            RuntimeError,
            BadZipFile,
            ToolFailure,
        ) as exc:
            return {"ok": False, "error": str(exc)}
