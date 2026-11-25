"""Offline regressions for the desktop's recurring GPU process probe."""
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import sysmon


class GpuProbeTests(unittest.TestCase):
    def setUp(self):
        self.saved_state = dict(sysmon._GPU_STATE)
        sysmon._GPU_STATE.update(disabled=False, available=False, name=None, sampler_calls=0)

    def tearDown(self):
        sysmon._GPU_STATE.update(self.saved_state)

    def test_every_recurring_probe_is_windowless_and_keeps_metrics(self):
        result = Mock(returncode=0, stdout="28, Test GPU\n")
        with patch.object(sysmon.subprocess, "run", return_value=result) as run:
            samples = [sysmon._query_gpu_windows() for _ in range(10)]
        self.assertEqual(run.call_count, 3)  # first, fifth, tenth samples
        self.assertEqual(samples[-1], {"gpu": 28.0, "gpu_name": "Test GPU"})
        for call in run.call_args_list:
            options = call.kwargs
            self.assertEqual(options["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertFalse(options["shell"])
            self.assertTrue(options["capture_output"])
            self.assertEqual(options["timeout"], 2.0)

    def test_failed_probe_stops_retrying(self):
        with patch.object(sysmon.subprocess, "run", side_effect=subprocess.TimeoutExpired("nvidia-smi", 2)) as run:
            self.assertIsNone(sysmon._query_gpu_windows())
            self.assertIsNone(sysmon._query_gpu_windows())
        self.assertEqual(run.call_count, 1)

    @unittest.skipUnless(os.name == "nt", "Windows console regression")
    def test_actual_console_child_has_no_console_window(self):
        original_run = subprocess.run
        script = "import ctypes; print('28, console=' + str(ctypes.windll.kernel32.GetConsoleWindow()))"

        def launch_probe(_command, **options):
            # Exercise the real Windows process flags without requiring a GPU.
            return original_run([sys.executable, "-c", script], **options)

        with patch.object(sysmon.subprocess, "run", side_effect=launch_probe):
            result = sysmon._query_gpu_windows()
        self.assertEqual(result, {"gpu": 28.0, "gpu_name": "console=0"})


if __name__ == "__main__":
    unittest.main()
