"""Provider import must retain the backend's declared adaptation settings."""
import json
import unittest
import io
from unittest.mock import patch

from config_model import normalize


class ProviderAdaptationImportTests(unittest.TestCase):
    def test_reasoning_and_usage_survive_streaming_and_tool_replay(self):
        from forge_client import ChatMessage, ForgeGatewayClient
        events = [
            {"choices": [{"delta": {"reasoning_content": "reason "}, "finish_reason": None}]},
            {"choices": [{"delta": {"reasoning_content": "data", "content": "ok"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
        ]
        wire = b"".join(("data: " + json.dumps(e) + "\n\n").encode() for e in events) + b"data: [DONE]\n\n"
        with patch("forge_client.open_response", return_value=io.BytesIO(wire)):
            result = ForgeGatewayClient("http://127.0.0.1:1").stream_chat([ChatMessage("user", "hi")])
        self.assertEqual(result.reasoning_content, "reason data")
        self.assertEqual(result.usage["prompt_tokens"], 10)
        message = ChatMessage("assistant", result.text, reasoning_content=result.reasoning_content)
        self.assertEqual(message.to_dict()["reasoning_content"], "reason data")

    def test_flat_provider_keeps_adaptation_fields_inside_config(self):
        fields = {"vendor": "anthropic", "cacheControl": "off", "expectedCalls": 1,
                  "callGapSeconds": 0, "adapt": False, "service": "", "rpm": 10,
                  "temperature": .5, "headers": {"x-test": "value"}, "models": ["actual-model"],
                  "thinkBudget": 1024, "flex": False, "defer": False, "maxDeferSeconds": 60}
        result = normalize(json.dumps({"id": "custom", "wire": "openai",
            "baseURL": "https://example.invalid/v1", "model": "actual-model", **fields}), add_model=False)
        config = result.rows[0]["config"]
        for key, value in fields.items():
            self.assertEqual(config.get(key), value, key)

    def test_flat_model_row_keeps_routing_strategy_and_tiers(self):
        routing = {"strategy": "premium", "tiers": [["medium", "draft-model"]],
                   "premium": [["review", "review-model"]], "small": ["lite", "small-model"]}
        result = normalize(json.dumps([{"id": "model", "name": "model:router",
            "primary": ["medium", "draft-model"], "routing": routing}]), add_model=False)
        self.assertEqual(result.rows[0]["config"]["routing"], routing)


if __name__ == "__main__":
    unittest.main()
