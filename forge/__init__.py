"""forge — a unified agent framework.

Integrated from the designs of Codex, Hermes Agent, DeepSeek Harness,
Claude Code, WorkBuddy/CodeBuddy, OpenClaw and OpenCode.

Module map
----------
config      空根 + 有序补丁层配置合成（DeepSeek Harness）
policy      二维权限模型 + 三档沙箱 + 命令黑名单（CodeBuddy / Codex）
tools       工具注册 + 延迟加载 / ToolSearch（CodeBuddy）
capability  skills / plugins / connectors 统一为目录即能力（Hermes / Codex / OpenClaw）
memory      双写 Markdown + 类型化记忆 + 上下文预算（CodeBuddy / OpenClaw）
compaction  上下文压缩策略（从 memory 拆分，独立可测试）
subagent    子代理编排：派生/预算/输出净化（从 loop 拆分）
thinking    沉思引擎：预算控制/收敛判定/三档模式（从 loop 拆分）
session     追加式事件日志 + 派生索引 + resume/fork（Codex rollout）
model       provider 抽象 + 分级供给（fallback 链 / MoA）（OpenCode / Hermes）
routing     三档智能路由：economy 最低成本 / balanced 中端 / premium 高端搭配（0.7.0 新增）
local_service  本地推理引擎门控：ollama / llamacpp / mnn 专属适配，云端不变
checkpoint  影子 git 快照与回滚（Hermes）
loop        agent 主循环 + 子代理编排与权限天花板（OpenClaw / CodeBuddy）
gateway     环回协议网关（本次集成实测产物）
wire        Anthropic ↔ OpenAI 协议翻译，含 SSE 流式
federation  异构 CLI 智能体联邦：描述符 / 派发 / 失败分类 / 输出归一
evolution   自我迭代进化：信号 → 候选 → 闸门 → 账本 → 回滚 → 代谢（Hermes）
registry    贡献模块注册表：静态禁令 + 动态加载 + 一致性闸门（第三方 agent 写的代码靠它合入）
toolwire    原生工具调用：OpenAI / Anthropic 两种协议的声明、调用解析与结果回流
pricing     成本核算：有效单价 + 消费账本 + 同工作量的比价
guard       共享安全禁令表（AST 表达式白名单 + 贡献源码静态扫描）
cli         run / dump-config / doctor / evolution / federation / modules / cost / selftest
"""

__version__ = "0.8.0"  # 0.8.0: module extraction + security hardening

from .config import Config, Row, load_config  # noqa: F401
from .policy import Policy, Decision, Mode, Sandbox  # noqa: F401

__all__ = ["Config", "Row", "load_config", "Policy", "Decision", "Mode", "Sandbox", "__version__"]
