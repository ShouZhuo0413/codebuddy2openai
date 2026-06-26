# codebuddy2openai

> 把 **CodeBuddy / WorkBuddy（腾讯代码助手）** 的订阅，转换成 **OpenAI 兼容 API**，让你能在 **Codex CLI**、ZCode、Cherry Studio、NextChat、LobeChat 等任何支持 OpenAI 协议的客户端里复用它。

[English](#english) · [中文文档](#中文文档)

---

## 中文文档

一个极简的本地协议转换器（proxy / adapter）：读取你本机已登录的 CodeBuddy 桌面端凭据，直接转发到 CodeBuddy 后端，在本地暴露标准的 OpenAI `/v1/chat/completions`、`/v1/responses`、`/v1/models` 接口。**不碰登录授权、不碰你已有的客户端配置、跨平台、轻量。**

### ✨ 特性

- 🔄 **OpenAI 兼容**：标准 `/v1/chat/completions`（支持流式 SSE）、`/v1/responses`（Responses API，Codex CLI 兼容）、`/v1/models`、`/health`。
- 🤖 **Codex CLI 支持**：内置 Responses API 适配层，自动将 Chat SSE 转换为 Responses 语义事件流，可直接接入 Codex CLI。
- 🛠️ **Function Calling（工具调用）**：支持请求里的 `tools`，返回 OpenAI 格式的 `tool_calls`，可在 ZCode / Cherry Studio 等 agent 客户端里驱动工具、多轮回传结果。
- 🪶 **轻量极简**：核心是 `converter.py` + `responses_adapter.py`，依赖少、易部署。
- 🔐 **零授权改动**：自动复用本机已登录的 CodeBuddy 桌面端凭据，不重新登录、不存密码。
- 🖥️ **跨平台**：自动定位 macOS / Windows / Linux 上的登录凭据文件。
- 🛡️ **安全**：默认只监听 `127.0.0.1`；工具的声明与执行都由客户端负责，转换器只做鉴权与透传。
- ⚡ **流式输出**：实时增量 token，体验与原生 OpenAI 流式一致。

### 🧠 它是怎么工作的

```
Codex CLI                         Cherry Studio / ZCode / 任意 OpenAI 客户端
    │  POST /v1/responses              │  POST /v1/chat/completions
    │  (Responses API)                 │  (Chat Completions API)
    ▼                                  ▼
┌─────────────────────────────────────────────┐
│  converter.py  ← 本地 FastAPI (127.0.0.1:8787)  │
│  ┌───────────────────────┐                  │
│  │ responses_adapter.py  │  Responses→Chat  │
│  │ Chat SSE→Responses 事件│  双向转换        │
│  └───────────────────────┘                  │
│  读 token + 注入鉴权 header + 透传          │
└─────────────────────────────────────────────┘
        │  POST /v2/chat/completions  (带 Authorization/X-User-Id 等头)
        ▼
┌────────────────────────────────┐
│  copilot.tencent.com 后端      │  ← 原生标准 OpenAI 协议
│  (GLM-5.2 / Kimi / DeepSeek)   │     含原生 tools / tool_calls / SSE 流式
└────────────────────────────────┘
```

转换器直连 CodeBuddy 后端（`copilot.tencent.com/v2/chat/completions`），该后端本身就是**标准 OpenAI chat/completions 协议**。转换器只做两件事：①读取本机登录凭据并注入鉴权 header；②在本地 `/v1/*` 与后端 `/v2/*` 之间透传。因为后端原生支持 `tools` / `tool_calls`，function calling 是模型自带能力，**无需任何 prompt 注入或文本解析**。token 过期时转换器会自动调刷新接口并回写。

> 历史版本曾通过「调 CLI + 文本标签解析」实现 function calling，但在嵌套 agent（subagent）场景下，subagent 的输出会夹带标签污染对话。**v2.0 改为直连后端，彻底解决了这个问题。**

### 📦 前置条件

1. 已安装并**登录** CodeBuddy / WorkBuddy 桌面端（[腾讯云 CodeBuddy 官网](https://www.codebuddy.ai/)）。转换器会自动在这些位置找登录态：
   - **macOS**：`~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/*.info`
   - **Windows**：`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info`
   - **Linux**：`~/.local/share/CodeBuddyExtension\Data\Public\auth\*.info`
2. **Python 3.8+**（无需 Node.js，不再依赖 CLI）。
3. 安装依赖（一次性）：
   ```bash
   pip install fastapi "uvicorn[standard]" httpx
   ```

### 🚀 快速开始

```bash
# 1. 克隆
git clone https://github.com/HanHan666666/codebuddy2openai.git
cd codebuddy2openai

# 2. 装依赖
pip install fastapi "uvicorn[standard]" httpx

# 3. 启动（确保 CodeBuddy 桌面端已登录）
python3 converter.py
# 看到「✅ 监听 http://127.0.0.1:8787」即成功
```

启动时会做一次预检，打印账号信息和 token 状态。

### 🛠️ Function Calling（工具调用）

后端原生支持标准 OpenAI function calling。客户端（如 ZCode / Cherry Studio）在请求里带 `tools`，模型原生返回 `tool_calls`（`finish_reason:"tool_calls"`），客户端执行工具后把 `role:"tool"` 的结果回传即可——和直连 OpenAI 完全一致。流式、非流式、多轮工具调用都支持。

### 🔌 接入客户端

#### ✅ 方式一：Codex CLI（推荐）

转换器内置了 Responses API 适配层（`/v1/responses`），可直接接入 Codex CLI。

1. 保持转换器运行：`python3 converter.py`
2. 将以下配置合并到 `~/.codex/config.toml`（或项目 `codex.toml`）：

```toml
[model_providers.codebuddy]
name = "CodeBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"              # 使用 Responses API
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.codebuddy]
model = "glm-5.2"                   # 也可用 kimi-k2.7 / deepseek-v4-pro / auto
model_provider = "codebuddy"
```

3. 设置环境变量（值随便填，除非转换器启用了 `--api-key`）：
```bash
export CODEBUDDY2OPENAI_KEY=any-value
```

4. 启动 Codex CLI：
```bash
codex --profile codebuddy "你的任务描述"
```

#### ✅ 方式二：OpenAI 兼容客户端

任何标准 OpenAI 兼容客户端（走 `/v1/chat/completions`）均可直接使用：

- **ZCode**（OpenAI 兼容 Agent）
- **Cherry Studio**
- **NextChat / LobeChat / Open WebUI**
- 任何支持自定义 `base_url` 的 OpenAI SDK 客户端

1. 保持转换器运行：`python3 converter.py`
2. 在客户端的「自定义模型 / OpenAI 兼容」设置里：
   - **API Base / 接口地址**：`http://127.0.0.1:8787/v1`
   - **API Key**：留空（转换器默认不校验）；若启动时用了 `--api-key`，则填同一个
   - **模型名**：`glm-5.2`（或 `kimi-k2.7` / `deepseek-v4-pro` / `auto` 等，见下方列表）

### 🧪 curl 验证

```bash
# 列模型
curl http://127.0.0.1:8787/v1/models

# 非流式
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'

# 流式
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","stream":true,"messages":[{"role":"user","content":"数1到5"}]}'
```

### 🤖 可用模型

`glm-5.2`、`glm-5.1`、`glm-5v-turbo`、`kimi-k2.7`、`kimi-k2.6`、`kimi-k2.5`、`deepseek-v4-pro`、`deepseek-v4-flash`、`minimax-m3-pay`、`hy3-preview-agent`、`auto`

（来自 CLI `--help` 的 `--model` 说明，具体可用性以你的订阅为准。）

### 📁 项目结构

```
codebuddy2openai/
├── converter.py                     # 转换器主程序
├── responses_adapter.py             # Responses API 适配层（Codex CLI 兼容）
├── desensitize.py                   # 脱敏模块（可选，--desensitize 启用）
├── codex-codebuddy.example.toml     # Codex CLI / 客户端配置示例
├── test_responses_adapter.py        # 适配层单元测试
├── README.md
└── LICENSE
```

### 🔧 命令行参数

```
python3 converter.py [--host HOST] [--port PORT] [--api-key KEY] [--log PATH] [--desensitize] [--skip-check]
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--api-key` | 无 | 启用鉴权；客户端需带同样 key（也可用环境变量 `CODEBUDDY2OPENAI_KEY`）|
| `--log` | 无 | **开启日志并写到该文件**（如 `--log converter.log`）。不传则不记。也可用环境变量 `CODEBUDDY2OPENAI_LOG`。|
| `--desensitize` | 关 | 启用脱敏：对客户端注入的合规/运行时说明文字做零宽脱敏，并对 Codex CLI 注入的超长提示做压缩摘要；同时可裁掉 `tools` 描述字段，缓解被后端内容审核误拦（见下方 FAQ）。|
| `--skip-check` | 否 | 跳过启动预检 |

示例：
```bash
python3 converter.py --log converter.log          # 记日志到当前目录 converter.log
python3 converter.py --log /tmp/cb.log            # 记到指定路径
python3 converter.py                              # 不记日志
```

每条日志记录：模型、是否流式、消息数、最后一条用户提问、耗时、finish_reason、工具调用、token 数；若后端内容审核拦截会标 `⚠️内容审核拦截`。**每次请求都用唯一 ID 串起来，并完整落盘**：发往后端的完整请求体（REQUEST BODY）、后端返回的完整内容（非流式是聚合后的 RESPONSE BODY，流式是后端原始的 RESPONSE RAW SSE）。排查"内容审核拦截""返回异常"等问题时，直接看日志里对应 ID 的完整报文即可。示例：
```
[2026-06-19 11:56:32] [9cc4488e] ▶ REQUEST glm-5.2 | stream=False | msgs=1 | last_user='Reply: pong'
[2026-06-19 11:56:32] [9cc4488e] ── REQUEST BODY ──
{ "model": "glm-5.2", "messages": [{"role":"user","content":"Reply: pong"}] }
[2026-06-19 11:56:35] [9cc4488e] ◀ RESPONSE glm-5.2 | 3.0s | finish=stop | tokens=11
[2026-06-19 11:56:35] [9cc4488e] ── RESPONSE BODY ──
{ "choices":[{"message":{"content":"pong"},...}], "usage":{...} }
```

### ❓ 常见问题

- **找不到登录文件**：在桌面端完成登录（不是只装、要登进去）。路径见上方「前置条件」。
- **客户端报 401**：转换器若用了 `--api-key`，客户端那边要带同样的 key；若是后端 401，可能是 token 失效（转换器会自动刷新，若仍失败需在桌面端重新登录）。
- **响应慢**：可换 `deepseek-v4-flash` 等更快的模型。
- **"敏感内容"被拦截**：这是 CodeBuddy 后端的**内容审核**（腾讯合规策略），在模型推理之前就拦了。对 Codex CLI 来说，常见触发点不只是一条 system prompt，还包括客户端注入的 `developer` 说明、运行时上下文（如 `AGENTS.md` / sandbox / permissions / skills 文本），以及工具 schema 的 `description` 字段——这些内容里常含 DoS / exploit / credential / sandbox / escalation / dangerous 等安全术语，虽然语义上是在**约束模型拒绝作恶**，但仍可能被后端误伤。两种应对：①用 `--log xxx.log` 在日志里看 `⚠️内容审核拦截` 标记，直接定位对应请求的 `REQUEST BODY`；②加 `--desensitize` 启用脱敏模块（`desensitize.py`），它会对这些客户端固定模板/上下文字段做零宽脱敏，并进一步把 Codex CLI 注入的超长运行时提示压缩成短摘要、移除 `tools` 里的高风险描述字段，同时**不改真实用户输入**。这比只改 system prompt 更适合 Codex CLI 场景。注意：这些处理只针对客户端固定模板，不能也不应绕过对用户真实有害输入的审核。

### ⚠️ 免责声明

本项目为个人学习与研究用途，非官方产品，与腾讯 / CodeBuddy / OpenAI 无任何关联。使用本工具即表示你已阅读并同意：仅在你拥有合法订阅的前提下使用，遵守相关服务条款，自负风险。

### 📄 开源协议

[MIT](./LICENSE)

---

<a name="english"></a>
# English

A minimal local **protocol converter / proxy** that exposes your already-logged-in **CodeBuddy / WorkBuddy (Tencent coding assistant)** subscription as a standard **OpenAI-compatible API**, so you can use it from **Codex CLI**, ZCode, Cherry Studio, NextChat, LobeChat, Open WebUI, or any OpenAI-protocol client. **No auth changes, cross-platform.**

### ✨ Features

- 🔄 **OpenAI-compatible**: `/v1/chat/completions` (streaming SSE), `/v1/responses` (Responses API for Codex CLI), `/v1/models`, `/health`.
- 🤖 **Codex CLI support**: built-in Responses API adapter converts Chat SSE to Responses semantic events.
- 🛠️ **Function Calling**: native `tools` / `tool_calls` support — works with ZCode, Cherry Studio, and any agent client.
- 🪶 **Minimal**: core is `converter.py` + `responses_adapter.py`, few dependencies.
- 🔐 **Zero-auth hassle**: reuses the desktop login session automatically; no re-login, no stored passwords.
- 🖥️ **Cross-platform**: auto-locates auth credentials on macOS / Windows / Linux.
- 🛡️ **Safe**: listens on `127.0.0.1` only; tool declaration and execution are handled by the client.
- ⚡ **Streaming**: real-time incremental tokens, indistinguishable from native OpenAI streaming.

### 🚀 Quick Start

```bash
git clone https://github.com/HanHan666666/codebuddy2openai.git
cd codebuddy2openai
pip install fastapi "uvicorn[standard]" httpx
python3 converter.py
# Look for "✅ 监听 http://127.0.0.1:8787" — that means it's running.
```

Then point your client at `http://127.0.0.1:8787/v1`. For **Codex CLI**, add `wire_api = "responses"` to your config (see `codex-codebuddy.example.toml`). For other OpenAI-compatible clients, just set the API base URL.

### 🛠️ Function Calling

The backend natively supports standard OpenAI function calling. Send `tools` in the request and the model returns `tool_calls` (`finish_reason: "tool_calls"`). Execute the tool, send back a `role: "tool"` message, and continue — exactly like calling OpenAI directly. Streaming, non-streaming, and multi-turn tool calls are all supported.

### 🤖 Available Models

`glm-5.2`, `glm-5.1`, `glm-5v-turbo`, `kimi-k2.7`, `kimi-k2.6`, `kimi-k2.5`, `deepseek-v4-pro`, `deepseek-v4-flash`, `minimax-m3-pay`, `hy3-preview-agent`, `auto`

(Availability depends on your subscription.)

### 🔧 CLI Options

```
python3 converter.py [--host HOST] [--port PORT] [--api-key KEY] [--log PATH] [--desensitize] [--skip-check]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `127.0.0.1` | Listen address |
| `--port` | `8787` | Listen port |
| `--api-key` | none | Enable client auth; client must send the same key (or set `CODEBUDDY2OPENAI_KEY`) |
| `--log` | none | Write logs to this file (e.g. `--log converter.log`). Also via `CODEBUDDY2OPENAI_LOG`. |
| `--desensitize` | off | Zero-width desensitization of client-injected compliance/runtime text to avoid backend content-review false positives |
| `--skip-check` | no | Skip startup preflight |

### ⚠️ Disclaimer

For personal learning and research only. Not affiliated with Tencent / CodeBuddy / OpenAI. Use only with a subscription you legally hold, in compliance with the relevant terms of service, at your own risk.

License: [MIT](./LICENSE)

---

<!-- SEO keywords -->
<sub>
**Keywords / 关键词:** codebuddy to openai · codebuddy2openai · codebuddy openai compatible api · codebuddy api proxy · codebuddy workbuddy openai adapter · tencent codebuddy openai · codebuddy glm-5.2 api · codebuddy kimi deepseek openai · openai compatible proxy local llm gateway · codebuddy function calling · codebuddy tool use tool_calls · codebuddy zcode cherry studio · 腾讯代码助手 openai · codebuddy 转 openai · codebuddy 接入 zcode cherry studio · 本地大模型代理 openai 协议 · codebuddy 订阅 复用 · workbuddy api 转换 · codebuddy 工具调用
</sub>
