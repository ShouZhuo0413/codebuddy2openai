#!/usr/bin/env python3
"""
codebuddy2openai — 把 CodeBuddy / WorkBuddy 的订阅能力包装成 OpenAI 兼容 API。

原理：
  - 不碰登录/授权：直接调用本机已安装的 CodeBuddy CLI（`codebuddy -p`），
    CLI 会自动复用桌面端登录态（读取 CodeBuddyExtension 下的 auth 文件）。
  - 把 OpenAI 的 /v1/chat/completions 请求翻译成 CLI 的 `--output-format stream-json`
    事件流，再转成 OpenAI 的响应（支持 stream 与非 stream）。

跨平台：自动定位 CLI 与 auth 目录（macOS / Windows / Linux）。
依赖：fastapi + uvicorn（pip install fastapi "uvicorn[standard]"）。
安全：默认只监听 127.0.0.1；调 CLI 时禁用所有内置工具（--tools ""），仅纯对话。

用法：
  python3 converter.py                       # 默认 127.0.0.1:8787
  python3 converter.py --port 9000
  python3 converter.py --api-key mysecret    # 启用鉴权
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import AsyncGenerator, Iterable, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

# ---------------------------------------------------------------------------
# 平台相关：定位 CLI 与 auth 目录
# ---------------------------------------------------------------------------

def auth_dirs() -> list[Path]:
    """桌面端登录态所在目录（与 app.asar 中 EXTENSION_DATA_DIR_NAME 一致）。"""
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        return [home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    if plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [local / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return [xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth"]


def find_auth_file() -> Path | None:
    for d in auth_dirs():
        if d.is_dir():
            for f in sorted(d.glob("*.info")):
                return f
    return None


def cli_candidates() -> list[Path]:
    """所有可能的 CLI 可执行路径（按优先级）。"""
    home = Path.home()
    plat = sys.platform
    cands: list[Path] = []

    env = os.environ.get("CODEBUDDY_CODE_PATH")
    if env:
        cands.append(Path(env))

    if plat == "darwin":
        for apps in (Path("/Applications"), home / "Applications"):
            cands += list(apps.glob("**/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"))
            cands += list(apps.glob("**/CodeBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy"))
    elif plat == "win32":
        pf = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        pf86 = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        for root in (pf, pf86, local):
            cands += list(root.glob("**/[Ww]ork[Bb]uddy/**/cli/bin/codebuddy*"))
            cands += list(root.glob("**/[Cc]ode[Bb]uddy/**/cli/bin/codebuddy*"))
    else:
        for opt in (Path("/opt"), home / ".local", home / ".codebuddy"):
            cands += list(opt.glob("**/cli/bin/codebuddy"))

    for name in ("codebuddy", "cbc"):
        p = shutil.which(name)
        if p:
            cands.append(Path(p))

    # 去重保序
    seen: set[str] = set()
    uniq: list[Path] = []
    for c in cands:
        k = str(c)
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def find_cli() -> Path | None:
    for c in cli_candidates():
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# 模型列表（来自 CLI --help 的 --model 说明）
# ---------------------------------------------------------------------------

DEFAULT_MODELS = [
    "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "kimi-k2.7", "kimi-k2.6", "kimi-k2.5",
    "deepseek-v4-pro", "deepseek-v4-flash",
    "minimax-m3-pay", "hy3-preview-agent", "auto",
]


# ---------------------------------------------------------------------------
# OpenAI messages -> CLI prompt
# ---------------------------------------------------------------------------

# 工具调用标签（模型按此约定输出，转换器解析）
TOOL_CALL_OPEN = "<tool_call>"
TOOL_CALL_CLOSE = "</tool_call>"

# 注入到对话里的「可用工具说明 + 输出约定」
TOOLS_SYSTEM_PROMPT = """\
# Available tools
You may call tools to help answer the user's request. Tools are described below as JSON Schemas.

