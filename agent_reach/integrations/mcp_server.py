# -*- coding: utf-8 -*-
"""
Agent Reach MCP Server — status plus read-only platform tools for chat clients.

Run: python -m agent_reach.integrations.mcp_server

Agents that can run shell commands should call upstream tools directly (see
SKILL.md). Chat clients such as Claude Desktop cannot, so this server exposes
each documented upstream command as an MCP tool (see mcp_tools.py).
"""

import asyncio
import json
import sys
from typing import Any

from agent_reach.config import Config
from agent_reach.core import AgentReach
from agent_reach.integrations import mcp_tools
from agent_reach.utils.text import scrub_url_credentials

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

    HAS_MCP = True
except ImportError:
    HAS_MCP = False


def _tool(name: str, description: str, schema: dict):
    # mcp 1.x and 2.x both accept the wire name ``inputSchema``; 2.x's typed
    # signature only lists ``input_schema``, which 1.x rejects.
    fields: dict[str, Any] = {"name": name, "description": description, "inputSchema": schema}
    return Tool(**fields)


def create_server():
    if not HAS_MCP:
        print(
            "MCP not installed. Install: python -m pip install "
            "'agent-reach[mcp] @ "
            "https://github.com/Panniantong/agent-reach/archive/main.zip'",
            file=sys.stderr,
        )
        sys.exit(1)

    config = Config(read_only=True)
    eyes = AgentReach(config)

    async def list_tools():
        return [
            _tool(
                "get_status",
                "Get Agent Reach status: which channels are installed and active.",
                {"type": "object", "properties": {}},
            ),
            *(_tool(spec.name, spec.description, spec.input_schema) for spec in mcp_tools.TOOLS),
        ]

    async def call_tool(name: str, arguments: dict):
        try:
            if name == "get_status":
                result = eyes.doctor_report()
            else:
                result = await asyncio.to_thread(mcp_tools.call, name, arguments, config)

            text = json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, (dict, list)) else str(result)
            return [TextContent(type="text", text=text)]
        except Exception as e:
            return [
                TextContent(
                    type="text",
                    text=f"Error: {scrub_url_credentials(e)}",
                )
            ]

    if hasattr(Server, "list_tools"):
        # mcp 1.x: handlers are registered with decorators.
        server = Server("agent-reach")
        getattr(server, "list_tools")()(list_tools)
        getattr(server, "call_tool")()(call_tool)
        return server

    # mcp 2.x: handlers are passed to the constructor and receive request params.
    async def on_list_tools(ctx, params):
        return ListToolsResult(tools=await list_tools())

    async def on_call_tool(ctx, params):
        return CallToolResult(content=await call_tool(params.name, params.arguments or {}))

    return Server("agent-reach", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def main():
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
