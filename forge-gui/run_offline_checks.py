"""Run desktop regressions without using the real user's state or API keys."""
from contextlib import ExitStack
from pathlib import Path
import importlib
import inspect
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import forge_gui_v2 as gui
import secret_store
import sub_agent
import plugin_market


def main():
    names = sys.argv[1:] or [
        "test_sysmon", "test_ui_icons", "test_chat_widgets", "test_gui_review",
        "test_interactions", "test_refactor_v3", "test_sub_agent", "test_runtime_review",
        "test_ui_ergonomics", "test_startup_responsiveness",
        "test_full_review", "test_config_model", "test_forge_client", "test_integration",
        "test_workspace", "test_ui_regression", "test_layout_dpi", "test_navigation",
        "test_plugin_market", "test_plugin_runtime", "test_plugin_adversarial",
        "test_plugin_capabilities", "test_plugin_market_ui", "test_compat_sources", "test_i18n", "test_remote_sources", "test_ime_inline",
        "test_market_responsiveness",
    ]
    with tempfile.TemporaryDirectory(prefix="forge-offline-") as tmp, ExitStack() as stack:
        home = Path(tmp)
        stack.enter_context(patch.object(gui, "DEFAULT_FORGE_HOME", home))
        stack.enter_context(patch.object(gui, "_desktop_config_path", return_value=home / "desktop.json"))
        stack.enter_context(patch.object(secret_store, "SECRETS_FILE", home / "secrets.json"))
        stack.enter_context(patch.object(sub_agent, "config_path", return_value=home / "agent-cluster.json"))
        if os.environ.get("FORGE_NET_TESTS") != "1":
            fetch = plugin_market._http_get_json
            def offline_market_fetch(url, timeout=10.0):
                # Exercise the GUI's real auto-sync path, but keep its workers
                # offline. Direct transport unit tests still reach their mocked
                # urlopen, including the response-size boundary test.
                if threading.current_thread().name.startswith("Forge-"):
                    raise OSError("offline regression run")
                return fetch(url, timeout=timeout)
            stack.enter_context(patch.object(plugin_market, "_http_get_json", side_effect=offline_market_fetch))
        suite = unittest.defaultTestLoader.loadTestsFromNames(names)
        for name in names:
            if "." in name:
                continue  # unittest already loaded the explicitly selected test.
            module = importlib.import_module(name)
            for fn_name, fn in inspect.getmembers(module, inspect.isfunction):
                if fn.__module__ == name and (fn_name.startswith("test_") or
                    (name == "test_config_model" and fn_name in {"run_pass", "run_fail", "run_merge", "run_sniff"})):
                    suite.addTest(unittest.FunctionTestCase(fn, description=f"{name}.{fn_name}"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
