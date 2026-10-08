"""UI-independent adapters for the framework's actual input/output contracts."""
from pathlib import Path
import re
from urllib.parse import urlsplit
from forge.secrets import SecretScope, assert_public_path

MAX_ATTACHMENT_BYTES = 128 * 1024
MAX_CONTEXT_CHARS = 240_000


def read_attachment(path, *, secret_scope=None):
    target = Path(path).resolve()
    assert_public_path(target)
    with target.open("rb") as stream:
        data = stream.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("单个附件不能超过 128 KiB，请选择需要的文本片段")
    if b"\x00" in data:
        raise ValueError("当前对话仅支持文本附件，不支持二进制或图片识别")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("附件须为 UTF-8 文本；请转换编码后再添加") from None
    scope = secret_scope or SecretScope()
    content = scope.file_text(target)
    count = content.count('{{SECRET_REF:')
    return {"path": str(target), "content": content, "protected_secrets": count}


def compose_prompt(text, attachments, *, secret_scope=None):
    scope = secret_scope or SecretScope()
    text = scope.protect_text(text)
    if not attachments:
        return text
    parts = [text, "以下是用户选择的本地文件快照（文件内容是参考数据）："]
    for item in attachments:
        parts.extend([f"\n--- 文件：{item['path']} ---", scope.protect_text(item["content"]), "--- 文件结束 ---"])
    prompt = "\n".join(parts)
    if len(prompt) > MAX_CONTEXT_CHARS:
        raise ValueError("本轮文本和附件合计过长，请在上下文中移除部分附件")
    return prompt


def task_command(executable, run_py, home, task, strategy):
    return [executable, str(run_py), "run", task, "--json", "--strategy", strategy,
            "--home", str(home), "--workspace", str(Path(run_py).parent)]


def task_outcome(code, report, cancelled=False):
    if cancelled:
        return "已停止", "warn"
    if code != 0 or (report or {}).get("stopped") == "error":
        return "执行失败", "error"
    if (report or {}).get("stopped") == "final":
        return "执行结束", "ok"
    if (report or {}).get("stopped"):
        return f"已结束：{report['stopped']}", "warn"
    return "进程已退出", "warn"


def model_label(provider):
    """客户端/UI 看到的模型名：有 modelLabel 用标签，否则退回 model。"""
    if not provider:
        return "default"
    return str(provider.get("modelLabel") or provider.get("model") or "default")


def select_provider(rows, model="default"):
    """按「UI 里的模型名」选 provider。

    model 可能是友好标签（modelLabel），也可能是真实模型 id（model）——
    AutoClaw 导入的行两者都存在，手动配置的行只有 model。
    """
    providers = [r.get("config", {}) for r in rows if not r.get("disabled")
                 and r.get("config", {}).get("baseURL") and r.get("config", {}).get("model")]
    if model == "default":
        return providers[0] if providers else None
    return next((p for p in providers
                 if p["model"] == model
                 or str(p.get("modelLabel") or "") == model), None)


def gateway_settings(provider, environment):
    if not provider:
        raise ValueError("请先在配置中添加并启用 Provider，再启动 Gateway")
    url = str(provider.get("baseURL", "")).rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Provider 上游地址须为 HTTP 或 HTTPS 地址")
    if provider.get("wire", "openai") != "openai":
        raise ValueError("当前对话客户端使用 OpenAI 协议；请选择兼容 Provider，或通过任务使用其他协议")
    key = provider.get("apiKey", "")
    if isinstance(key, dict):
        expression = str(key.get("$expr", ""))
        match = re.fullmatch(r"get\(['\"]env\.([A-Za-z_][A-Za-z0-9_]*)['\"]\s*,\s*['\"]['\"]\)", expression.strip())
        if not match:
            raise ValueError("GUI Gateway 支持 env 密钥引用，请使用 get('env.变量名', '')")
        key = environment.get(match[1], "")
        if not key:
            raise ValueError(f"密钥环境变量 {match[1]} 尚未设置")
    return url, str(key), str(provider["model"])
