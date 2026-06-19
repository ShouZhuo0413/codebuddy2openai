# codebuddy2openai

> 把 **CodeBuddy / WorkBuddy（腾讯代码助手）** 的订阅，转换成 **OpenAI 兼容 API**，让你能在任何支持 OpenAI 协议的客户端（ZCode、Cherry Studio、NextChat、LobeChat 等）里复用它。

> ⚠️ **关于 Codex CLI**：新版 Codex CLI 已不再支持 `wire_api = "chat"`，只支持 `completions` 格式，因此**本工具无法直接接入 Codex CLI**，请改用下方「OpenAI 兼容客户端」方案。

[English](#english) · [中文文档](#中文文档)

---

## 中文文档

一个极简的本地协议转换器（proxy / adapter）：读取你本机已登录的 CodeBuddy 桌面端凭据，把它的对话能力包装成标准的 OpenAI `/v1/chat/completions`、`/v1/models` 接口。**不碰登录授权、不碰你已有的客户端配置、跨平台、单文件。**

### ✨ 特性

- 🔄 **OpenAI 兼容**：标准 `/v1/chat/completions`（支持流式 SSE）、`/v1/models`、`/health`。
- 🛠️ **Function Calling（工具调用）**：支持请求里的 `tools`，返回 OpenAI 格式的 `tool_calls`，可在 ZCode / Cherry Studio 等 agent 客户端里驱动工具、多轮回传结果。
- 🪶 **单文件、极简**：核心就一个 `converter.py`，不复杂。
- 🔐 **零授权改动**：直接调用本机已登录的 `codebuddy` CLI，自动复用桌面端登录态，不重新登录、不存密码。
- 🖥️ **跨平台**：自动定位 macOS / Windows / Linux 上的 CLI 与登录文件。
- 🛡️ **安全**：默认只监听 `127.0.0.1`；调用 CLI 时禁用 CLI 内置工具，工具的声明与执行都由你的客户端负责。
- ⚡ **流式输出**：实时增量 token，体验与原生 OpenAI 流式一致。

### 🧠 它是怎么工作的

```
ZCode / Cherry Studio / 任意 OpenAI 客户端
        │  POST /v1/chat/completions  (OpenAI 协议)
        ▼
┌────────────────────┐
│  converter.py      │  ← 本地 FastAPI 服务 (127.0.0.1:8787)
│  协议转换          │
└────────────────────┘
        │  codebuddy -p --output-format stream-json
        ▼
┌────────────────────┐
│  CodeBuddy CLI     │  ← 自动复用桌面端登录态
│  (GLM-5.2 / Kimi / │
│   DeepSeek ...)    │
└────────────────────┘
```

转换器把 OpenAI 的 messages 翻译成 CLI 的 prompt，再把 CLI 的 `stream-json` 事件流（Anthropic 风格的 `content_block_delta` 等）实时转成 OpenAI 的 SSE chunk。

### 📦 前置条件

1. 已安装并**登录** CodeBuddy / WorkBuddy 桌面端（[腾讯云 CodeBuddy 官网](https://www.codebuddy.ai/)）。转换器会自动在这些位置找登录态：
   - **macOS**：`~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/*.info`
   - **Windows**：`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info`
   - **Linux**：`~/.local/share/CodeBuddyExtension/Data/Public/auth/*.info`
2. **Python 3.8+** 与 **Node.js**（CLI 是 node 脚本，桌面端自带）。
3. 安装依赖（一次性）：
   ```bash
   pip install fastapi "uvicorn[standard]"
   ```

### 🚀 快速开始

```bash
# 1. 克隆
git clone https://github.com/HanHan666666/codebuddy2openai.git
cd codebuddy2openai

# 2. 装依赖
pip install fastapi "uvicorn[standard]"

# 3. 启动（确保 CodeBuddy 桌面端已登录）
python3 converter.py
# 看到「✅ 监听 http://127.0.0.1:8787」即成功
```

启动时会做一次预检，打印找到的 CLI 和登录文件路径。

### 🛠️ Function Calling（工具调用）是怎么实现的

CodeBuddy CLI 只能选它**内置**的工具（Bash/Read/Edit…），不能注入客户端自定义的工具。为了让 ZCode / Cherry Studio 等 agent 客户端的工具也能用，转换器做了这层适配：

1. 客户端在请求里带上标准 OpenAI 的 `tools`（`[{"type":"function","function":{...}}]`）。
2. 转换器把工具说明格式化后注入对话（并约定模型用 `<tool_call>{"name":...,"arguments":...}</tool_call>` 格式输出）。
3. 调 CLI 时用 `--tools ""` 禁用其内置工具，让模型**只产出 tool call 文本、不自己执行**。
4. 转换器解析模型输出里的 `<tool_call>` 块，转成 OpenAI 的 `tool_calls`（`finish_reason:"tool_calls"`）返回。
5. 客户端执行完工具，把 `role:"tool"` 的结果再发回来，转换器拼回对话上下文，模型据此继续。

这样**工具的声明和执行都由客户端负责**，转换器只做协议桥接——客户端能用自己的全部工具集。

### 🔌 接入客户端

⚠️ **关于 Codex CLI（重要）**：新版本 Codex CLI 已**移除** `wire_api = "chat"` 的支持，目前只认 `completions` 格式，因此**本转换器无法直接接入 Codex CLI**。仓库里的 `codex-codebuddy.example.toml` 仅作历史/参考保留，实测在当前 Codex 上跑不通，请不要照抄。

✅ **可用方式 —— 任何标准 OpenAI 兼容客户端**（走 `/v1/chat/completions`）。常见选择：

- **ZCode**（OpenAI 兼容 Agent）
- **Cherry Studio**
- **NextChat / LobeChat / Open WebUI**
- 任何支持自定义 `base_url` 的 OpenAI SDK 客户端

通用接入步骤（以这类客户端为例）：

1. 保持转换器运行：`python3 converter.py`
2. 在客户端的「自定义模型 / OpenAI 兼容」设置里：
   - **API Base / 接口地址**：`http://127.0.0.1:8787/v1`
   - **API Key**：留空（转换器默认不校验）；若启动时用了 `--api-key`，则填同一个
   - **模型名**：`glm-5.2`（或 `kimi-k2.7` / `deepseek-v4-pro` / `auto` 等，见下方列表）

示例配置（如果你用的客户端读 toml / 自定义 provider 片段）：

```toml
[model_providers.codebuddy]
name = "CodeBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
env_key = "CODEBUDDY2OPENAI_KEY"
# 注意：本接口是 OpenAI chat 协议（/v1/chat/completions）。
# Codex CLI 因不再支持该 wire_api 而无法使用，请用 ZCode / Cherry Studio 等 OpenAI 兼容客户端。
```

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
├── converter.py                     # 转换器主程序（单文件）
├── codex-codebuddy.example.toml     # provider 配置示例片段（仅供参考；Codex CLI 已不支持，见上方说明）
├── README.md
└── LICENSE
```

### 🔧 命令行参数

```
python3 converter.py [--host HOST] [--port PORT] [--api-key KEY] [--cwd DIR] [--skip-check]
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--api-key` | 无 | 启用鉴权；客户端需带同样 key（也可用环境变量 `CODEBUDDY2OPENAI_KEY`）|
| `--cwd` | 当前目录 | CLI 工作目录 |
| `--skip-check` | 否 | 跳过启动预检 |

### ❓ 常见问题

- **找不到 CLI**：确认装了桌面端；或设环境变量 `CODEBUDDY_CODE_PATH` 指向 `cli/bin/codebuddy`。
- **找不到登录文件**：在桌面端完成登录（不是只装、要登进去）。
- **客户端报 401**：转换器若用了 `--api-key`，客户端那边要带同样的 key。
- **响应慢**：CLI 首次调用冷启动，后续会快；也可换 `deepseek-v4-flash` 等更快的模型。

### ⚠️ 免责声明

本项目为个人学习与研究用途，非官方产品，与腾讯 / CodeBuddy / OpenAI 无任何关联。使用本工具即表示你已阅读并同意：仅在你拥有合法订阅的前提下使用，遵守相关服务条款，自负风险。

### 📄 开源协议

[MIT](./LICENSE)

---

<a name="english"></a>
# English

A minimal local **protocol converter / proxy** that exposes your already-logged-in **CodeBuddy / WorkBuddy (Tencent coding assistant)** subscription as a standard **OpenAI-compatible API**, so you can use it from any OpenAI-protocol client (ZCode, Cherry Studio, NextChat, LobeChat, Open WebUI, etc.). **No auth changes, no edits to your client config, cross-platform, single file.**

> ⚠️ **Codex CLI note:** newer Codex CLI dropped `wire_api = "chat"` and only supports the `completions` format, so **this tool cannot be used with Codex CLI**. Use any OpenAI-compatible client instead.

### ✨ Features

- 🔄 **OpenAI-compatible**: standard `/v1/chat/completions` (streaming SSE), `/v1/models`, `/health`.
- 🪶 **Single-file & minimal**: core is one `converter.py`.
- 🔐 **Zero-auth hassle**: calls your locally-logged-in `codebuddy` CLI; reuses the desktop login session.
- 🖥️ **Cross-platform**: auto-locates CLI & auth on macOS / Windows / Linux.
- 🛡️ **Safe**: listens on `127.0.0.1` only; disables all built-in CLI tools for pure chat.

### 🚀 Quick Start

```bash
git clone https://github.com/HanHan666666/codebuddy2openai.git
cd codebuddy2openai
pip install fastapi "uvicorn[standard]"
python3 converter.py
```

Then point your OpenAI-compatible client at `http://127.0.0.1:8787/v1` (API base), leave the key blank unless you started the converter with `--api-key`. Note: Codex CLI is **not** supported (it dropped `wire_api = "chat"`); use ZCode, Cherry Studio, or any OpenAI-compatible client instead.

### ⚠️ Disclaimer

For personal learning and research only. Not affiliated with Tencent / CodeBuddy / OpenAI. Use only with a subscription you legally hold, in compliance with the relevant terms of service, at your own risk.

License: [MIT](./LICENSE)

---

<!-- SEO keywords -->
<sub>
**Keywords / 关键词:** codebuddy to openai · codebuddy2openai · codebuddy openai compatible api · codebuddy api proxy · codebuddy workbuddy openai adapter · tencent codebuddy openai · codebuddy glm-5.2 api · codebuddy kimi deepseek openai · openai compatible proxy local llm gateway · codebuddy function calling · codebuddy tool use tool_calls · codebuddy zcode cherry studio · 腾讯代码助手 openai · codebuddy 转 openai · codebuddy 接入 zcode cherry studio · 本地大模型代理 openai 协议 · codebuddy 订阅 复用 · workbuddy api 转换 · codebuddy 工具调用
</sub>
