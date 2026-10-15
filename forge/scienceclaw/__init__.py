"""ScienceClaw capability port (from AgentTeam-TaichuAI/ScienceClaw@master).

Three capabilities ported with forge's discipline (stdlib-only, offline
testable, no langchain dependency):

* offload    – Cursor-style tool-result offloading: results past a
               threshold are written to workspace files; the agent sees
               a summary + path reference instead of a truncated blob
* plan       – PlanStep model + normalisation (stable frontend shape)
* thinking   – reasoning-content extraction across the three vendor
               formats (DeepSeek reasoning_content / Claude content
               blocks / DeepSeek-Qwen <think> tags)

Upstream sources (ScienceClaw-master/backend/deepagent/):
    offload_middleware.py, plan_types.py, runner.py (_extract_thinking)
"""
from .offload import (
    DEFAULT_THRESHOLD,
    OffloadPolicy,
    ToolResultOffloader,
    extract_text,
    make_summary,
)
from .plan import PlanStep, normalize_plan_steps
from .thinking import extract_thinking, strip_think_tags

__all__ = [
    "DEFAULT_THRESHOLD", "OffloadPolicy", "ToolResultOffloader",
    "extract_text", "make_summary",
    "PlanStep", "normalize_plan_steps",
    "extract_thinking", "strip_think_tags",
]

