"""端到端示例：Claude Agent SDK + Sciverse MCP server 跑一次 RAG。

与 `python_anthropic_rag.py` 的区别：
- 该示例用 `anthropic` SDK 的 `messages.create` 原生 tool calling 回环
  + `sciverse` Python SDK 内嵌的 ANTHROPIC_TOOLS 常量
- 本示例用 `claude-agent-sdk`，通过 `mcp_servers` 配置直接挂 sciverse-mcp-server，
  Claude Code 风格的 agent loop 由 SDK 全权处理 —— 这是 coding-agent 风格应用的主流方式

运行（MCP server 用本仓库构建，不要装 npm 上的同名包——那是 1.0.1 旧线，
工具名是 search_metadata）：
    pip install claude-agent-sdk
    cd packages/mcp && npm install && npm run build
    export SCIVERSE_API_TOKEN=...
    export ANTHROPIC_API_KEY=...
    python examples/python_claude_agent_sdk.py
"""
import os
from pathlib import Path

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

# 本仓库构建出来的 MCP server（`cd packages/mcp && npm run build`）。
# 用环境变量可覆盖，便于放到别的位置。
MCP_ENTRY = os.environ.get(
    "SCIVERSE_MCP_ENTRY",
    str(Path(__file__).resolve().parent.parent / "packages" / "mcp" / "dist" / "cli.js"),
)


async def main() -> None:
    options = ClaudeAgentOptions(
        # mcp_servers：直接挂本仓库构建的 MCP server，Claude Code agent loop
        # 自动 spawn 进程、转发 tool list / call。
        mcp_servers={
            "sciverse": {
                "command": "node",
                "args": [MCP_ENTRY],
                "env": {
                    "SCIVERSE_API_TOKEN": os.environ["SCIVERSE_API_TOKEN"],
                },
            },
        },
        # 显式允许要用的 tool（server 一共暴露 6 个）；默认 Claude Code 会请求确认，
        # agent 场景下用 allowed_tools 跳过 prompt。
        allowed_tools=[
            "mcp__sciverse__search_papers",
            "mcp__sciverse__semantic_search",
            "mcp__sciverse__read_content",
        ],
        # 按需指定 model（默认走 SDK 配置的 Claude Code 默认模型）
        # 例如：model="claude-opus-4-7" 或 model="claude-sonnet-4-6"
        system_prompt=(
            "你是学术文献检索助手。优先用 semantic_search 找相关 chunk，"
            "需要扩展上下文时用 read_content。每个引用都附 doc_id 与 title。"
        ),
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "找 3 篇关于 Transformer 注意力机制的论文，每篇引用一段原文。"
        )
        async for message in client.receive_response():
            # message 类型：AssistantMessage / ToolUseBlock / TextBlock / ResultMessage 等。
            # 简单 demo：把所有 text 打出来。
            for block in getattr(message, "content", []) or []:
                if hasattr(block, "text"):
                    print(block.text, end="", flush=True)
        print()  # 末尾换行


if __name__ == "__main__":
    anyio.run(main)
