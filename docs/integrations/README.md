# Coding agent 接入指南

> Sciverse 通过 MCP server [`sciverse-mcp-server`](../../packages/mcp/) 给主流 coding agent 提供学术文献检索能力。本目录是各 agent 的 5 分钟接入指南。

## 选择你的 agent

| Agent | 指南 | 配置文件 |
|---|---|---|
| Claude Code (`claude` CLI / VS Code) | [claude-code.md](./claude-code.md) | `.mcp.json` |
| Cursor | [cursor.md](./cursor.md) | `.cursor/mcp.json` |
| Codex CLI (OpenAI) | [codex-cli.md](./codex-cli.md) | `~/.codex/config.toml` |
| Windsurf (Codeium) | [windsurf.md](./windsurf.md) | `~/.codeium/windsurf/mcp_config.json` |

## 底层协议

所有 agent 都通过 [MCP (Model Context Protocol)](https://modelcontextprotocol.io) 接入。`sciverse-mcp-server` 是 stdio 形态的 MCP server，包装 Sciverse 六个 API：

- `list_catalog` — 字段 introspection（agent 第一次碰到不确定的字段先调此接口）
- `search_papers` — 结构化元数据检索（`collection` 可切到 authors / sources）
- `semantic_search` — 自然语言语义检索（RAG）
- `list_paper_relations` — 按 `unique_id` + `relation` 分页取引用 / 被引 / 相关工作
- `read_content` — 按 Unicode 码点区间读取文献原文
- `get_resource` — 取 `read_content` Markdown 中引用的图片字节流（多模态 RAG）

## 通用前置条件

1. Sciverse API Token：从 https://sciverse.space 控制台申请，形如 `sv-xxx`
2. Node.js 18+
3. **本地构建的 MCP server**（npm registry 上的同名包是上游 1.0.1 线，结构化检索叫 `search_metadata`，不要用）：

   ```bash
   cd packages/mcp && npm install && npm run build
   ```

   下文所有配置里的 `node` + `dist/cli.js` 都指这个构建产物；完整版本线对照见 [docs/INSTALL.md](../INSTALL.md)。

## 不在列表里的 agent？

只要支持 MCP stdio，都能用同样模式接入。把以下 JSON 翻译到对应 agent 的配置语法即可：

```json
{
  "command": "node",
  "args": ["/绝对路径/Sciverse-Agent-Tools/packages/mcp/dist/cli.js"],
  "env": { "SCIVERSE_API_TOKEN": "sv-xxx" }
}
```
