"""End-to-end mode verification through REAL gateway processes.

Not part of the default regression: each test spawns actual `forge gateway
--tools` subprocesses (the same entry the GUI uses) and talks to them over
HTTP, so the whole chain is exercised — CLI args → config composition →
Policy → tool bridge → registry → handler → filesystem. A wiring mistake that
unit tests cannot see (e.g. "the config says sandbox but the gateway never
reads it") fails here.

Run explicitly::

    python -m unittest forge.test_e2e_modes            # ~25 s
    python run.py selftest --include e2e                # if wired

What "the two modes are done" means, and what this pins:

  A) sandbox (balanced):      outside read refused, outside write refused
                              (verified on disk, not just the tool return),
                              inside read works — confinement, not a blanket ban.
  B) full access (aggressive + --i-know): outside read AND write really work
                              (verified on disk).
  C) full access without the acknowledgement flag refuses to start at all.

Upstreams point at 127.0.0.1:9 (never accepts); no model is called, no
traffic leaves the machine. All probe files live in a TemporaryDirectory.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUN = REPO / "run.py"
PY = sys.executable
WAIT_SECONDS = 15.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_gateway(port: int, workspace: Path, profile_args: list[str]) -> subprocess.Popen:
    cmd = [PY, str(RUN), "gateway",
           "--upstream", "http://127.0.0.1:9/v1",   # never called; tools only
           "--upstream-wire", "openai", "--client-wire", "openai",
           "--models", "stub",
           "--port", str(port),
           "--tools", "--workspace", str(workspace),
           ] + profile_args
    return subprocess.Popen(cmd, cwd=str(REPO), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")


def _wait_ready(port: int, proc: subprocess.Popen) -> None:
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"gateway died at startup:\n{out[-2000:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("gateway did not become ready in time")


def _tool_call(port: int, name: str, arguments: dict) -> dict:
    body = json.dumps({"name": name, "arguments": arguments}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/tools/call", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}: {exc.read().decode()[:200]}"}


class SandboxModeTests(unittest.TestCase):
    """--profile balanced: the default mode the GUI ships."""

    def test_outside_access_refused_inside_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            outside = root / "outside"
            ws.mkdir()
            outside.mkdir()
            (ws / "inside.txt").write_text("inside-content", encoding="utf-8")
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("SENTINEL-MARKER-773", encoding="utf-8")
            target = outside / "probe_out.txt"

            port = _free_port()
            proc = _start_gateway(port, ws, ["--profile", "balanced"])
            self.addCleanup(self._stop, proc)
            _wait_ready(port, proc)

            # outside read refused, and the refusal must not echo the content
            r = _tool_call(port, "read_file", {"path": str(sentinel)})
            detail = str(r.get("error") or r.get("content", ""))
            self.assertFalse(r.get("ok"), f"outside read was allowed: {detail}")
            self.assertNotIn("SENTINEL-MARKER-773", detail)

            # outside write refused — on disk, not just in the return value
            r = _tool_call(port, "write_file", {"path": str(target), "content": "probe"})
            self.assertFalse(r.get("ok"))
            self.assertFalse(target.exists(), "file appeared outside the workspace")

            # inside still works: confinement, not a blanket ban
            r = _tool_call(port, "read_file", {"path": str(ws / "inside.txt")})
            self.assertTrue(r.get("ok"), str(r.get("error")))
            self.assertIn("inside-content", str(r.get("content", "")))

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()


class FullAccessModeTests(unittest.TestCase):
    """--profile aggressive --i-know: the acknowledged full-access mode."""

    def test_outside_read_write_really_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            outside = root / "outside"
            ws.mkdir()
            outside.mkdir()
            (ws / "inside.txt").write_text("inside-content", encoding="utf-8")
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("SENTINEL-MARKER-773", encoding="utf-8")
            target = outside / "probe_out.txt"

            port = _free_port()
            proc = _start_gateway(port, ws, ["--profile", "aggressive", "--i-know"])
            self.addCleanup(SandboxModeTests._stop, proc)
            _wait_ready(port, proc)

            r = _tool_call(port, "read_file", {"path": str(sentinel)})
            self.assertTrue(r.get("ok"), str(r.get("error")))
            self.assertIn("SENTINEL-MARKER-773", str(r.get("content", "")))

            r = _tool_call(port, "write_file", {"path": str(target), "content": "probe"})
            self.assertTrue(r.get("ok"), str(r.get("error")))
            self.assertTrue(target.exists(), "full access failed to write outside")

            r = _tool_call(port, "read_file", {"path": str(ws / "inside.txt")})
            self.assertTrue(r.get("ok"))


class AcknowledgementGateTests(unittest.TestCase):
    """aggressive without --i-know must refuse to start."""

    def test_refuses_without_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            port = _free_port()
            proc = _start_gateway(port, ws, ["--profile", "aggressive"])
            self.addCleanup(SandboxModeTests._stop, proc)
            time.sleep(4)
            alive = proc.poll() is None
            listening = False
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                    listening = True
            except OSError:
                listening = False
            self.assertFalse(alive, "aggressive gateway survived without --i-know")
            self.assertFalse(listening, "aggressive gateway is listening without --i-know")


if __name__ == "__main__":
    unittest.main()
