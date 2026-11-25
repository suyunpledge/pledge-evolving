"""Built-in Office model shared by the local editor and the Work conversation."""

import json
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4
from zipfile import BadZipFile

from PySide6.QtCore import QObject, QThread, QUrl, Signal, Slot

from .local_tool_runtime import ToolFailure
from .office_workspace import EXTENSIONS, OfficeWorkspace
from .office_formats import calculated, export_file, import_file
from .office_charts import CHART_FIELD, chart_scene, parse_chart
from .office_slides import DESIGN, slide_order
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
        "Supply optional initial fields. Document P1, P2 (P1.style heading/paragraph/bullet); sheet A1, B2, T2.A1; "
        "optional editable XY chart per sheet: T1.chart is a JSON string, e.g. "
        '{"expression":"x^2","min":-5,"max":5,"title":"Parabola"}. '
        "Slides: S1 title, S1.body, S1.right (second column), S1.notes (private speaker notes), "
        "S1.layout content/cover/columns, S1.transition none/fade, S1.hidden true/false as strings. "
        "S1.table is a JSON string of rectangular text rows (1-8 rows, 1-6 columns, first row is the header). "
        "slideTheme paper/midnight/sage; slideOrder is a comma-separated sequence of stable slide IDs.",
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
        "Read an Office file and version. For spreadsheets, charts lists editable chart fields. "
        "Pass chart='T1.chart' to inspect the actual expression, cell formulas, texts and XY points, "
        "chart-local positions and visible editor CSS bounds. No screenshot needed. Paginate with offset.",
        obj({"id": STRING, "offset": {"type": "integer", "minimum": 0}, "chart": STRING}, "id"),
    ),
    "office_apply": (
        "After office_read, submit independent edits in suggestion mode in ONE call: "
        'suggestions maps each field to its new value, e.g. {"B7":"42","D8":"confirmed"}. '
        "Each field gets its own review and undo. Use changes instead for a single grouped edit or direct mode, "
        "with exact prior values (null if missing). For presentations use the stable slide IDs from office_read, "
        "e.g. S2.notes, S2.layout or slideOrder. Spreadsheet chart field Tn.chart takes a JSON string: "
        "use expression/min/max or equal-length xRange/yRange, plus title, xTitle, yTitle, style "
        "(line/scatter), color and labels. Finish after a successful submission.",
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


def sheet_scene(d, field, computed):
    if not CHART_FIELD.fullmatch(field) or field not in d["fields"]:
        raise ValueError("未找到此图表，请先读取表格查看 charts。")
    return chart_scene(field, parse_chart(d["fields"][field]), d["fields"], computed)


def editor_state(d, chart_field="T1.chart"):
    computed = calculated(d["fields"]) if d["kind"] == "spreadsheet" else {}
    state = {
        **summary(d),
        "fields": d["fields"],
        "pending": d["pending"],
        "history": [
            {k: v for k, v in entry.items() if k != "changes"}
            for entry in reversed(d["history"])
        ],
        "computed": computed,
    }
    if d["kind"] == "spreadsheet" and chart_field in d["fields"] and CHART_FIELD.fullmatch(chart_field):
        state["chartScene"] = {"version": d["version"], **sheet_scene(d, chart_field, computed)}
    if d["kind"] == "presentation":
        state.update(slideOrder=slide_order(d["fields"]), slideDesign=DESIGN)
    return state


class OfficeController(QObject):
    changed = Signal()
    inspectChart = Signal(str, str, int, str, int)

    def __init__(self, workspace, parent=None):
        super().__init__(parent)
        self.workspace = workspace
        self._view_lock = Lock()
        self._chart_view_active = False
        self._view_requests = {}

    @Slot(bool)
    def setChartViewActive(self, active):
        with self._view_lock:
            self._chart_view_active = active
            if not active:
                for event, _ in self._view_requests.values():
                    event.set()

    @Slot(str, str)
    def finishChartView(self, token, raw):
        with self._view_lock:
            pending = self._view_requests.get(token)
            if pending and not pending[0].is_set():
                pending[1].append(raw)
                pending[0].set()

    def _viewport(self, doc, field, start):
        # Qt services WebEngine callbacks on the GUI thread. Never wait on it.
        if QThread.currentThread() == self.thread():
            return None
        token, event, reply = uuid4().hex, Event(), []
        with self._view_lock:
            if not self._chart_view_active:
                return None
            self._view_requests[token] = (event, reply)
        try:
            self.inspectChart.emit(token, doc["id"], doc["version"], field, start)
            event.wait(0.8)
            return json.loads(reply[0]) if reply else None
        finally:
            with self._view_lock:
                self._view_requests.pop(token, None)

    @Slot(str, result=str)
    def request(self, payload):
        try:
            a = json.loads(payload)
            store = OfficeWorkspace(self.workspace())
            action = a["action"]
            if action == "list":
                data = store.recent()
            elif action == "open":
                data = editor_state(store.load(a["id"]), a.get("chartField", "T1.chart"))
            elif action == "chart":
                d = store.load(a["id"])
                data = {"version": d["version"], **sheet_scene(d, a["field"], calculated(d["fields"]))}
            elif action == "create":
                data = editor_state(store.create(a["kind"], a["title"]))
            elif action == "export":
                data = str(export_file(store.root, store.load(a["id"])))
            elif action in ("apply", "mode", "accept", "reject", "undo", "arrange"):
                data = editor_state(store.change(a["id"], action, a.get("args", {})), a.get("chartField", "T1.chart"))
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
                d = store.read_for_tool(args["id"])
                start = args.get("offset", 0)
                if type(start) is not int or start < 0:
                    raise ValueError("页偏移无效。")
                if "chart" in args:
                    field = args["chart"]
                    scene = sheet_scene(d, field, calculated(d["fields"]))
                    points = scene.pop("points")
                    page = points[start:start + 12]
                    labels = {"v" + p["id"][1:] for p in page}
                    scene["texts"] = [t for t in scene["texts"] if not t["id"].startswith("v") or t["id"] in labels]
                    data = {**summary(d), **scene, "points": page, "point_count": len(points),
                            "next_offset": start + 12 if start + 12 < len(points) else None,
                            "coordinates": "cx/cy: chart viewBox, top-left origin; viewport: measured CSS pixels "
                                           "relative to Office editor, null when current layout unavailable.",
                            "viewport": self._viewport(d, field, min(start, len(points)))}
                else:
                    pairs = list(d["fields"].items())
                    data = {**summary(d), "fields": dict(pairs[start:start + 80]),
                            "next_offset": start + 80 if start + 80 < len(pairs) else None}
                    if d["kind"] == "spreadsheet":
                        data["charts"] = [k for k in d["fields"] if CHART_FIELD.fullmatch(k)]
                    if d["kind"] == "presentation":
                        data["slideOrder"] = slide_order(d["fields"])
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
            error = str(exc)
            if key == "office_read":
                error += " 可用 office_list 获取完整文件编号，再重新读取。"
            state = "unknown"
            if key not in OFFICE_WRITES or isinstance(exc, (KeyError, TypeError, ValueError, FileNotFoundError)):
                state = "not_started"
            if isinstance(exc, ToolFailure):
                state = exc.state
            return {"ok": False, "error": error, "meta": {"execution_state": state}}
