"""Every entry point resolves LoopLimits the same way (revon.loop_limits)."""
from pathlib import Path
import tempfile
import unittest

from .assembly import build_agent
from .config import USER_LAYER_NAME, Config
from .loop_limits import BOUNDS, NO_STEP_LIMIT, is_unlimited, resolve_loop_limits, resolve_max_steps
from .test_project_rules import Router

MIMO = [{"id": "p", "name": "provider:reseller", "config": {
            "baseURL": "https://gateway.example/v1", "model": "deployment-blue",
            "modelKinds": {"deployment-blue": "minimax.m3"}}},
        {"id": "model", "name": "model", "config": {"primary": ["p", "deployment-blue"]}}]


def config(*layers):
    cfg = Config()
    cfg.apply_patch([{"id": "loop", "name": "loop:core", "config": {"maxSteps": 12, "maxDepth": 2,
                                                                    "spawnBudget": 8}}], label="base.json")
    for rows, label in layers:
        cfg.apply_patch(rows, label=label)
    return cfg


class LoopLimitsTests(unittest.TestCase):
    def test_vendor_hint_raises_bundle_default_everywhere(self):
        cfg = config((MIMO, "bundle"))
        self.assertEqual(resolve_max_steps(cfg, None), (12, 'config'))
        limits = resolve_loop_limits(cfg)
        self.assertEqual((limits.max_steps, limits.max_depth, limits.spawn_budget), (24, 2, 8))
        with tempfile.TemporaryDirectory() as folder:
            agent = build_agent(home=Path(folder) / 'home', workspace=Path(folder), config=cfg,
                                router=Router([]), mount_contrib=False)
            self.addCleanup(agent.secret_scope.close)
            self.assertEqual(agent.limits.max_steps, 24)

    def test_user_layer_and_request_override_beat_the_hint(self):
        cfg = config((MIMO, "bundle"), ([{"id": "loop", "config": {"maxSteps": 15}}], USER_LAYER_NAME))
        self.assertEqual(resolve_loop_limits(cfg).max_steps, 15)
        self.assertEqual(resolve_loop_limits(cfg, overrides={'max_steps': 30, 'max_depth': None}).max_steps, 30)
        self.assertEqual(resolve_loop_limits(config((MIMO, "bundle")), use_hint=False).max_steps, 12)

    def test_config_values_are_clamped_to_shared_bounds(self):
        cfg = config(([{"id": "loop", "config": {"maxSteps": 5000, "maxDepth": -3, "spawnBudget": 99999}}],
                      USER_LAYER_NAME))
        limits = resolve_loop_limits(cfg)
        self.assertEqual((limits.max_steps, limits.max_depth, limits.spawn_budget),
                         (BOUNDS['max_steps'][1], 0, BOUNDS['spawn_budget'][1]))
        self.assertEqual(resolve_loop_limits(cfg, context_chars=777).context_chars, 777)

    def test_zero_or_missing_max_steps_means_no_limit(self):
        self.assertEqual(resolve_max_steps(Config(), None), (NO_STEP_LIMIT, 'config'))
        unlimited = config(([{"id": "loop", "config": {"maxSteps": 0}}], USER_LAYER_NAME))
        self.assertEqual(resolve_max_steps(unlimited, None), (NO_STEP_LIMIT, 'user'))
        self.assertTrue(is_unlimited(resolve_loop_limits(unlimited).max_steps))
        # A vendor hint only raises a numeric default; it never shrinks "no limit".
        self.assertEqual(resolve_loop_limits(config((MIMO, "bundle"),
                         ([{"id": "loop", "config": {"maxSteps": 0}}], "base.json"))).max_steps, NO_STEP_LIMIT)
        self.assertEqual(resolve_loop_limits(config(), overrides={'max_steps': 0}).max_steps, NO_STEP_LIMIT)
        self.assertEqual(resolve_loop_limits(config(), overrides={'max_steps': 7}).max_steps, 7)

    def test_shipped_bundles_have_no_step_limit(self):
        from .config import load_config
        with tempfile.TemporaryDirectory() as home:
            limits = resolve_loop_limits(load_config(Path(home)), use_hint=False)
        self.assertTrue(is_unlimited(limits.max_steps))

    def test_wrap_up_flag_and_context_chars_come_from_config(self):
        cfg = config(([{"id": "loop", "config": {"wrapUpOnLimit": False, "contextChars": 50000}}], "bundle"))
        limits = resolve_loop_limits(cfg, use_hint=False)
        self.assertFalse(limits.wrap_up_on_limit)
        self.assertEqual(limits.context_chars, 50000)


if __name__ == '__main__':
    unittest.main()
