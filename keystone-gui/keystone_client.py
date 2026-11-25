"""The former desktop gateway client import used by local integrations."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "revon-gui"))
from revon_client import (ChatMessage, CompletionResult, GatewayError, GenerationCancelled,
                          ModelStreamInterrupted, RevonGatewayClient, http_error_detail)

KeystoneGatewayClient = RevonGatewayClient
