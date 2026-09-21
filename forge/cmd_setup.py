"""
pledge-evolving 首次运行向导 — 交互式配置 API 密钥
用法：python run.py setup
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

HOME = Path.home() / ".forge"
USER_LAYER = HOME / "forge.patch.json"

# 可配置的 provider 列表
PROVIDERS = [
    {
        "id": "deepseek",
        "name": "DeepSeek（推荐，便宜好用）",
        "env_key": "FORGE_DEEPSEEK_KEY",
        "placeholder": "sk-...",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "wire": "openai",
    },
    {
        "id": "openai",
        "name": "OpenAI（GPT 系列）",
        "env_key": "FORGE_OPENAI_KEY",
        "placeholder": "sk-...",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "wire": "openai",
    },
    {
        "id": "anthropic",
        "name": "Anthropic（Claude 系列）",
        "env_key": "FORGE_ANTHROPIC_KEY",
        "placeholder": "sk-ant-...",
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-4-20250514",
        "wire": "anthropic",
    },
    {
        "id": "ollama",
        "name": "Ollama（本地推理，无需密钥）",
        "env_key": "FORGE_OLLAMA_KEY",
        "placeholder": "本地无需密钥（直接回车）",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen3:8b",
        "wire": "openai",
        "service": "ollama",
    },
    {
        "id": "llamacpp",
        "name": "llama.cpp server（本地推理，无需密钥）",
        "env_key": "FORGE_LLAMACPP_KEY",
        "placeholder": "本地无需密钥（直接回车）",
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "",     # 用户填（llama-server 的模型名较随意）
        "wire": "openai",
        "service": "llamacpp",
    },
    {
        "id": "mnn",
        "name": "MNN（本地推理，无需密钥）",
        "env_key": "FORGE_MNN_KEY",
        "placeholder": "本地无需密钥（直接回车）",
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "",     # 用户填
        "wire": "openai",
        "service": "mnn",
    },
    {
        "id": "custom",
        "name": "自定义（OpenAI 兼容接口）",
        "env_key": "FORGE_CUSTOM_KEY",
        "placeholder": "你的 API Key",
        "base_url": "",  # 用户填
        "model": "",     # 用户填
        "wire": "openai",
    },
]


def _clear():
    os.system("cls" if os.name == "nt" else "clear")


def _banner():
    print("""
╔══════════════════════════════════════════════════╗
║       pledge-evolving · 首次运行向导            ║
║                                                  ║
║  只需一步：配置你的 AI 模型 API 密钥            ║
║  配置完成后，直接输入任务即可开始使用           ║
╚══════════════════════════════════════════════════╝
""")


def _choose_provider() -> dict:
    print("选择你的 AI 模型提供商：\n")
    for i, p in enumerate(PROVIDERS, 1):
        print(f"  [{i}] {p['name']}")
    print()

    while True:
        choice = input("输入编号（1-4）> ").strip()
        if choice in ("1", "2", "3", "4"):
            return PROVIDERS[int(choice) - 1]
        print(f"无效输入，请输入 1-{len(PROVIDERS)}")


def _get_api_key(provider: dict) -> str:
    env_val = os.environ.get(provider["env_key"], "")
    if env_val:
        masked = env_val[:6] + "..." + env_val[-4:] if len(env_val) > 10 else "***"
        print(f"\n检测到环境变量 {provider['env_key']} = {masked}")
        use_env = input("使用这个密钥？(Y/n) > ").strip().lower()
        if use_env != "n":
            return env_val

    # 本地引擎（ollama / llamacpp / mnn）在环回地址上跑，不存在密钥。
    # 允许留空，而不是逼用户编一个假 key 塞进配置。
    if provider.get("service"):
        print(f"\n{provider['name']} 是本地推理引擎，默认无需密钥（直接回车跳过）：")
        return getpass.getpass(f"  {provider['env_key']}（可留空）> ").strip()

    print(f"\n请输入你的 {provider['name']} API 密钥：")
    print(f"  （将保存到 ~/.forge/forge.patch.json，不会上传）")
    key = getpass.getpass(f"  {provider['env_key']} > ").strip()
    return key


def _get_custom_config() -> tuple[str, str]:
    print("\n自定义接口需要额外配置：")
    base_url = input("  API 地址（如 http://localhost:8787/v1）> ").strip()
    model = input("  模型名称（如 deepseek-flash）> ").strip()
    return base_url, model


def _build_patch(provider: dict, api_key: str, custom_url: str = "", custom_model: str = "") -> list[dict]:
    """构建 forge.patch.json 的内容。"""
    patch = []

    # Provider 行
    provider_row = {
        "id": "medium",
        "name": "provider:medium",
        "config": {
            "wire": provider.get("wire", "openai"),
            "baseURL": custom_url or provider["base_url"],
            "apiKey": api_key,
            "model": custom_model or provider["model"],
            "smallModel": custom_model or provider["model"],
        },
    }
    # 本地推理引擎（ollama / llamacpp / mnn）带上 service，`forge run` 时才会
    # 启用本地专属路径（OpenAI 兼容端点 + 沉思预算），云端 provider 不带该键。
    if provider.get("service"):
        provider_row["config"]["service"] = provider["service"]
    patch.append(provider_row)

    # Model 行 — 把选中的 provider 设为默认。
    #
    # 注意两点：
    # 1. apply_patch 是整行替换（DSH 语义），这里没写的键会被丢掉，所以
    #    整行显式列出，而不是只写要改的部分。
    # 2. tiers 只绑到用户真正配置的那个档（medium）。早期版本还塞了一个
    #    ["lite", ...]——但 lite 的 provider 行并没有被覆盖，仍指向 base.json
    #    里的环回占位网关，于是 --strategy economy 会打到不存在的
    #    127.0.0.1:8810。未配置的档位不写，而不是写一个假的。
    model_name = custom_model or provider["model"]
    model_row = {
        "id": "model",
        "name": "model:router",
        "config": {
            "primary": ["medium", model_name],
            "fallback": [],
            "moa": False,
            "moaModels": [],
            "routing": {
                "strategy": "balanced",
                "tiers": [["medium", model_name]],
                "small": ["medium", model_name],
                "notes": (
                    "forge setup 生成的单档配置：只绑定 medium。"
                    "如需 economy/premium，请手动补 lite/premium 的 provider 行，"
                    "并把它们加进 tiers。"
                ),
            },
            "notes": (
                f"由 forge setup 写入：provider medium → {model_name}。"
                "整行替换语义意味着这里列出的就是最终值。"
            ),
        },
    }
    patch.append(model_row)

    return patch


def _save(patch: list[dict]):
    HOME.mkdir(parents=True, exist_ok=True)

    # 如果已有用户层，合并（新配置覆盖旧的同 id 行）
    existing = []
    if USER_LAYER.is_file():
        try:
            existing = json.loads(USER_LAYER.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []

    # 合并：按 id 去重，新的在前
    seen = {r["id"] for r in patch}
    merged = patch + [r for r in existing if r.get("id") not in seen]

    USER_LAYER.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n✅ 配置已保存到 {USER_LAYER}")


def _verify():
    """快速验证配置是否生效。"""
    print("\n验证配置...")
    try:
        from .config import load_config
        from .model import ModelRouter

        # 找到 bundles 目录
        pkg_bundles = Path(__file__).resolve().parent.parent / "bundles"
        repo_bundles = Path(__file__).resolve().parent.parent.parent / "bundles"
        bundle_dir = pkg_bundles if pkg_bundles.is_dir() else repo_bundles

        cfg = load_config(HOME, bundles=sorted(bundle_dir.glob("*.json")))
        router = ModelRouter.from_config(cfg)

        providers = list(router.providers.keys())
        if providers:
            print(f"✅ 发现 {len(providers)} 个 provider: {', '.join(providers)}")
            print("✅ 配置验证通过！")
        else:
            print("⚠️  未检测到有效的 provider，请检查 API 密钥是否正确")
    except Exception as e:
        print(f"⚠️  验证出错（不影响使用）: {e}")


def _show_next_steps():
    print(f"""
