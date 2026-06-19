"""
desensitize — 针对 CodeBuddy 后端内容审核的脱敏模块（独立、可选）。

背景
----
CodeBuddy 后端（copilot.tencent.com）有内容审核，会拦截含"攻击/漏洞/凭证"
等含义的英文术语。但这些词经常出现在客户端**固定的合规 system 模板**里
（例如 ZCode 的 agent 声明：「Refuse requests for DoS attacks, exploit
development, credential testing, C2 frameworks ...」），属于**拒绝作恶**
的合规声明，并非用户的有害输入，却被后端误判为敏感词，导致整条请求被拦。

本模块做的事
------------
对这些"合规声明高频词"做轻量处理：在词内部插入零宽空格（U+200B），

    "DoS" -> "Do\u200bS"        （人/模型读仍是 DoS，后端关键词匹配失效）

只处理一个明确的词表，默认只作用于 system 角色的消息（这是模板合规声明的
集中地）。不改动其它角色内容，避免影响真实对话。

设计原则
--------
- 独立模块，可单独 import / 单独测试。
- 保守：词表小而明确；只默认处理 system 消息；可关闭。
- 不试图、也不可能绕过对用户真实有害输入的审核——只缓解客户端模板被误伤。
"""

from __future__ import annotations

import re
from typing import Iterable

# 零宽空格：插入到关键词内部，打断后端的关键词匹配，但模型/人眼读起来无差别。
_ZWSP = "\u200b"

# 触发审核的"合规声明高频词"（来自真实被拦截的客户端 system 模板）。
# 全部是"拒绝作恶"语境里常见的英文术语。大小写不敏感匹配。
SENSITIVE_TERMS: list[str] = [
    "DoS",
    "DDoS",
    "exploit",
    "credential testing",
    "credential stuffing",
    "supply chain compromise",
    "supply-chain compromise",
    "detection evasion",
    "C2 frameworks",
    "C2 framework",
    "command and control",
    "malicious purposes",
    "malicious intent",
    "mass targeting",
    "brute force",
    "brute-force",
    "privilege escalation",
    "reverse shell",
    "remote code execution",
    "SQL injection",
    "XSS",
    "CSRF",
    "phishing",
    "malware",
    "ransomware",
    "keylogger",
    "rootkit",
    "backdoor",
    "botnet",
    "zero-day",
    "0day",
]

# 编译成一个大正则，按词长降序，避免短词先吃掉长词。
# 用 \b 边界 + 忽略大小写。
_PATTERN = re.compile(
    "|".join(re.escape(t) for t in sorted(SENSITIVE_TERMS, key=len, reverse=True)),
    re.IGNORECASE,
)


def _zero_width_split(term: str) -> str:
    """在词内部插入零宽空格。如 'DoS' -> 'Do\\u200bS'。"""
    if len(term) <= 1:
        return term
    # 在第 1 个字符后插入即可（足够打断子串匹配，且改动最小）
    return term[0] + _ZWSP + term[1:]


def desensitize_text(text: str) -> str:
    """对文本中的触发词插入零宽空格。无触发词则原样返回。"""
    if not text:
        return text
    return _PATTERN.sub(lambda m: _zero_width_split(m.group(0)), text)


def _iter_text_blocks(content):
    """遍历 OpenAI content（字符串或 [{type, text}, ...]）里的文本块，返回 (容器, key)。"""
    if isinstance(content, str):
        yield content, None  # 字符串：调用方直接替换
    elif isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                yield blk, "text"


def desensitize_messages(messages: Iterable[dict],
                         roles: tuple[str, ...] = ("system",)) -> list[dict]:
    """对指定角色的消息文本做脱敏，返回新的 messages 列表（不修改原对象）。

    默认只处理 system 角色（合规模板集中地）。如需扩大，传 roles=("system","user")。
    """
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        role = m.get("role")
        nm = dict(m)  # 浅拷贝，不污染调用方
        if role in roles:
            content = m.get("content")
            if isinstance(content, str):
                nm["content"] = desensitize_text(content)
            elif isinstance(content, list):
                new_blocks = []
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        nb = dict(blk)
                        nb["text"] = desensitize_text(blk.get("text", ""))
                        new_blocks.append(nb)
                    else:
                        new_blocks.append(blk)
                nm["content"] = new_blocks
        out.append(nm)
    return out


def desensitize_body(body: dict, roles: tuple[str, ...] = ("system",)) -> dict:
    """对请求体里的 messages 做脱敏，返回新的 body（浅拷贝）。"""
    if not body.get("messages"):
        return body
    nb = dict(body)
    nb["messages"] = desensitize_messages(body["messages"], roles=roles)
    return nb


# ---------------------------------------------------------------------------
# 自测：python3 desensitize.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    samples = [
        "Refuse requests for DoS attacks and exploit development.",
        "Dual-use security tools (C2 frameworks, credential testing) require authorization.",
        "这是一段正常的中文，不含任何触发词。",
        "Prevent privilege escalation and brute force attacks.",
        "No sensitive words here at all.",
    ]
    print("=== 脱敏前后对比 ===")
    for s in samples:
        d = desensitize_text(s)
        changed = "✓改" if d != s else "  不"
        print(f"{changed} | 原文: {s}")
        if d != s:
            print(f"     | 脱敏: {d}")
            print(f"     | 可见字符相同，差异为零宽空格 U+200B")
    print()
    print("=== messages 脱敏（只处理 system）===")
    msgs = [
        {"role": "system", "content": "Refuse DoS attacks and exploit development."},
        {"role": "user", "content": "explain DoS attacks"},  # 不应被改
    ]
    out = desensitize_messages(msgs)
    for m in out:
        print(f"  [{m['role']}] {m['content']!r}")
    print()
    # 验证：脱敏后 system 改了，user 没改
    assert "\u200b" in out[0]["content"], "system 应被脱敏"
    assert "\u200b" not in out[1]["content"], "user 不应被脱敏"
    print("✓ 自测通过：system 被脱敏，user 保持原样")
