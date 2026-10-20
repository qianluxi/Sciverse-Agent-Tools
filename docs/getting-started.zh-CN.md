# Sciverse Agent Tools 新手指引

本文面向第一次接触本项目的用户，帮助你在 10 分钟内完成接入。完整说明见根目录 [README.zh-CN.md](../README.zh-CN.md)。

## 1. 这个项目是什么

Sciverse Agent Tools 把 [Sciverse 开放平台](https://sciverse.space) 的学术检索能力封装成 6 个标准工具，供 LLM Agent 调用：

| 工具 | 用途 |
|---|---|
| `list_catalog` | 查询可用字段、过滤算子和枚举取值 |
| `search_papers` | 按作者、年份、期刊等结构化条件检索论文 |
| `semantic_search` | 自然语言语义检索（RAG 首选） |
| `list_paper_relations` | 获取某篇论文的引用、参考文献、相关工作 |
| `read_content` | 读取论文原文片段（Markdown） |
| `get_resource` | 下载原文中的图片、表格等二进制资源 |

支持 Python / TypeScript SDK、CLI、MCP Server、Claude Code skill 等多种接入方式。

## 2. 准备工作

- Python >= 3.10，或 Node.js（使用 npm 包 / MCP 时需要）
- 一个 Sciverse API Token：登录 <https://sciverse.space> 控制台申请，形如 `sv-...`
- 设置环境变量（推荐）：

```bash
# Linux / macOS
export SCIVERSE_API_TOKEN=sv-...

# Windows PowerShell
$env:SCIVERSE_API_TOKEN = "sv-..."
```

> Token 也可以通过 `sciverse auth login` 保存到 `~/.sciverse/credentials.json`。
> 读取优先级：显式传参 > 环境变量 > 凭据文件。

## 3. 选择接入方式

### 方式 A：在 AI 编程工具中使用（MCP）

适合 Claude Code、Cursor、Codex CLI、Windsurf 等支持 MCP 的 agent。在项目 MCP 配置文件中加入：

```json
{
  "mcpServers": {
    "sciverse": {
      "command": "npx",
      "args": ["-y", "sciverse-mcp-server"],
      "env": { "SCIVERSE_API_TOKEN": "${SCIVERSE_API_TOKEN}" }
    }
  }
}
```

各工具的详细配置见 [docs/integrations/](./integrations/)。

Claude Code 用户也可以直接安装插件：

```bash
claude /plugin marketplace add https://github.com/opendatalab/Sciverse-Agent-Tools
claude /plugin install sciverse
npm install -g sciverse-mcp-server
```

### 方式 B：Python SDK（写自己的 Agent）

```bash
pip install sciverse
```

```python
import asyncio
from sciverse import AgentToolsClient


async def main():
    async with AgentToolsClient() as c:
        r = await c.semantic_search(query="Transformer 注意力机制", top_k=5)
        for hit in r["hits"]:
            print(hit["title"], hit["score"])


asyncio.run(main())
```

注意：SDK 是异步接口，需配合 `async` / `await` 使用。

### 方式 C：命令行 CLI（快速验证）

安装 `sciverse` 后即可使用：

```bash
sciverse auth login                                  # 交互式保存 token
sciverse auth status                                 # 查看当前凭据
sciverse catalog --samples                           # 查看可用字段
sciverse search --author Hinton --year-from 2020     # 结构化检索
sciverse semantic-search "注意力机制"                # 语义检索
sciverse content <doc_id> --offset 0 --limit 4096    # 读取原文
```

输出为 JSON，可配合 `jq` 使用。

### 方式 D：接入 Agent 框架

SDK 内置各框架的工具 schema：

```python
from sciverse import ANTHROPIC_TOOLS, OPENAI_TOOLS
```

```ts
import { OPENAI_TOOLS, AgentToolsClient } from "sciverse";
```

完整可运行示例（Anthropic、OpenAI、LangChain、Claude Agent SDK 等）见 [examples/](../examples/)。

## 4. 推荐使用流程

1. 首次接入先调用 `list_catalog(include_sample_values=True)`，了解字段名和枚举值。
2. 用 `semantic_search` 做语义检索，或 `search_papers` 做精确过滤。
3. 检查结果中的 `is_content_accessible`，为 `true` 时再用 `read_content` 读取原文。
4. 原文 Markdown 中的图片通过 `get_resource` 获取。

## 5. 本地开发（修改本项目代码）

```bash
uv sync                  # 安装构建依赖
bash scripts/build.sh    # 由 openapi.yaml 生成 dist/ 和各包源码
uv run pytest tests/     # 运行测试
```

Windows 下 `scripts/build.sh` 需在 Git Bash 或 WSL 中执行。

## 6. 常见问题

| 现象 | 可能原因 / 处理 |
|---|---|
| 401 | Token 缺失或无效，检查 `SCIVERSE_API_TOKEN` |
| 400 | 参数或字段名错误，先调用 `list_catalog` 确认 |
| 429 | 触发配额 / 限流，稍后重试 |
| 502 / 503 | 上游服务不可用，稍后重试 |
| 找不到 `sciverse` 命令 | 确认 `pip install sciverse` 成功且脚本目录在 PATH 中 |

更多信息：

- 完整中文文档：[README.zh-CN.md](../README.zh-CN.md)
- 接口定义：[openapi.yaml](../openapi.yaml)
- 变更记录：[CHANGELOG.md](../CHANGELOG.md)
