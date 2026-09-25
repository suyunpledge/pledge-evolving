"""Regression checks for configuration safety, layout, and asynchronous UI behavior.

All writes use temporary directories; no real gateway or model is contacted.
Run: python -m unittest test_gui_review -v
"""
import copy
import json
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import forge_gui_v2 as gui
from config_model import ConfigNormalizeError, load_user_layer, normalize, save_user_layer


def sample_rows():
    return [
        {"id": "demo", "name": "provider:demo", "config": {
            "wire": "openai", "baseURL": "https://example.invalid/v1", "model": "demo-model",
            "apiKey": {"$expr": "get('env.DEMO_KEY', '')"}}},
        {"id": "model", "config": {"primary": ["demo", "demo-model"], "moa": False}},
        {"id": "channel:demo", "config": {"enabled": True, "accountId": "demo-account"}},
    ]


class ConfigReviewTests(unittest.TestCase):
    def test_repeated_normalization_preserves_router_channel_and_disabled(self):
        rows = sample_rows()
        rows[0]["disabled"] = True
        rows[0]["inject"] = {"tag": "keep"}
        once = normalize(json.dumps(rows), add_model=False).rows
        twice = normalize(json.dumps(once), add_model=False).rows
        self.assertEqual(once, twice)
        self.assertEqual(twice[1:], rows[1:])
        self.assertTrue(twice[0]["disabled"])
        self.assertEqual(twice[0]["inject"], {"tag": "keep"})

    def test_provider_edit_does_not_create_empty_router(self):
        rows = normalize(json.dumps(sample_rows()[0]), add_model=False).rows
        self.assertEqual([r["id"] for r in rows], ["demo"])

    def test_malformed_json_is_not_silently_guessed(self):
        with self.assertRaises(ConfigNormalizeError):
            normalize('{"id":"demo","baseURL":"https://example.invalid",')

    def test_missing_url_does_not_leave_plaintext_key(self):
        rows = normalize('{"id":"demo","apiKey":"example-test-secret"}').rows
        self.assertNotIn("example-test-secret", json.dumps(rows))
        self.assertTrue(rows[0]["disabled"])

    def test_invalid_file_is_not_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / "forge.patch.json"
            fp.write_text('{"broken":', encoding="utf-8")
            with self.assertRaises(ConfigNormalizeError):
                save_user_layer(Path(tmp), sample_rows())
            self.assertEqual(fp.read_text(encoding="utf-8"), '{"broken":')

    def test_replace_failure_preserves_original_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            original = sample_rows()
            save_user_layer(home, original)
            changed = copy.deepcopy(original)
            changed[1]["config"]["moa"] = True
            with patch("config_model.os.replace", side_effect=OSError("simulated disk failure")):
                with self.assertRaises(OSError):
                    save_user_layer(home, changed)
            self.assertEqual(load_user_layer(home), original)
            self.assertFalse(list(home.glob(".forge-*.tmp")))


class GuiReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui._setup_dpi()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        save_user_layer(self.home, sample_rows())
        self.root = tk.Tk()
        with patch.object(gui, "DEFAULT_FORGE_HOME", self.home), patch.object(gui, "_find_run_py", return_value=None):
            self.app = gui.ForgeGuiApp(self.root)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.root.update()

    def tearDown(self):
        self.app._closing = True
        self.root.after_cancel(self.app._event_poll)
        self.root.destroy()
        self.tmp.cleanup()
        self.assertEqual(self.errors, [])

    def entry(self, row_id, kind="config"):
        return next(e for e in self.app._feature_entries if e["row_id"] == row_id and e["kind"] == kind)

    def pump_until(self, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)
        self.assertTrue(condition())

    def test_toggle_back_restores_clean_state(self):
        entry = self.entry("model")
        entry["var"].set(True)
        self.app._mark_features_dirty()
        self.assertTrue(self.app._feature_dirty)
        entry["var"].set(False)
        self.app._mark_features_dirty()
        self.assertFalse(self.app._feature_dirty)
        self.assertEqual(self.app.feature_save_btn["state"], "disabled")

    def test_toggle_save_preserves_external_unrelated_changes(self):
        latest = load_user_layer(self.home)
        latest[2]["config"]["accountId"] = "changed-externally"
        latest[2]["config"]["enabled"] = False
        save_user_layer(self.home, latest)
        self.entry("model")["var"].set(True)
        self.app._mark_features_dirty()
        self.app._save_feature_toggles()
        saved = load_user_layer(self.home)
        self.assertTrue(saved[1]["config"]["moa"])
        self.assertEqual(saved[2], latest[2])

    def test_deleted_toggle_target_is_not_recreated(self):
        save_user_layer(self.home, sample_rows()[:2])
        self.entry("channel:demo")["var"].set(False)
        self.app._mark_features_dirty()
        self.app._save_feature_toggles()
        self.assertTrue(self.app._feature_dirty)
        self.assertEqual(len(load_user_layer(self.home)), 2)

    def test_disabled_provider_disappears_from_model_choices(self):
        self.entry("demo", "provider")["var"].set(False)
        self.app._mark_features_dirty()
        self.app._save_feature_toggles()
        self.assertNotIn("demo-model", self.app.model_combo["values"])

    def test_stale_editor_cannot_override_saved_switch(self):
        self.app.provider_list.selection_set(1)
        self.app._on_provider_select()
        self.app._do_organize()
        self.entry("model")["var"].set(True)
        self.app._mark_features_dirty()
        self.app._save_feature_toggles()
        self.app._do_save()
        self.assertTrue(load_user_layer(self.home)[1]["config"]["moa"])
        self.assertIn("编辑期间已变化", self.app.status_var.get())

    def test_minimum_layout_has_reachable_controls_and_scrollable_editor(self):
        self.root.geometry("940x700")
        self.root.update()
        self.assertTrue(self.app.feature_save_btn.winfo_ismapped())
        self.assertTrue(self.app.status_lbl.winfo_ismapped())
        self.assertGreater(self.app.feature_canvas.winfo_height(), 100)
        self.app.tab_manage.master.select(self.app.tab_manage)
        self.root.update()
        self.assertTrue(self.app.save_btn.winfo_ismapped())
        self.assertGreater(self.app.preview_text.winfo_height(), 80)
        self.app.editor_canvas.yview_moveto(1)
        self.root.update()
        self.assertTrue(self.app.warn_text.winfo_ismapped())

    def test_provider_save_preserves_router_and_channel(self):
        edited = copy.deepcopy(sample_rows()[0])
        edited["config"]["model"] = "updated-model"
        self.app._clear_placeholder()
        self.app.input_text.insert("1.0", json.dumps(edited))
        self.app._do_organize()
        self.app._do_save()
        saved = load_user_layer(self.home)
        self.assertEqual(saved[0]["config"]["model"], "updated-model")
        self.assertEqual(saved[1:], sample_rows()[1:])

    def test_unsaved_raw_draft_can_cancel_close(self):
        self.app._clear_placeholder()
        self.app.input_text.insert("1.0", "unsaved text")
        with patch.object(gui.messagebox, "askyesno", return_value=False) as question:
            self.app._on_close()
        question.assert_called_once()
        self.assertFalse(self.app._closing)

    def test_old_gateway_callbacks_do_not_reset_new_process(self):
        current = Mock()
        current.poll.return_value = None
        old = Mock()
        old.poll.return_value = 1
        self.app.gateway_proc = current
        before = self.app.gw_status_var.get()
        self.app._gateway_exited(old)
        self.app._gateway_up(old)
        self.app._gateway_timeout(old)
        self.assertIs(self.app.gateway_proc, current)
        self.assertEqual(self.app.gw_status_var.get(), before)

    def test_many_switches_remain_scrollable_and_save_visible(self):
        self.app.user_rows += [{"id": f"feature:{i}", "config": {"enabled": True}} for i in range(30)]
        self.app._rebuild_feature_toggles(force=True)
        self.root.geometry("940x700")
        self.root.update()
        self.assertTrue(self.app.feature_save_btn.winfo_ismapped())
        self.assertLess(self.app.feature_canvas.yview()[1], 1)
        self.app.feature_canvas.yview_moveto(1)
        self.assertGreater(self.app.feature_canvas.yview()[0], 0)

    def test_async_request_blocks_duplicate_send_and_clear(self):
        release = threading.Event()
        calls = []
        class Client:
            def health(self):
                release.wait(1)
                return True, "ok"
            def stream_chat(self, messages, **kwargs):
                calls.append(messages)
                kwargs["on_chunk"]("reply")
        self.app.client = Client()
        self.app.send_var.set("hello")
        started = time.monotonic()
        self.app._do_send()
        self.assertLess(time.monotonic() - started, .25)
        self.app.send_var.set("next draft")
        self.app._do_send()
        self.app._clear_chat()
        release.set()
        self.pump_until(lambda: not self.app._sending)
        self.assertEqual(len(calls), 1)
        self.assertEqual([m.content for m in self.app._chat_history], ["hello", "reply"])
        self.assertEqual(self.app.send_var.get(), "next draft")

    def test_failure_keeps_retry_text_without_polluting_history(self):
        class Client:
            def health(self): return False, "test offline"
        self.app.client = Client()
        self.app.send_var.set("retry this")
        self.app._do_send()
        self.pump_until(lambda: not self.app._sending)
        self.assertEqual(self.app.send_var.get(), "retry this")
        self.assertEqual(self.app._chat_history, [])


if __name__ == "__main__":
    unittest.main()
