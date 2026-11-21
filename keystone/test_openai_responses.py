"""Offline checks for the GPT function round-trip and terminal stream handling."""
import io
import json
import unittest

from . import openai_responses as native
from .model import HttpTransport, Provider

MODEL = "gpt-6.1-sol"
TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
CALL = {"type": "function_call", "status": "completed", "call_id": "call_fixture",
        "name": "lookup", "arguments": '{  "key": "fixture" }'}
REASONING = {"type": "reasoning", "id": "rs_fixture", "summary": [], "encrypted_content": "opaque"}


def response(status="completed"):
    return {"id": "resp_fixture", "model": MODEL, "status": status,
            "output": [REASONING, CALL], "usage": {"input_tokens": 2050, "output_tokens": 12,
                "input_tokens_details": {"cached_tokens": 1800, "cache_write_tokens": 200}}}


class ResponsesTests(unittest.TestCase):
    def test_direct_transport_uses_responses_and_preserves_replay(self):
        received = []

        def opener(request, **options):
            received.append((request.full_url, json.loads(request.data)))
            return io.BytesIO(json.dumps(response()).encode())

        provider = Provider("fixture", "https://api.openai.com/v1", default_model=MODEL)
        transport = HttpTransport(response_opener=opener)
        _, usage, extra = transport.complete(provider, MODEL, [{"role": "user", "content": "look up"}],
                                              tools=TOOLS, max_tokens=128, reasoning_effort="low")
        self.assertEqual(received[0][0], "https://api.openai.com/v1/responses")
        first = received[0][1]
        self.assertEqual(first["max_output_tokens"], 128)
        self.assertFalse(first["tools"][0]["strict"])
        self.assertEqual(first["reasoning"], {"effort": "low"})
        self.assertFalse(first["store"])
        self.assertEqual((usage.cached_tokens, usage.cache_write_tokens), (1800, 200))
        call = extra["tool_calls"][0]
        self.assertEqual(call["id"], CALL["call_id"])
        transport.complete(provider, MODEL, [extra["assistant_message"],
            {"role": "tool", "tool_call_id": call["id"], "content": "fixture"}], tools=TOOLS)
        replay = received[1][1]["input"]
        self.assertEqual(replay[0], REASONING)
        self.assertEqual(replay[1]["arguments"], CALL["arguments"])
        self.assertEqual(replay[2], {"type": "function_call_output", "call_id": "call_fixture",
                                    "output": "fixture"})
        alternate = native.to_request(
            {"model": "gpt-6.1-terra", "messages": [extra["assistant_message"]]}, "other")
        self.assertEqual([i["type"] for i in alternate["input"]], ["function_call"])

    def test_gpt6_sol_reasoning_tools_use_native_endpoint_without_gpt61_cache_extensions(self):
        from .cache_affinity import apply_openai_breakpoint
        model = 'gpt-6-sol'
        payload = {'model': model, 'messages': [{'role': 'user', 'content': 'calculate'}],
                   'reasoning_effort': 'medium'}
        provider = Provider('official', 'https://api.openai.com/v1', default_model=model)
        tools = {**payload, 'tools': TOOLS}
        self.assertTrue(native.uses_responses(tools, provider))
        self.assertEqual(native.to_request(tools, 'scope')['reasoning'], {'effort': 'medium'})
        self.assertTrue(native.uses_responses({**payload, 'messages': [{'role': 'tool'}]}, provider))
        self.assertFalse(native.uses_responses(payload, provider))
        self.assertFalse(native.uses_responses(tools, Provider('other', 'https://example.com/v1')))
        self.assertIsNone(apply_openai_breakpoint(tools, provider, model))

    def test_stream_has_no_native_calls_until_completed(self):
        pending = {"type": "response.function_call_arguments.delta", "delta": '{"unfinished"'}
        def frame(event):
            return ("data: " + json.dumps(event) + "\n\n").encode()
        with self.assertRaisesRegex(ValueError, "before completion"):
            list(native.stream_chat(io.BytesIO(frame(pending)), MODEL, "scope"))
        for status, expected in (("completed", True), ("incomplete", False)):
            terminal = frame({"type": "response." + status, "response": response(status)})
            chunks = list(native.stream_chat(io.BytesIO(frame(pending) + terminal), MODEL, "scope"))
            self.assertEqual(any(b'"tool_calls"' in chunk for chunk in chunks), expected)
            self.assertTrue(any(b'"cached_tokens": 1800' in chunk for chunk in chunks))

    def test_text_and_reseller_routes_keep_chat_protocol(self):
        provider = Provider("official", "https://api.openai.com/v1", default_model=MODEL)
        reseller = Provider("reseller", "https://proxy.invalid/v1", default_model=MODEL)
        payload = {"model": MODEL, "messages": [{"role": "user", "content": "text"}]}
        self.assertFalse(native.uses_responses(payload, provider))
        self.assertFalse(native.uses_responses({**payload, "tools": TOOLS}, reseller))
