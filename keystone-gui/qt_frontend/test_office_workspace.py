from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest

from PySide6.QtCore import QUrl
from .office_controller import OfficeController
from .office_formats import calculated, export_file, import_file
from .office_workspace import OfficeWorkspace
from .permissions import authorize


class OfficeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = OfficeWorkspace(self.root)
        self.controller = OfficeController(lambda: self.root)
        self.cancel = Event()

    def ui(self, action, **args):
        response = json.loads(
            self.controller.request(json.dumps(dict(action=action, **args)))
        )
        self.assertTrue(response["ok"], response.get("error"))
        return response["data"]

    def tool(self, name, **args):
        tools = self.controller.tools(self.root)
        target = tools["keystone_office_" + name][1]
        family, key, root = target
        self.assertEqual(family, "office")
        result = self.controller.call(key, root, args, self.cancel)
        self.assertTrue(result["ok"], result.get("error"))
        return json.loads(result["content"])

    def test_user_and_work_tools_share_durable_suggestions(self):
        d = self.ui("create", kind="document", title="会议记录")
        delta = dict(field="P1", before="", value="可离线编辑")
        self.tool("apply", id=d["id"], changes=[delta], version=1, label="建议")
        replay = self.tool(
            "apply", id=d["id"], changes=[delta], version=1, label="建议"
        )
        self.assertEqual(replay["pending"], 1)
        reopened = self.ui("open", id=d["id"])
        self.assertEqual(reopened["fields"]["P1"], "")
        self.ui("accept", id=d["id"], args={"id": reopened["pending"][0]["id"]})
        self.assertEqual(self.tool("read", id=d["id"])["fields"]["P1"], "可离线编辑")
        self.ui("undo", id=d["id"], args={"author": "ai"})
        self.assertEqual(OfficeWorkspace(self.root).load(d["id"])["fields"]["P1"], "")
        self.assertEqual(self.tool("list")[0]["id"], d["id"])

    def test_interrupted_work_generation_reuses_create_and_independent_suggestion(self):
        from unittest.mock import patch
        from keystone_client import ChatMessage, CompletionResult, ModelStreamInterrupted
        from .conversation import run

        tools = self.controller.tools(self.root)
        attempts, dispatched = [], []
        def call(name, args):
            return {"id": f"call_{len(attempts)}_{name}", "type": "function",
                    "function": {"name": "keystone_office_" + name, "arguments": json.dumps(args)}}

        def dispatch(target, args, cancel):
            _, key, root = target
            dispatched.append(key)
            return self.controller.call(key, root, args, cancel)

        class Client:
            def list_tools(inner, **_):
                return []

            def stream_chat(inner, messages, **options):
                attempts.append(options.get("retryable_office_round", False))
                create = call("create", {"kind": "spreadsheet", "title": "预算", "fields": {"A2": "80"}})
                if len(attempts) == 1:
                    return CompletionResult("", tool_calls=[create])
                if len(attempts) == 2:
                    raise ModelStreamInterrupted("stalled before complete tool arguments")
                if len(attempts) == 5:
                    return CompletionResult("完成")
                document = self.store.recent()[0]
                apply = call("apply", {"id": document["id"], "version": 1, "label": "材料",
                                      "changes": [{"field": "A2", "before": "80", "value": "90"}]})
                return CompletionResult("", tool_calls=[create, apply] if len(attempts) == 3 else [apply])

        with patch("qt_frontend.conversation.MODEL_RETRY_DELAYS", (0,)):
            run(Client(), [ChatMessage("user", "调整预算")], model="Seed", effort=None,
                cancel=self.cancel, authorize=lambda *_: None, chunk=lambda *_: None,
                checkpoint=lambda *_: None, status=lambda *_: None, next_round=lambda: None,
                mcp=(tools, dispatch))
        self.assertEqual(attempts, [False, True, False, True, True])
        self.assertEqual(dispatched.count("office_create"), 1)
        self.assertEqual(len(self.store.recent()), 1)
        d = self.store.load(self.store.recent()[0]["id"])
        self.assertEqual(len(d["pending"]), 1)
        self.assertEqual(d["fields"]["A2"], "80")

    def test_conflicting_edits_preserve_user_text(self):
        d = self.tool("create", kind="document", title="说明")
        self.tool(
            "apply",
            id=d["id"],
            version=1,
            changes=[dict(field="P1", before="", value="AI")],
        )
        d = self.ui(
            "apply",
            id=d["id"],
            args=dict(version=1, changes=[dict(field="P1", before="", value="用户")]),
        )
        proposal = d["pending"][0]["id"]
        with self.assertRaisesRegex(ValueError, "变化"):
            self.store.change(d["id"], "accept", {"id": proposal})
        d = self.ui("reject", id=d["id"], args={"id": proposal})
        self.assertEqual(d["fields"]["P1"], "用户")
        self.assertFalse(d["pending"])

    def test_independent_edits_merge_and_same_cell_conflicts(self):
        d = self.store.create("spreadsheet", "预算")
        self.store.change(d["id"], "mode", {"value": "direct"})

        def set_cell(key):
            self.store.change(
                d["id"],
                "apply",
                dict(version=1, changes=[dict(field=key, before=None, value="12")]),
                "ai",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(set_cell, ["A1", "B2"]))
        with self.assertRaises(ValueError):
            set_cell("A1")
        d = self.store.load(d["id"])
        self.assertEqual([d["fields"][k] for k in ("A1", "B2")], ["12", "12"])

    def test_office_round_trips(self):
        examples = [
            ("document", {"P1": "议程", "P1.style": "heading", "P2": "跟进"}),
            (
                "spreadsheet",
                {
                    "T1.name": "预算",
                    "A1": "12",
                    "B1": "=A1*2",
                    "T2.name": "成本",
                    "T2.A1": "4",
                },
            ),
            (
                "presentation",
                {"S1": "提案", "S1.body": "下一步", "S2": "结尾", "S2.body": "谢谢"},
            ),
        ]
        for kind, fields in examples:
            with self.subTest(kind=kind):
                d = self.store.create(kind, "团队计划", fields)
                output = export_file(self.root, d)
                data = output.read_bytes()
                self.assertEqual(import_file(output), (kind, fields))
                result = json.loads(
                    self.controller.importSelected(QUrl.fromLocalFile(str(output)))
                )
                self.assertTrue(result["ok"], result)
                self.assertNotEqual(result["data"]["id"], d["id"])
                self.assertEqual(output.read_bytes(), data)

    def test_formulas_and_invalid_expressions(self):
        self.assertEqual(
            calculated(
                {
                    "A1": "3",
                    "A2": "5",
                    "B1": "=SUM(A1:A2)*2",
                    "B2": "=B1/4",
                    "C1": "=C1",
                    "C2": "=1 # comment",
                    "T2.A1": "12",
                    "T2.B2": "=A1-1",
                }
            ),
            {"B1": "16", "B2": "4", "C1": "#ERROR!", "C2": "#ERROR!", "T2.B2": "11"},
        )

    def test_workspace_and_permission_boundaries(self):
        from keystone.agent_templates import load_templates

        roles, errors = load_templates(None)
        self.assertFalse(errors)
        for role in roles:
            self.assertTrue(role.tool_allowed("keystone_office_read"), role.id)
            self.assertEqual(
                role.tool_allowed("keystone_office_apply"), role.id != "reviewer"
            )
        with self.assertRaises(ValueError):
            self.store.load("../other.kdoc")
        with self.assertRaises(ValueError):
            self.store.create("spreadsheet", "预算", {"T1.A1": "alias"})
        self.assertFalse(
            self.controller.call("office_list", self.root.parent, {}, self.cancel)["ok"]
        )
        with self.assertRaises(PermissionError):
            authorize(
                "manual",
                self.root,
                "keystone_office_create",
                {},
                lambda *_: False,
                self.cancel,
            )
        self.cancel.set()
        self.assertFalse(
            self.controller.call(
                "office_create",
                self.root,
                {"kind": "document", "title": "取消"},
                self.cancel,
            )["ok"]
        )
        self.assertFalse(self.store.recent())