╔══════════════════════════════════════════════════╗
║                 🎉 配置完成！                   ║
╠══════════════════════════════════════════════════╣
║                                                  ║
║  现在你可以：                                    ║
║                                                  ║
║  💬 直接聊天：                                   ║
║     python run.py run "帮我写一首诗"             ║
║                                                  ║
║  🖥️  打开图形界面：                              ║
║     python forge_gui.py                          ║
║                                                  ║
║  🔧 检查配置：                                   ║
║     python run.py doctor                         ║
║                                                  ║
║  🧪 运行自测：                                   ║
║     python run.py selftest                       ║
║                                                  ║
║  ⚙️  修改配置：编辑 ~/.forge/forge.patch.json    ║
║                                                  ║
╚══════════════════════════════════════════════════╝
""")


def cmd_setup(args) -> int:
    _clear()
    _banner()

    # 检查是否已有配置
    if USER_LAYER.is_file():
        try:
            existing = json.loads(USER_LAYER.read_text(encoding="utf-8"))
            provider_rows = [r for r in existing if r.get("id", "").startswith("provider")]
            if provider_rows:
                print(f"检测到已有配置（{len(provider_rows)} 个 provider）")
                redo = input("重新配置？(y/N) > ").strip().lower()
                if redo != "y":
                    print("保持现有配置，直接使用。")
                    _verify()
                    return 0
        except (json.JSONDecodeError, OSError):
            pass

    # 选择 provider
    provider = _choose_provider()

    # 获取密钥
    api_key = _get_api_key(provider)
    # 本地引擎允许空密钥；云端仍要求密钥
    if not api_key and not provider.get("service"):
        print("❌ 未输入密钥，配置取消。")
        return 1

    # 自定义接口额外配置
    custom_url, custom_model = "", ""
    if provider["id"] == "custom":
        custom_url, custom_model = _get_custom_config()
    elif not provider.get("model"):
        # 本地引擎的模型名没有固定约定（llama-server / MNN 的已加载模型各异），
        # 空模型名会让 model 行的 primary 无法解析，所以这里必须问。
        print("\n本地引擎需要指定模型名：")
        custom_model = input("  模型名称（如 qwen3-8b / llama-3.1-8b）> ").strip()
        if not custom_model:
            print("❌ 未输入模型名，配置取消。")
            return 1

    # 构建并保存
    patch = _build_patch(provider, api_key, custom_url, custom_model)
    _save(patch)
    _verify()
    _show_next_steps()

    return 0
