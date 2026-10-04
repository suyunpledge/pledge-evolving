"""Session durability, isolated configuration and malformed local HTTP requests."""
from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from forge import config
from forge.gateway import build_handler, GatewayConfig
from forge.session import Event, Session, SessionIndex


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "actual.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_interrupted_tail_preserved_and_resume_keeps_only_actual_messages(self):
        with Session(self.path) as session:
            session.append("user_message", content="actual message")
            started = session.meta["started_at"]
        with self.path.open("ab") as stream:
            stream.write(b'{"ordinal":2,"type":"assistant_message","content":"partial')
        with Session(self.path) as resumed:
            self.assertEqual(resumed.messages(), [{"role": "user", "content": "actual message"}])
            self.assertEqual(resumed.meta["started_at"], started)
            resumed.append("assistant_message", content="actual result")
        self.assertEqual(len(list(self.root.glob("*.incomplete-*"))), 1)
        self.assertEqual(len(list(Session(self.path).replay())), 3)

    def test_valid_last_record_without_newline_is_not_lost(self):
        with Session(self.path) as session:
            session.append("user_message", content="complete")
        self.path.write_bytes(self.path.read_bytes().rstrip(b"\n"))
        with Session(self.path) as session:
            self.assertEqual(session.messages()[0]["content"], "complete")
            session.append("assistant_message", content="reply")
        self.assertEqual(len(list(Session(self.path).replay())), 3)
        self.assertEqual(list(self.root.glob("*.incomplete-*")), [])

    def test_corrupt_completed_record_does_not_silently_change_history(self):
        self.path.write_text('{bad}\n', encoding="utf-8")
        session = Session(self.path)
        with self.assertRaises(ValueError):
            session.open()
        self.assertIsNone(session._fh)
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{bad}\n')

    def test_failed_append_does_not_invent_memory_event(self):
        with Session(self.path) as session:
            real = session._fh
            session._fh = Mock()
            session._fh.write.side_effect = OSError("disk unavailable")
            try:
                with self.assertRaises(OSError):
                    session.append("user_message", content="unpersisted")
                self.assertEqual(session.messages(), [])
            finally:
                session._fh = real

    def test_zero_history_fork_and_reserved_event_fields(self):
        with Session(self.path, {"type": "fake", "ordinal": 99}) as session:
            session.append("user_message", content="actual")
            with session.fork(self.root / "child.jsonl", keep_last=0) as child:
                self.assertEqual(child.messages(), [])
            with self.assertRaises(ValueError):
                session.fork(self.root / "bad.jsonl", keep_last=-1)
        self.assertEqual(next(Session(self.path).replay()).type, "session_meta")
        self.assertEqual(json.loads(Event(3, "actual", data={"ordinal": 88, "type": "fake"}).to_line())["ordinal"], 3)

    def test_bad_index_and_bad_session_do_not_hide_other_sessions(self):
        with Session(self.path) as session:
            session.append("user_message", content="actual")
        (self.root / "broken.jsonl").write_text('{bad}\n', encoding="utf-8")
        index = SessionIndex(self.root)
        index.path.write_text('[]', encoding="utf-8")
        rows = index.load()["sessions"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(next(row for row in rows if row["session_id"] == "actual")["events"], 1)
        self.assertIn("error", next(row for row in rows if row["session_id"] == "broken"))


class ConfigConcurrencyTests(unittest.TestCase):
    def test_parallel_expression_contexts_do_not_cross(self):
        barrier = threading.Barrier(2)
        original = config._helper_get

        def synchronized_get(*args):
            barrier.wait(timeout=2)
            return original(*args)

        with patch.dict(config._ALLOWED_CALLS, {"get": synchronized_get}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(config._eval_expr, "get('provider')", {"provider": key}) for key in ("a", "b")]
                self.assertEqual([future.result() for future in futures], ["a", "b"])


class GatewayValidationTests(unittest.TestCase):
    def test_invalid_lengths_and_chunked_requests_return_400(self):
        handler = build_handler(GatewayConfig(upstream="http://example.invalid"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            for headers in ({"Content-Length": "-1"}, {"Content-Length": "not-a-number"}, {"Transfer-Encoding": "chunked"}):
                conn = http.client.HTTPConnection(*server.server_address, timeout=2)
                try:
                    conn.request("POST", "/v1/tools/call", headers=headers)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 400)
                    self.assertIn("error", json.loads(response.read()))
                finally:
                    conn.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(1)


if __name__ == "__main__":
    unittest.main()
