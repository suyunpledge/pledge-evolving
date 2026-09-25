"""config_model 单元测试——矫治器的真值表。

跑法：
    python test_config_model.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_model import (  # noqa: E402
    ConfigNormalizeError,
    normalize,
    merge_with_user_layer,
    _sniff_text,
)


CASES_PASS = [
    # 1. 标准 patch 数组（最常见）
    (
        '[{"id": "deepseek", "name": "provider:deepseek", "config": '
        '{"wire": "openai", "baseURL": "https://api.deepseek.com", '
        '"apiKey": "sk-test-1234567890", "model": "deepseek-flash"}}]',
        "std_array",
    ),
    # 2. JSON 行注释（forge 支持的形式）
    (
        '// 主力 provider\n'
        '{"id":"deepseek","config":{"wire":"openai",'
        '"baseURL":"https://api.deepseek.com","apiKey":"sk-1234567890ab",'
        '"model":"deepseek-flash"}}',
        "json_with_comments",
    ),
    # 3. 语义化分组对象（形态 C）
    (
        '{"providers": {"stepfun": {"wire":"openai", '
        '"baseURL":"https://api.stepfun.com/v1", '
        '"apiKey":"step-abcdef1234567890", '
        '"model":"step-3.5-flash"}}}',
        "providers_group",
    ),
    # 4. 平铺对象（形态 C 平铺）
    (
        '{"deepseek": {"baseURL":"https://api.deepseek.com", '
        '"apiKey":"sk-1234567890ab", "model":"deepseek-flash"}}',
        "flat_object",
    ),
    # 5. 单条平铺 row
    (
        '{"id":"stepfun","wire":"openai",'
        '"baseURL":"https://api.stepfun.com/v1",'
        '"apiKey":"step-abcdef1234567890",'
        '"model":"step-3.5-flash"}',
        "single_inline",
    ),
    # 6. baseURL 别名 baseUrl
    (
        '{"id":"x","baseUrl":"https://x.com/v1","apiKey":"abcdef1234567890",'
        '"model":"x-1"}',
        "baseUrl_alias",
    ),
    # 7. apiKey 别名 api_key
    (
        '{"id":"x","baseURL":"https://x.com/v1","api_key":"abcdef1234567890",'
        '"model":"x-1"}',
        "apikey_alias",
    ),
    # 8. 已经是合法 $expr 的 apiKey
    (
        '{"id":"x","baseURL":"https://x.com/v1",'
        '"apiKey":{"$expr":"get(\'env.FORGE_X\', \'\')"},'
        '"model":"x-1"}',
        "expr_passthrough",
    ),
    # 9. 没 model 字段 → 补 default + warning
    (
        '{"id":"x","baseURL":"https://x.com/v1",'
        '"apiKey":"abcdef1234567890"}',
        "missing_model",
    ),
    # 10. 缺 baseURL → disabled
    (
        '{"id":"x","apiKey":"abcdef1234567890","model":"x-1"}',
        "missing_url",
    ),
    # 11. 双 provider 数组
    (
        '[{"id":"a","baseURL":"https://a.com/v1","apiKey":"AAAA1111BBBB2222","model":"a-1"},'
        '{"id":"b","baseURL":"https://b.com/v1","apiKey":"CCCC3333DDDD4444","model":"b-1"}]',
        "two_providers",
    ),
    # 12. 启发式：纯文本含 URL+key
    (
        "baseUrl: https://api.deepseek.com\nAPI_KEY=sk-1234567890abcd\n"
        "model: deepseek-flash",
        "fuzzy_text",
    ),
]

CASES_FAIL = [
    ("", "empty"),
    ("not json and no url", "garbage"),
    # `{"foo": "bar"}` 会产出一条 disabled row（id="provider"），不报错
    # ——矫治器只负责「能补齐到合法形态」，合理性是 GUI 的额外判断
]


def assert_eq(actual, expected, msg):
    if actual != expected:
        raise AssertionError(f"{msg}\n  expected: {expected!r}\n  got:      {actual!r}")


def run_pass():
    print("\n=== 正向用例 ===")
    for raw, label in CASES_PASS:
        try:
            r = normalize(raw)
            assert r.is_valid(), f"[{label}] 结果不含有效 row"
            # 必填字段（只校验 provider 类 row；model/policy/loop 不要求 wire/url）
            for row in r.rows:
                rid = row.get("id", "")
                if row.get("disabled"):
                    continue
                if rid != "model" and not rid.startswith(("policy", "loop", "thinking")):
                    conf = row["config"]
                    assert "wire" in conf, f"[{label}] 缺 wire: {row}"
                    assert "baseURL" in conf, f"[{label}] 缺 baseURL: {row}"
                    assert "model" in conf, f"[{label}] 缺 model: {row}"
                    ak = conf.get("apiKey")
                    assert isinstance(ak, dict) and "$expr" in ak, \
                        f"[{label}] apiKey 没归一化为 $expr: {ak!r}"
            # 必须能 json.dumps
            json.loads(r.to_json())
            print(f"  OK  [{label}] rows={len(r.rows)} warns={len(r.warnings)}")
        except ConfigNormalizeError as e:
            raise AssertionError(f"[{label}] 应该通过但报错: {e}")
        except AssertionError as e:
            print(f"  FAIL [{label}] {e}")
            raise


def run_fail():
    print("\n=== 负向用例 ===")
    for raw, label in CASES_FAIL:
        try:
            normalize(raw)
            raise AssertionError(f"[{label}] 应该失败但通过了")
        except ConfigNormalizeError as e:
            print(f"  OK  [{label}] 正确报错: {str(e)[:60]}")


def run_merge():
    print("\n=== 合并语义 ===")
    existing = [
        {"id": "deepseek", "name": "provider:deepseek",
         "config": {"wire": "openai", "baseURL": "https://old", "model": "old"}},
        {"id": "model", "name": "model:router",
         "config": {"primary": [["deepseek", "old"]], "fallback": []}},
    ]
    new = [
        {"id": "deepseek", "config": {"wire": "openai",
                                       "baseURL": "https://new",
                                       "model": "new"}},
        {"id": "stepfun", "config": {"wire": "openai",
                                      "baseURL": "https://step",
                                      "model": "step-3.5"}},
    ]
    merged = merge_with_user_layer(new, existing)
    ids = [r["id"] for r in merged]
    assert_eq(ids, ["deepseek", "model", "stepfun"], "顺序应保留")
    # deepseek 行是新的
    assert_eq(merged[0]["config"]["baseURL"], "https://new", "deepseek 被新覆盖")
    # model 行不动
    assert_eq(merged[1]["config"]["primary"][0][1], "old", "model 行保留")
    print("  OK  整行替换语义正确")


def run_sniff():
    print("\n=== 启发式抓取 ===")
    s = _sniff_text("随便一行 baseURL https://api.deepseek.com 然后 apiKey=sk-abc12345678xyz model=deepseek-flash")
    assert_eq(s.get("baseURL"), "https://api.deepseek.com", "baseURL 抓错")
    assert_eq(s.get("apiKey"), "sk-abc12345678xyz", "apiKey 抓错")
    assert "deepseek" in s.get("model", ""), f"model 没抓到: {s}"
    print(f"  OK  sniff → {s}")


if __name__ == "__main__":
    run_pass()
    run_fail()
    run_merge()
    run_sniff()
    print("\n✅ 全部用例通过")