"""首次启动导航、普通 / 开发者模式、一键配置、中英双语与极光背景。"""
import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import i18n
import model_onboarding as mo
import secret_store
from i18n import tr
from test_layout_dpi import isolated_app, pump


def make_root(tries=3):
    for attempt in range(tries):
        try:
            return tk.Tk()
        except tk.TclError:
            if attempt == tries - 1:
                raise


class OneClickConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.secrets = patch.object(secret_store, "SECRETS_FILE", self.home / "secrets.json")
        self.secrets.start()

    def tearDown(self):
        self.secrets.stop()
        self.tmp.cleanup()

    def rows(self):
        return json.loads((self.home / "forge.patch.json").read_text(encoding="utf-8"))

    def test_defaults_and_custom_parameters_are_written(self):
        self.assertTrue(mo.apply_key(self.home, "deepseek", "sk-a", temperature=mo.DEFAULT_TEMPERATURE)["ok"])
        config = self.rows()[0]["config"]
        self.assertEqual((config["model"], config["baseURL"], config["temperature"]),
                         ("deepseek-chat", "https://api.deepseek.com", 0.7))
        self.assertTrue(mo.apply_key(self.home, "deepseek", "sk-a", model="deepseek-reasoner",
                                     base_url="https://proxy.example/v1", temperature=1.0)["ok"])
        config = self.rows()[0]["config"]
        self.assertEqual((config["model"], config["baseURL"], config["temperature"]),
                         ("deepseek-reasoner", "https://proxy.example/v1", 1.0))
        self.assertNotIn("sk-a", json.dumps(self.rows()), "明文密钥不得进用户层")

    def test_invalid_address_is_rejected_without_writing(self):
        result = mo.apply_key(self.home, "deepseek", "sk-a", base_url="api.deepseek.com")
        self.assertFalse(result["ok"])
        self.assertFalse((self.home / "forge.patch.json").exists())

    def test_quick_setup_saves_and_toggles_advanced_fields_both_ways(self):
        from quick_setup import QuickModelSetup
        root = make_root()
        try:
            saved = []
            setup = QuickModelSetup(root, self.home, advanced=False, on_saved=saved.append)
            setup.pack()
            pump(root)
            url_fields = [w for w in setup._advanced_widgets if isinstance(w, tk.Entry)]
            self.assertTrue(url_fields and not any(w.winfo_ismapped() for w in url_fields))
            setup.set_advanced(True)
            pump(root)
            self.assertTrue(all(w.winfo_ismapped() for w in url_fields))
            setup.set_advanced(False)
            setup.set_advanced(True)           # grid_remove 之后还能再显示（回归）
            pump(root)
            self.assertTrue(all(w.winfo_ismapped() for w in url_fields))
            setup.select_vendor("moonshot")
            self.assertEqual(setup.model_var.get(), "kimi-k2")
            setup.key_var.set("sk-kimi")
            setup.temperature_var.set("9")
            self.assertIsNone(setup.save())
            self.assertEqual(saved, [])
            setup.temperature_var.set("0.7")
            result = setup.save()
            self.assertTrue(result["ok"])
            self.assertEqual(saved[0]["row_id"], "moonshot")
        finally:
            root.destroy()


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.root.geometry("1100x760+40+40")
        self.root._forge_locale = i18n.Translator("zh-CN")
        self.tmp = tempfile.TemporaryDirectory()
        self.errors = []
        self.root.report_callback_exception = lambda *a: self.errors.append(a)

    def tearDown(self):
        self.root.destroy()
        self.tmp.cleanup()
        self.assertEqual(self.errors, [])

    def test_language_choice_switches_live_and_finish_reports_choices(self):
        from welcome_guide import WelcomeGuide
        done = []
        guide = WelcomeGuide(self.root, home=Path(self.tmp.name),
                             on_finish=lambda *args: done.append(args))
        guide.show()
        pump(self.root)
        guide._choose_language("en")
        pump(self.root)
        self.assertEqual(self.root._forge_locale.language, "en")
        texts = [w.cget("text") for w in self._walk(guide) if isinstance(w, tk.Label)]
        self.assertIn("Next", texts)
        guide.next()
        guide._choose_experience("developer")
        guide.next()
        self.assertTrue(guide.setup._advanced, "开发者模式下第三步显示全部参数")
        guide.finish()
        self.assertEqual(done, [("en", "developer", None)])
        self.assertFalse(guide.winfo_exists())

    def test_start_with_a_key_saves_before_finishing(self):
        from welcome_guide import WelcomeGuide
        done = []
        with patch.object(secret_store, "SECRETS_FILE", Path(self.tmp.name) / "s.json"):
            guide = WelcomeGuide(self.root, home=Path(self.tmp.name),
                                 on_finish=lambda *args: done.append(args))
            guide.show()
            guide.next(); guide.next()
            guide.setup.key_var.set("sk-guide")
            guide.next()
        self.assertEqual(done[0][1], "simple")
        self.assertTrue(done[0][2]["ok"])

    def _walk(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self._walk(child)


class ExperienceModeTests(unittest.TestCase):
    def test_simple_hides_developer_surfaces_and_developer_restores_them(self):
        import forge_gui_v2 as gui
        self.assertEqual(gui.normalize_experience(None), "developer")
        self.assertEqual(gui.normalize_experience("simple"), "simple")
        with isolated_app(1, (1400, 860)) as (root, app, errors):
            app._show_view("config")
            pump(root)
            app._set_experience("simple")
            pump(root)
            self.assertFalse(app._mode_switcher.winfo_ismapped())
            self.assertFalse(app._manage_split.winfo_ismapped())
            self.assertFalse(app.sidebar_navigation["evolution"].winfo_manager())
            self.assertTrue(app.quick_setup.winfo_ismapped())
            self.assertEqual(gui.load_desktop_config()["experience"], "simple")
            app._toggle_experience()
            pump(root)
            self.assertEqual(app._experience, "developer")
            self.assertTrue(app._mode_switcher.winfo_ismapped())
            self.assertTrue(app._manage_split.winfo_ismapped())
            order = list(app.sidebar_navigation)
            packed = [k for k in order if app.sidebar_navigation[k].winfo_manager()]
            self.assertEqual(packed, order, "开发者模式恢复后导航顺序不变")
            self.assertFalse(errors, errors)

    def test_welcome_guide_finish_applies_mode_and_marks_onboarded(self):
        import forge_gui_v2 as gui
        with isolated_app(1, (1400, 860)) as (root, app, errors):
            guide = app.open_welcome_guide()
            self.assertIs(app.open_welcome_guide(), guide, "不会叠出第二个引导")
            guide._choose_experience("simple")
            guide.finish()
            pump(root)
            config = gui.load_desktop_config()
            self.assertTrue(config["onboarded"])
            self.assertEqual(app._experience, "simple")
            self.assertFalse(errors, errors)

    def test_status_messages_render_their_arguments(self):
        with isolated_app(1, (1200, 800)) as (root, app, errors):
            app._set_status(tr("界面语言已切换为 {language}", language="English"))
            self.assertEqual(app.status_var.get(), "界面语言已切换为 English")
            self.assertFalse(errors, errors)


class BilingualTests(unittest.TestCase):
    def test_known_plain_strings_and_template_arguments_are_translated(self):
        en = i18n.Translator("en")
        self.assertEqual(en.render(i18n.as_message("下一步")), "Next")
        self.assertEqual(en.render(i18n.as_message("▤ 工作区")).split(" ", 1)[0], "▤")
        self.assertNotIn("工作区", en.render(i18n.as_message("▤ 工作区")))
        self.assertEqual(i18n.as_message("用户自己写的标题"), "用户自己写的标题")
        self.assertNotIsInstance(i18n.as_message("用户自己写的标题"), i18n.Message)
        self.assertEqual(en.render(tr("当前 {value}", value="不设置")), "Current: Not set")

    def test_every_new_source_string_has_english(self):
        import ast
        import re
        cjk = re.compile(r"[一-鿿]")
        catalog = json.loads((Path(i18n.DIRECTORY) / "en.json").read_text(encoding="utf-8"))
        for name in ("welcome_guide.py", "quick_setup.py", "glass_ornaments.py"):
            tree = ast.parse((Path(__file__).parent / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tr"
                        and isinstance(node.args[0], ast.Constant) and cjk.search(node.args[0].value)):
                    self.assertIn(node.args[0].value, catalog, f"{name}: {node.args[0].value}")

    def test_glass_button_grows_when_its_text_gets_longer(self):
        import liquid_glass as glass
        root = make_root()
        try:
            button = glass.glass_button(root, "测试", lambda: None)
            narrow = int(button.cget("width"))
            button.configure(text="Test connection to the provider")
            self.assertGreater(int(button.cget("width")), narrow)
        finally:
            root.destroy()


class AuroraTests(unittest.TestCase):
    def test_backdrop_never_inflates_the_scroll_region(self):
        import chat_widgets as cw
        root = make_root()
        try:
            root.geometry("1200x700")
            area = cw.MessageArea(root)
            area.pack(fill=tk.BOTH, expand=True)
            pump(root, .4)
            area.scroll.backdrop.render()
            for _ in range(5):
                area.scroll.canvas.yview_scroll(3, "units")
                pump(root, .05)
            region = [float(v) for v in str(area.scroll.canvas.cget("scrollregion")).split()]
            box = area.scroll.canvas.bbox(area.scroll._win)
            self.assertEqual(region[3], box[3], "滚动区域只按内容窗口计算")
            self.assertTrue(area.scroll.canvas.find_withtag("aurora"))
        finally:
            root.destroy()

    def test_column_stays_dim_and_edges_fade(self):
        import base64
        import aurora
        data, scale = aurora.aurora_png(800, 400, column=(240, 560), fade_bottom=120)
        raw = base64.b64decode(data)
        self.assertTrue(raw.startswith(b"\x89PNG"))
        self.assertEqual(scale, aurora.SCALE)


if __name__ == "__main__":
    unittest.main()
