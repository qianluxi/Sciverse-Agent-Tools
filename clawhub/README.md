# academic-retrieval — ClawHub skill bundle

ClawHub skill that gives any OpenClaw agent Sciverse academic-paper retrieval
capabilities (English | [中文](#中文说明)).

This bundle is **not published to the ClawHub registry** from this repo. The
registry entry `@sciverse/academic-retrieval` is the upstream 1.0.1 line — its
structured-search tool is `search_metadata` (not `search_papers`) and its
`facets` shape differs. Install this bundle from the local directory instead.

## Install

```bash
# from the repo root — local bundle, matches this repo's openapi.yaml
openclaw skills install ./clawhub
```

> Registry alternative (upstream 1.0.1, not this repo):
> `openclaw skills install @sciverse/academic-retrieval`

## Configure

```bash
export SCIVERSE_API_TOKEN=sv-xxx       # obtain from https://sciverse.space
```

## Tools at a glance

| Tool | Purpose |
|---|---|
| `list_catalog` | Field introspection (call once to learn available fields + enum values) |
| `search_papers` | Structured metadata search over papers / authors / sources (set `collection`) |
| `semantic_search` | Natural-language semantic chunk retrieval (for RAG) |
| `list_paper_relations` | Paged citations / references for one paper (`unique_id` + `relation`) |
| `read_content` | Character-range read of a paper's original text (offset/limit in Unicode code points) |
| `get_resource` | Fetch figure / table image bytes referenced inside `read_content` Markdown |

See `SKILL.md` for full agent-facing documentation.

## Direct invocation (bypass OpenClaw)

```bash
node scripts/semantic_search.mjs '{"query":"Transformer attention mechanism","top_k":3}'
```

## Relationship to the SDK

This skill is **complementary** to the SDKs in `packages/`:

- **This skill** — OpenClaw users only. Zero external deps (Node 18+ native fetch),
  scripts call the Sciverse HTTP API directly.
- **Python / TypeScript SDK** — Any LLM agent framework (OpenAI, Anthropic, LangChain, LlamaIndex…).

## License

Apache-2.0

---

## 中文说明

OpenClaw 用户专用：通过 ClawHub 给 agent 加上 Sciverse 学术文献检索能力。

本 fork **尚未把这份 bundle 发布到 ClawHub registry**。registry 上的
`@sciverse/academic-retrieval` 是上游 1.0.1 线（结构化检索叫 `search_metadata`，
`facets` 形状也不同），与本仓库的 `search_papers` 不是同一套接口。请从本地目录安装。

### 安装

```bash
# 在仓库根目录执行 —— 本地 bundle，与 openapi.yaml 一致
openclaw skills install ./clawhub
```

> 想装 registry 版本可以用 `openclaw skills install @sciverse/academic-retrieval`，
> 但那是上游 1.0.1，不是本仓库实现。

### 配置

```bash
export SCIVERSE_API_TOKEN=sv-xxx   # 从 https://sciverse.space 控制台申请
# 可选：export SCIVERSE_BASE_URL=https://api-custom.sciverse.space
```

### 工具速览

| Tool | 用途 |
|---|---|
| `list_catalog` | 字段 introspection（首次接入调一次，学习可用字段和 enum 取值） |
| `search_papers` | 按结构化条件查 papers / authors / sources（用 `collection` 切换实体集合） |
| `semantic_search` | 自然语言语义检索文献片段（RAG 用） |
| `list_paper_relations` | 按 `unique_id` + `relation` 分页取引用 / 被引 / 相关工作 |
| `read_content` | 按 Unicode 码点区间读取文献原文片段 |
| `get_resource` | 取 `read_content` Markdown 中引用的图片字节流（多模态 RAG） |

agent 视角的完整文档见 `SKILL.md`（英文）。

### 直接调用（不通过 OpenClaw）

```bash
node scripts/semantic_search.mjs '{"query":"Transformer 注意力机制","top_k":3}'
```

### 与 SDK 的关系

本 skill 与 `packages/` 里的 SDK 是**互补**的：

- **本 skill**：OpenClaw 用户专用，零外部依赖（仅 Node 18+ native fetch），脚本直接调 Sciverse HTTP API
- **Python / TypeScript SDK**：任意 LLM Agent 框架（OpenAI / Anthropic / LangChain / LlamaIndex...）
