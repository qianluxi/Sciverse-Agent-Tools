// 端到端示例：OpenAI Agents SDK + Sciverse MCP server 跑一次 RAG。
//
// 与 `ts_openai.ts` 的区别：
// - 那个示例用 `openai` SDK 的 chat completions + 原生 function calling 回环
//   + `sciverse` 内嵌的 OPENAI_TOOLS。
// - 本示例用 `@openai/agents`，通过 mcpServers 配置直接挂 sciverse-mcp-server，
//   agent loop 由 SDK 处理 —— coding-agent 风格应用主流方式。
//
// 运行（MCP server 用本仓库构建，不要装 npm 上的同名包——那是 1.0.1 旧线）：
//     cd packages/mcp && npm install && npm run build
//     npm install @openai/agents
//     export SCIVERSE_API_TOKEN=...
//     export OPENAI_API_KEY=...
//     tsx examples/ts_openai_agents.ts

import { fileURLToPath } from "node:url";

import { Agent, run, MCPServerStdio } from "@openai/agents";

async function main() {
  // 启动本仓库构建出来的 MCP server（stdio）
  const sciverse = new MCPServerStdio({
    name: "sciverse",
    command: process.execPath,
    args: [fileURLToPath(new URL("../packages/mcp/dist/cli.js", import.meta.url))],
    env: {
      SCIVERSE_API_TOKEN: process.env.SCIVERSE_API_TOKEN!,
    },
  });
  await sciverse.connect();

  try {
    const agent = new Agent({
      name: "Sciverse Agent",
      instructions:
        "你是学术文献检索助手。优先用 semantic_search 找相关 chunk，" +
        "需要扩展上下文时用 read_content。每个引用都附 doc_id 与 title。",
      mcpServers: [sciverse],
    });

    const result = await run(
      agent,
      "找 3 篇关于 Transformer 注意力机制的论文，每篇引用一段原文。",
    );
    console.log(result.finalOutput);
  } finally {
    await sciverse.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