To call a tool, output ONLY this block and nothing else (no prose before or after):
{open}
{{"name": "<tool_name>", "arguments": {{<json object matching the tool's parameters>}}}}
{close}

Rules:
- Output the {open}...{close} block verbatim. The JSON inside must be valid (double quotes, no trailing commas).
- Call AT MOST ONE tool per turn. Wait for the result before continuing.
- If you do not need a tool, answer the user directly in plain text.
- "arguments" must match the tool's parameter schema. Omit unknown fields.

## Tools
""".format(open=TOOL_CALL_OPEN, close=TOOL_CALL_CLOSE)


def _content_to_text(content) -> str:
    if isinstance(content, list):
        bits = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                bits.append(blk.get("text", ""))
            elif isinstance(blk, str):
                bits.append(blk)
        return "\n".join(bits)
    if not isinstance(content, str):
        return str(content)
    return content


def _format_tools(tools: list[dict]) -> str:
    """把 OpenAI tools（[{"type":"function","function":{...}}]）渲染成可读说明。"""
    lines: list[str] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") == "function" else t
        if not isinstance(fn, dict):
            continue
        name = fn.get("name", "?")
        desc = (fn.get("description") or "").strip()
        params = fn.get("parameters") or {}
        lines.append(f"- {name}: {desc}")
        # 简要列参数
        props = params.get("properties") if isinstance(params, dict) else None
        if isinstance(props, dict) and props:
            required = params.get("required", []) if isinstance(params, dict) else []
            pstrs = []
            for pname, pinfo in props.items():
                if not isinstance(pinfo, dict):
                    continue
                ptype = pinfo.get("type", "any")
                pdesc = (pinfo.get("description") or "").strip().split("\n")[0]
                req = "required" if pname in required else "optional"
                pstrs.append(f'    - {pname} ({ptype}, {req}): {pdesc}')
            if pstrs:
                lines.append("  parameters:")
                lines.extend(pstrs)
    return "\n".join(lines)


def messages_to_prompt(messages: list[dict], tools: list[dict] | None = None) -> str:
    """把 OpenAI messages 数组拼成纯文本 prompt，保留角色与多轮上下文。

    若 tools 非空，注入工具说明，并处理 assistant 的 tool_calls 与 tool 结果。
    """
    parts: list[str] = []

    # 1) 若有工具，先注入工具说明
    if tools:
        parts.append("[system]\n" + TOOLS_SYSTEM_PROMPT + _format_tools(tools))

    # 2) 遍历 messages
    for m in messages:
        role = m.get("role", "user")
        if role == "assistant" and m.get("tool_calls"):
            # assistant 发起的工具调用：还原成约定的 <tool_call> 文本
            calls = []
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                name = fn.get("name")
                args = fn.get("arguments", "{}")
                # arguments 可能是 JSON 字符串或对象
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass
                calls.append(json.dumps({"name": name, "arguments": args}, ensure_ascii=False))
            block = TOOL_CALL_OPEN + "\n" + "\n".join(calls) + "\n" + TOOL_CALL_CLOSE
            extra = _content_to_text(m.get("content", "")).strip()
            parts.append(f"[assistant]\n{block}" + (f"\n{extra}" if extra else ""))
            continue

        if role == "tool":
            # 工具执行结果回传
            tid = m.get("tool_call_id", "")
            text = _content_to_text(m.get("content", "")).strip()
            parts.append(f"[tool result{(' id=' + tid) if tid else ''}]\n{text}")
            continue

        text = _content_to_text(m.get("content", "")).strip()
        if not text:
            continue
        tag = {"system": "system", "user": "user", "assistant": "assistant"}.get(role, role)
        parts.append(f"[{tag}]\n{text}")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 解析模型输出里的 <tool_call> 块 -> OpenAI tool_calls
# ---------------------------------------------------------------------------

_TOOL_CALL_RE = re.compile(
    re.escape(TOOL_CALL_OPEN) + r"(.*?)" + re.escape(TOOL_CALL_CLOSE),
    re.DOTALL,
)


def parse_tool_calls(text: str) -> list[dict]:
    """从模型输出文本里解析所有 <tool_call> 块，返回 OpenAI tool_calls 列表（可能为空）。"""
    calls: list[dict] = []
    for m in _TOOL_CALL_RE.finditer(text):
        raw = m.group(1).strip()
        # 块里可能有多个 JSON（每行一个），也可能是一个对象
        for cand in _split_json_objects(raw):
            try:
                obj = json.loads(cand)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            name = obj.get("name")
            args = obj.get("arguments", {})
            if name is None:
                continue
            if isinstance(args, (dict, list)):
                args = json.dumps(args, ensure_ascii=False)
            elif not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            calls.append({
                "id": "call_" + uuid.uuid4().hex[:24],
                "type": "function",
                "function": {"name": str(name), "arguments": args},
            })
    return calls


def strip_tool_call_blocks(text: str) -> str:
    """去掉所有 <tool_call> 块，返回剩余纯文本（用于 content 字段）。"""
    cleaned = _TOOL_CALL_RE.sub("", text)
    return cleaned.strip()


def _split_json_objects(s: str) -> list[str]:
    """把可能含多个 JSON 对象的字符串，按顶层 { } 边界切分。"""
    objs: list[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                objs.append(s[start:i + 1])
                start = -1
    return objs


# ---------------------------------------------------------------------------
# 调用 CLI 并解析 stream-json 事件流
# ---------------------------------------------------------------------------

class CliError(RuntimeError):
    pass


def build_cli_argv(prompt: str, model: str | None) -> list[str]:
    cli = find_cli()
    if cli is None:
        raise CliError(
            "未找到 CodeBuddy CLI。请先安装并登录 CodeBuddy / WorkBuddy 桌面端，"
            "或用环境变量 CODEBUDDY_CODE_PATH 指定 CLI 路径。"
        )
    node = shutil.which("node")
    if cli.suffix.lower() in (".cmd", ".bat", "") and node:
        argv = [node, str(cli)]  # bin/codebuddy 是 node 脚本
    else:
        argv = [str(cli)]
    argv += [
        "-p",                          # 非交互，打印后退出
        "--output-format", "stream-json",
        "--include-partial-messages",  # 增量 token 事件（流式用）
        "--verbose",
        "--tools", "",                 # 禁用所有内置工具，纯对话
        "--model", model or "auto",
        prompt,
    ]
    return argv


def run_cli_stream(prompt: str, model: str | None, cwd: str) -> Iterable[dict]:
    """
    启动 CLI 子进程，逐行读取 stream-json，yield 解析后的事件 dict。
    结束时检查退出码，非 0 抛 CliError。
    """
    argv = build_cli_argv(prompt, model)
    env = dict(os.environ)
    env.setdefault("CI", "1")
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError as e:
        raise CliError(f"无法启动 CLI：{e}")

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # 非 JSON 行（调试日志）忽略
    finally:
        proc.stdout.close()
        rc = proc.wait()
        if rc not in (0, None):
            err = ""
            if proc.stderr:
                try:
                    err = proc.stderr.read()
                except Exception:
                    err = ""
            raise CliError(f"CLI 退出码 {rc}。{err[:500]}")


# ---------------------------------------------------------------------------
# 事件归一化：CLI 的 stream_event（Anthropic 风格）/ assistant / result -> 统一事件
# ---------------------------------------------------------------------------

def normalize_events(events: Iterable[dict]) -> Iterable[dict]:
    """
    yield:
        {"kind": "delta", "text": "..."}
        {"kind": "final", "text": "...", "usage": {...}, "stop_reason": "..."}
        {"kind": "error", "message": "..."}
    """
    final_text_parts: list[str] = []
    final_usage: dict | None = None
    stop_reason: str | None = None
    saw_result = False

    for ev in events:
        t = ev.get("type")

        if t == "stream_event":
            inner = ev.get("event", {})
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    txt = delta.get("text", "")
                    if txt:
                        yield {"kind": "delta", "text": txt}
            continue

        if t == "assistant":
            msg = ev.get("message", {})
            final_usage = msg.get("usage") or final_usage
            stop_reason = msg.get("stop_reason") or stop_reason
            for blk in msg.get("content", []) or []:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    txt = blk.get("text", "")
                    if txt:
                        final_text_parts.append(txt)
            continue

        if t == "result":
            saw_result = True
            if ev.get("is_error"):
                yield {"kind": "error", "message": str(ev.get("result", "未知错误"))}
                return
            if ev.get("usage"):
                final_usage = ev["usage"]
            if not final_text_parts and ev.get("result"):
                final_text_parts.append(str(ev["result"]))
            if ev.get("subtype") == "success_max_turns" and not stop_reason:
                stop_reason = "max_tokens"
            continue

        if t == "system" and ev.get("subtype") == "error":
            yield {"kind": "error", "message": str(ev.get("message", "CLI 系统错误"))}
            return

    if not saw_result and not final_text_parts:
        yield {"kind": "error", "message": "CLI 未返回任何结果"}
        return

    yield {
        "kind": "final",
        "text": "".join(final_text_parts),
        "usage": final_usage or {},
        "stop_reason": stop_reason or "stop",
    }


# ---------------------------------------------------------------------------
# OpenAI 响应构造
# ---------------------------------------------------------------------------

def _usage_to_openai(usage: dict | None) -> dict:
    usage = usage or {}
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    return {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out}


def make_chat_completion(text: str, model: str, usage: dict | None,
                         tools: list[dict] | None = None) -> dict:
    tool_calls = parse_tool_calls(text) if tools else []
    if tool_calls:
        # 模型发起了工具调用：返回 tool_calls，content 去掉 tool_call 块
        content = strip_tool_call_blocks(text) or None
        message = {"role": "assistant", "content": content, "tool_calls": tool_calls}
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": text}
        finish = "stop"
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish,
        }],
        "usage": _usage_to_openai(usage),
    }


def sse_chunk(cid: str, model: str, *, delta_content: str | None = None,
              role: str | None = None, finish_reason: str | None = None,
              tool_calls: list[dict] | None = None,
              tool_call_index: int | None = None) -> str:
    delta: dict = {}
    if role is not None:
        delta["role"] = role
    if delta_content is not None:
        delta["content"] = delta_content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="codebuddy2openai", version="1.0")

# 运行时配置（由 main() 注入）
CONFIG = {"api_key": "", "cwd": os.getcwd()}


def _check_auth(authorization: str | None, x_api_key: str | None):
    key = CONFIG["api_key"]
    if not key:
        return
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    if token != key:
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})


@app.get("/health")
def health():
    return {
        "status": "ok",
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "cli": str(find_cli() or "(未找到)"),
        "auth_file": str(find_auth_file() or "(未找到)"),
    }


@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    data = [{"id": m, "object": "model", "created": 1700000000, "owned_by": "codebuddy"}
            for m in DEFAULT_MODELS]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    model = payload.get("model") or "auto"
    stream = bool(payload.get("stream"))
    tools = payload.get("tools") or None
    prompt = messages_to_prompt(messages, tools)
    cwd = CONFIG["cwd"]

    # 启动 CLI（同步子进程）；在异步上下文中放线程池执行，避免阻塞事件循环
    import asyncio
    loop = asyncio.get_event_loop()

    def make_iter():
        return run_cli_stream(prompt, model, cwd)

    if stream:
        return StreamingResponse(
            _stream_response(loop, make_iter, model, tools),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：在线程池里跑完
    def run_all():
        return list(normalize_events(make_iter()))

    try:
        events = await loop.run_in_executor(None, run_all)
    except CliError as e:
        raise HTTPException(status_code=502, detail={"error": {"message": str(e), "type": "upstream_error"}})

    final_text, usage = "", None
    for ev in events:
        k = ev.get("kind")
        if k == "final":
            final_text = ev.get("text", "")
            usage = ev.get("usage")
        elif k == "error":
            raise HTTPException(status_code=502, detail={"error": {"message": ev.get("message", "error"), "type": "upstream_error"}})
    return JSONResponse(make_chat_completion(final_text, model, usage, tools))


async def _stream_response(loop, make_iter, model: str,
                           tools: list[dict] | None = None) -> AsyncGenerator[bytes, None]:
    """把 CLI 事件流转换成 OpenAI SSE。CLI 读取放到线程池里逐行消费。

    有 tools 时：tool_calls 以文本形式产出且可能跨多个 delta，因此先缓冲完整文本，
    再在结尾判断是否是工具调用（若是则输出 tool_calls，否则整段输出文本）。
    无 tools 时：逐 delta 实时流式输出。
    """
    import asyncio
    cid = "chatcmpl-" + uuid.uuid4().hex

    yield sse_chunk(cid, model, role="assistant").encode("utf-8")

    queue: asyncio.Queue = asyncio.Queue()
    SENTINEL = object()

    def producer():
        try:
            for ev in normalize_events(make_iter()):
                asyncio.run_coroutine_threadsafe(queue.put(ev), loop).result()
        except CliError as e:
            asyncio.run_coroutine_threadsafe(
                queue.put({"kind": "error", "message": str(e)}), loop).result()
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(SENTINEL), loop).result()

    loop.run_in_executor(None, producer)

    had_error = False

    if tools:
        # 有工具：缓冲完整文本，结尾统一判定
        # 注意：normalize_events 先 yield 多个 delta，再 yield 一个 final（含完整文本）。
        # final.text 已是完整文本，deltas 是它的片段，二者不可叠加，否则重复。
        delta_parts: list[str] = []   # 仅在没收到 final 时兜底
        final_text: str | None = None
        while True:
            item = await queue.get()
            if item is SENTINEL:
                break
            k = item.get("kind")
            if k == "delta":
                delta_parts.append(item.get("text", ""))
            elif k == "final":
                final_text = item.get("text", "")
            elif k == "error":
                had_error = True
                yield sse_chunk(cid, model, delta_content=f"[error] {item.get('message','')}").encode("utf-8")

        full_text = final_text if final_text is not None else "".join(delta_parts)
        tool_calls = parse_tool_calls(full_text)
        if had_error:
            yield sse_chunk(cid, model, finish_reason="error").encode("utf-8")
        elif tool_calls:
            # 流式发 tool_calls（OpenAI 约定：每个 call 一个 chunk，arguments 可分片）
            for idx, tc in enumerate(tool_calls):
                yield sse_chunk(cid, model, tool_calls=[{
                    "index": idx,
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
                }]).encode("utf-8")
            yield sse_chunk(cid, model, finish_reason="tool_calls").encode("utf-8")
        else:
            # 不是工具调用：把文本作为单个 content delta 发出
            yield sse_chunk(cid, model, delta_content=full_text).encode("utf-8")
            yield sse_chunk(cid, model, finish_reason="stop").encode("utf-8")
        yield b"data: [DONE]\n\n"
        return

    # 无工具：逐 delta 实时流式
    while True:
        item = await queue.get()
        if item is SENTINEL:
            break
        k = item.get("kind")
        if k == "delta":
            yield sse_chunk(cid, model, delta_content=item.get("text", "")).encode("utf-8")
        elif k == "error":
            had_error = True
            yield sse_chunk(cid, model, delta_content=f"[error] {item.get('message','')}").encode("utf-8")
        # final: usage 暂不入 chunk（OpenAI 流式 usage 走可选的最后一个 chunk，这里略）

    yield sse_chunk(cid, model, finish_reason="error" if had_error else "stop").encode("utf-8")
    yield b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def preflight() -> bool:
    cli = find_cli()
    auth = find_auth_file()
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"CLI       : {cli or '(未找到)'}\n")
    sys.stderr.write(f"登录文件  : {auth or '(未找到)'}\n")
    if auth_dirs():
        sys.stderr.write(f"已查目录  : {', '.join(str(d) for d in auth_dirs())}\n")
    ok = True
    if cli is None:
        sys.stderr.write("\n[警告] 未找到 CodeBuddy CLI。请安装 WorkBuddy/CodeBuddy 桌面端，\n"
                         "       或设置环境变量 CODEBUDDY_CODE_PATH 指向 CLI。\n")
        ok = False
    if auth is None:
        sys.stderr.write("\n[警告] 未找到登录文件。请在桌面端完成登录。\n")
        ok = False
    sys.stderr.write("================\n")
    return ok


def main():
    ap = argparse.ArgumentParser(description="CodeBuddy -> OpenAI 兼容转换器")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default=os.environ.get("CODEBUDDY2OPENAI_KEY", ""),
                    help="可选：要求客户端携带的 API key（默认不校验）")
    ap.add_argument("--cwd", default=os.getcwd(), help="CLI 工作目录（默认跟随当前目录）")
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    args = ap.parse_args()

    CONFIG["api_key"] = args.api_key
    CONFIG["cwd"] = args.cwd

    if not args.skip_check:
        preflight()

    sys.stderr.write(f"\n✅ 监听 http://{args.host}:{args.port}\n")
    sys.stderr.write("   GET  /v1/models\n")
    sys.stderr.write("   POST /v1/chat/completions   (支持 stream:true)\n")
    sys.stderr.write("   GET  /health\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
    sys.stderr.write("按 Ctrl+C 退出。\n\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
