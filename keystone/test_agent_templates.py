"""Role templates (keystone.agent_templates): loading, validation, no escalation, per-role effects."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from . import agent_templates as at
from .agent_templates import AgentTemplate, TemplateError, apply_template, clamp_policy
from .assembly import build_agent
from .capability import BUNDLED_ROOT
from .cli import BUNDLE_DIR, PROFILE_PRESETS, main
from .config import USER_LAYER_NAME, Config, load_config
from .loop_limits import resolve_loop_limits
from .policy import Decision, Mode, Policy, Sandbox, WRITE_TOOLS
from .test_project_rules import Router

NAMES = {"fullstack": "全栈开发师", "modeler": "建模设计师", "tutor": "学习助手", "butler": "智能管家",
         "writer": "写作助手", "reviewer": "代码审查员", "researcher": "信息调研师"}
RANK = {Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 2}


def raw(**changes):
    base = AgentTemplate.from_raw(json.loads((at.BUILTIN_DIR / "writer.json").read_text(encoding="utf-8"))).to_raw()
    base.update(id="custom", name="自定义")
    base.update(changes)
    return base


def composed(policy=None, *, user_rows=(), expose=None):
    """base bundle + a ceiling policy (as --profile would leave it) + optional user layer rows."""
    cfg = load_config(Path(tempfile.gettempdir()), bundles=[BUNDLE_DIR / "base.json"], include_user_layer=False)
    if policy is not None:
        cfg.apply_patch([{"id": "policy", "name": "policy:core", "config": dict(policy)}], label="profile")
    if expose is not None:
        cfg.apply_patch([{"id": "tools", "name": "tools:expose", "config": {"expose": list(expose)}}], label="x")
    if user_rows:
        cfg.apply_patch(list(user_rows), label=USER_LAYER_NAME)
    return cfg


def policy_of(cfg, workspace):
    return Policy.from_config(cfg, workspace=workspace, non_interactive=False)


class LoadingTests(unittest.TestCase):
    def test_seven_builtins_ship_with_their_display_names(self):
        templates, problems = at.load_templates(None)
        self.assertEqual(problems, [])
        self.assertEqual({t.id: t.name for t in templates}, NAMES)
        self.assertEqual([t.id for t in templates], list(NAMES))  # user's own order
        known = at.known_tools()
        for t in templates:
            with self.subTest(t.id):
                self.assertTrue(t.builtin and t.description and t.guidance and t.icon)
                self.assertGreaterEqual(len(t.starters), 3)
                unknown = (set(t.expose) | set(t.allow) | set(t.prefer)) - known
                self.assertEqual(unknown, set(), "template names a tool that does not exist")
                self.assertTrue(set(t.prefer) <= set(t.expose))
                self.assertIn(t.loop.get("maxSteps"), range(1, 201))
                # round trip is lossless
                self.assertEqual(AgentTemplate.from_raw(t.to_raw(), builtin=True, path=t.path), t)

    def test_icons_exist_in_the_desktop_icon_set(self):
        lucide = Path(__file__).resolve().parents[1] / "keystone-gui" / "assets" / "ui" / "lucide"
        if not lucide.is_dir():
            self.skipTest("desktop assets not in this checkout")
        for t in at.builtin_templates():
            self.assertTrue((lucide / f"{t.icon}.svg").is_file(), t.icon)

    def test_modeler_leans_on_the_model3d_tools_and_skill(self):
        t = at.get_template("modeler")
        self.assertIn("model3d", t.skills)
        self.assertTrue((BUNDLED_ROOT / "model3d" / "SKILL.md").is_file())
        self.assertTrue({"model3d_build", "model3d_inspect"} <= set(t.expose))
        self.assertIn('skill_list(name="model3d")', t.role_text())

    def test_validation_rejects_what_a_template_may_not_grant(self):
        bad = {
            "bypass mode": raw(policy={"mode": "bypassPermissions"}),
            "dontAsk mode": raw(policy={"mode": "dontAsk"}),
            "full sandbox": raw(policy={"sandbox": "danger-full-access"}),
            "wildcard allow": raw(policy={"allow": ["*"]}),
            "prefix allow": raw(policy={"allow": ["git_*"]}),
            "read roots": raw(policy={"readRoots": ["C:/"]}),
            "workspace": raw(policy={"workspace": "C:/"}),
            "unknown key": raw(extra=1),
            "bad id": raw(id="Bad Id"),
            "loop too big": raw(loop={"maxSteps": 100000}),
            "loop wrong key": raw(loop={"contextChars": 1}),
            "bool loop": raw(loop={"maxSteps": True}),
            "planning": raw(planning="ultra"),
            "starters": raw(starters=["x" * 500]),
            "no guidance": raw(guidance=" "),
            "schema": raw(schema=2),
        }
        for label, value in bad.items():
            with self.subTest(label), self.assertRaises(TemplateError):
                AgentTemplate.from_raw(value)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_duplicate_customise_persist_delete(self):
        copy = at.duplicate_template(self.home, "reviewer")
        self.assertEqual((copy.id, copy.based_on, copy.builtin), ("reviewer-copy", "reviewer", False))
        self.assertTrue((self.home / "agents" / "reviewer-copy.json").is_file())
        second = at.duplicate_template(self.home, "reviewer", name="严格审查")
        self.assertEqual((second.id, second.name), ("reviewer-copy2", "严格审查"))
        edited = copy.to_raw()
        edited.update(name="安全审查", guidance="只看安全问题。", starters=["查注入"])
        at.save_template(self.home, edited)
        templates, problems = at.load_templates(self.home)
        self.assertEqual(problems, [])
        mine = {t.id: t for t in templates if not t.builtin}
        self.assertEqual(mine["reviewer-copy"].name, "安全审查")
        # a copy keeps the source's limits: still read-only
        self.assertEqual(mine["reviewer-copy"].mode, Mode.READ_ONLY)
        at.delete_template(self.home, "reviewer-copy")
        self.assertNotIn("reviewer-copy", {t.id for t in at.load_templates(self.home)[0]})

    def test_builtins_cannot_be_overwritten_or_deleted(self):
        with self.assertRaises(TemplateError):
            at.save_template(self.home, at.get_template("fullstack").to_raw())
        with self.assertRaises(TemplateError):
            at.delete_template(self.home, "fullstack")
        with self.assertRaises(TemplateError):
            at.delete_template(self.home, "../evil")

    def test_broken_or_shadowing_user_files_are_reported_not_fatal(self):
        folder = self.home / "agents"
        folder.mkdir()
        (folder / "broken.json").write_text("{not json", encoding="utf-8")
        (folder / "fullstack.json").write_text(json.dumps(raw(id="fullstack")), encoding="utf-8")
        (folder / "escalate.json").write_text(json.dumps(raw(id="escalate", policy={"mode": "bypassPermissions"})),
                                              encoding="utf-8")
        (folder / "renamed.json").write_text(json.dumps(raw(id="other")), encoding="utf-8")
        templates, problems = at.load_templates(self.home)
        self.assertEqual(len(problems), 4)
        self.assertEqual([t.id for t in templates], list(NAMES))
        self.assertTrue(all(t.builtin for t in templates))


class NoEscalationTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_conservative_user_keeps_fullstack_read_only(self):
        cfg = composed(PROFILE_PRESETS["conservative"])
        applied = apply_template(cfg, at.get_template("fullstack"), workspace=self.ws)
        policy = policy_of(cfg, self.ws)
        self.assertEqual((policy.mode, policy.sandbox), (Mode.READ_ONLY, Sandbox.READ_ONLY))
        self.assertIs(policy.evaluate("shell_exec"), Decision.DENY)
        self.assertIsNot(policy.evaluate("write_file"), Decision.ALLOW)
        self.assertNotIn("write_file", policy.allow)
        self.assertTrue(applied.narrowed)

    def test_user_deny_and_ask_rules_win(self):
        ceiling = dict(PROFILE_PRESETS["balanced"], deny=["web_search", "fetch_url"], ask=["write_file", "shell_exec"])
        cfg = composed(ceiling)
        applied = apply_template(cfg, at.get_template("researcher"), workspace=self.ws)
        policy = policy_of(cfg, self.ws)
        self.assertIs(policy.evaluate("web_search"), Decision.DENY)
        self.assertNotIn("web_search", applied.expose)
        cfg = composed(ceiling)
        apply_template(cfg, at.get_template("writer"), workspace=self.ws)
        self.assertIs(policy_of(cfg, self.ws).evaluate("write_file"), Decision.ASK)

    def test_dont_ask_ceiling_still_refuses_writes(self):
        cfg = composed(dict(PROFILE_PRESETS["balanced"], mode="dontAsk", allow=["read_file"]))
        apply_template(cfg, at.get_template("fullstack"), workspace=self.ws)
        policy = policy_of(cfg, self.ws)
        for tool in ("write_file", "edit_file", "apply_patch"):
            self.assertIs(policy.evaluate(tool), Decision.DENY, tool)

    def test_template_cannot_widen_sandbox_or_read_scope(self):
        ceiling = dict(PROFILE_PRESETS["balanced"], sandbox="read-only", readScope="workspace",
                       readRoots=[str(self.ws / "docs")])
        evil = AgentTemplate.from_raw(raw(policy={"mode": "auto", "sandbox": "workspace-write", "readScope": "all",
                                                  "allow": ["write_file", "shell_exec"]}))
        conf, notes = clamp_policy(ceiling, evil, workspace=self.ws)
        self.assertEqual((conf["sandbox"], conf["readScope"]), ("read-only", "workspace"))
        self.assertEqual(conf["readRoots"], [str(self.ws / "docs")])
        self.assertNotIn("shell_exec", conf["allow"])
        self.assertTrue(notes)
        outside = Path(self.ws.anchor) / "Windows" / "x.txt"
        result = Policy.from_config(at._RowView({"policy": conf}), workspace=self.ws)
        self.assertIs(result.evaluate("read_file", args={"path": str(outside)}), Decision.DENY)

    def test_every_builtin_under_every_ceiling_is_no_more_permissive(self):
        ceilings = {name: dict(preset) for name, preset in PROFILE_PRESETS.items()}
        ceilings["base-default"] = None
        ceilings["custom-deny"] = dict(PROFILE_PRESETS["aggressive"], deny=["read_file", "web_*"], ask=["edit_file"])
        probes = sorted(at.known_tools() | WRITE_TOOLS | {"mcp_remote_tool", "plugin.unknown"})
        for cname, ceiling in ceilings.items():
            for template in at.builtin_templates():
                with self.subTest(ceiling=cname, template=template.id):
                    before = composed(ceiling)
                    cap = policy_of(before, self.ws)
                    after = composed(ceiling)
                    apply_template(after, template, workspace=self.ws)
                    got = policy_of(after, self.ws)
                    self.assertLessEqual(got.mode.rank, cap.mode.rank)
                    self.assertLessEqual(at._SANDBOX_RANK[got.sandbox], at._SANDBOX_RANK[cap.sandbox])
                    if got.read_allows_anywhere():
                        self.assertTrue(cap.read_allows_anywhere())
                    for tool in probes:
                        self.assertGreaterEqual(RANK[got.evaluate(tool)], RANK[cap.evaluate(tool)], tool)
                    self.assertTrue(set(cap.deny) <= set(got.deny))

    def test_user_loop_values_are_a_ceiling(self):
        cfg = composed(PROFILE_PRESETS["balanced"], user_rows=[{"id": "loop", "config": {"maxSteps": 10}}])
        applied = apply_template(cfg, at.get_template("fullstack"), workspace=self.ws)
        self.assertEqual(resolve_loop_limits(cfg, use_hint=False).max_steps, 10)
        self.assertEqual(applied.loop["maxDepth"], 3)  # undeclared keys still take the template value

    def test_user_expose_list_is_intersected(self):
        cfg = composed(PROFILE_PRESETS["balanced"], expose=["read_file", "grep", "web_search"])
        applied = apply_template(cfg, at.get_template("fullstack"), workspace=self.ws)
        self.assertEqual(applied.expose, ["read_file", "grep", "web_search"])


class AppliedRoleTests(unittest.TestCase):
    """Each built-in, wired through build_agent, gets its intended tools, limits and guidance."""

    def test_each_builtin_applies_its_tools_and_limits(self):
        for template in at.builtin_templates():
            with self.subTest(template.id), tempfile.TemporaryDirectory() as folder:
                ws = Path(folder)
                cfg = composed(PROFILE_PRESETS["balanced"])
                apply_template(cfg, template, workspace=ws)
                agent = build_agent(home=ws / "home", workspace=ws, config=cfg, router=Router([]),
                                    expose=cfg.get("tools", "expose"), mount_contrib=False)
                self.addCleanup(agent.secret_scope.close)
                visible = set(agent.registry.names())
                registered = {s.name for s in agent.registry.all_specs()}
                expected = {t for t in template.expose if t in registered and t not in template.deny}
                deferred = {s.name for s in agent.registry.all_specs() if s.is_deferred}
                self.assertEqual(visible | (expected & deferred), expected)
                self.assertEqual(agent.limits.max_steps, template.loop["maxSteps"])
                self.assertEqual(agent.limits.max_depth, template.loop["maxDepth"])
                self.assertEqual(agent.limits.spawn_budget, template.loop["spawnBudget"])
                self.assertEqual(agent.planning_level, template.planning)
                self.assertIn(f"[角色模板：{template.name}]", agent.run_envelope())
                self.assertEqual(agent.policy.mode, template.mode)
                for tool in template.deny:
                    self.assertIs(agent.policy.evaluate(tool), Decision.DENY, tool)

    def test_reviewer_is_read_only_and_researcher_cannot_write_outside(self):
        with tempfile.TemporaryDirectory() as folder:
            ws = Path(folder)
            cfg = composed(PROFILE_PRESETS["aggressive"])
            apply_template(cfg, at.get_template("reviewer"), workspace=ws)
            reviewer = policy_of(cfg, ws)
            for tool in ("write_file", "edit_file", "apply_patch", "shell_exec", "git_commit"):
                self.assertIs(reviewer.evaluate(tool, args={"path": "a.txt"}), Decision.DENY, tool)
            self.assertIs(reviewer.evaluate("read_file", args={"path": "a.txt"}), Decision.ALLOW)
            cfg = composed(PROFILE_PRESETS["aggressive"])
            apply_template(cfg, at.get_template("researcher"), workspace=ws)
            researcher = policy_of(cfg, ws)
            self.assertIs(researcher.evaluate("web_search"), Decision.ALLOW)
            outside = str(Path(ws.anchor) / "elsewhere" / "report.md")
            self.assertIs(researcher.evaluate("write_file", args={"path": outside}), Decision.DENY)
            self.assertIs(researcher.evaluate("write_file", args={"path": "report.md"}), Decision.ASK)
            cfg = composed(PROFILE_PRESETS["balanced"])
            apply_template(cfg, at.get_template("fullstack"), workspace=ws)
            fullstack = policy_of(cfg, ws)
            self.assertIs(fullstack.evaluate("write_file", args={"path": "src/a.py"}), Decision.ALLOW)
            self.assertIs(fullstack.evaluate("write_file", args={"path": outside}), Decision.DENY)


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_template_flag_layers_after_profile(self):
        with tempfile.TemporaryDirectory() as home:
            code, out, _ = self.run_cli("--home", home, "--profile", "balanced", "--template", "reviewer",
                                        "dump-config")
            self.assertEqual(code, 0)
            rows = {row["id"]: row.get("config", {}) for row in json.loads(out)}
            self.assertEqual((rows["policy"]["mode"], rows["policy"]["sandbox"]), ("read-only", "read-only"))
            self.assertEqual(rows["loop"]["maxSteps"], 30)
            self.assertEqual(rows["role"]["template"], "reviewer")
            self.assertNotIn("write_file", rows["tools"]["expose"])

    def test_templates_command_lists_and_duplicates(self):
        with tempfile.TemporaryDirectory() as home:
            code, out, _ = self.run_cli("--home", home, "templates", "list", "--json")
            self.assertEqual(code, 0)
            self.assertEqual([row["id"] for row in json.loads(out)], list(NAMES))
            code, out, _ = self.run_cli("--home", home, "templates", "duplicate", "tutor", "--name", "数学家教")
            self.assertEqual(code, 0)
            self.assertTrue((Path(home) / "agents" / "tutor-copy.json").is_file())
            code, out, _ = self.run_cli("--home", home, "--template", "tutor-copy", "dump-config")
            self.assertEqual(json.loads(out)[-1]["config"]["name"], "数学家教")

    def test_unknown_template_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as home, self.assertRaises(SystemExit) as caught:
            self.run_cli("--home", home, "--template", "nope", "dump-config")
        self.assertIn("nope", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
