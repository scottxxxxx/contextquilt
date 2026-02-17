"""Main entry point for the ContextQuilt MCP Server."""

import asyncio
import structlog
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, Resource, ReadResourceResult, TextResourceContents, CallToolResult, TextContent

from .tools import get_user_profile, update_user_memory, search_user_history
from .resources import get_user_profile_resource

import sys
import logging
import structlog

# Configure logging to write to stderr to avoid interfering with MCP stdio protocol
logging.basicConfig(
    format="%(message)s",
    stream=sys.stderr,
    level=logging.INFO,
)

structlog.configure(
    processors=[
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

def create_server() -> Server:
    """Create and configure the MCP server."""
    server = Server("contextquilt-mcp")

    # Register Tools
    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return [
            Tool(
                name="get_user_context",
                description="Get comprehensive user profile from ContextQuilt's longitudinal memory",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": "Unique identifier for the user"
                        },
                        "include_history": {
                            "type": "boolean",
                            "description": "Include conversation history",
                            "default": True
                        }
                    },
                    "required": ["user_id"]
                }
            ),
            Tool(
                name="update_user_memory",
                description="Update user memory with new conversation or facts",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "conversation_log": {"type": "string"},
                        "facts": {
                            "type": "array",
                            "items": {"type": "string"}
                        }
                    },
                    "required": ["user_id", "conversation_log"]
                }
            ),
            Tool(
                name="search_user_history",
                description="Search through user's conversation history",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "query": {"type": "string"}
                    },
                    "required": ["user_id", "query"]
                }
            )
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict | None) -> list:
        if not arguments:
            raise ValueError("Arguments required")
            
        if name == "get_user_context":
            return await get_user_profile(
                user_id=arguments["user_id"],
                include_history=arguments.get("include_history", True)
            )
        elif name == "update_user_memory":
            return await update_user_memory(
                user_id=arguments["user_id"],
                conversation_log=arguments["conversation_log"],
                facts=arguments.get("facts", [])
            )
        elif name == "search_user_history":
            return await search_user_history(
                user_id=arguments["user_id"],
                query=arguments["query"]
            )
        else:
            raise ValueError(f"Unknown tool: {name}")

    # Register Resources
    @server.list_resources()
    async def handle_list_resources() -> list[Resource]:
        return [
            Resource(
                uri="contextquilt://users/{user_id}/profile",
                name="User Profile",
                description="Comprehensive user profile",
                mimeType="application/json"
            )
        ]

    @server.read_resource()
    async def handle_read_resource(uri: str) -> list:
        contents = await get_user_profile_resource(uri)
        # contents is a list of dicts (from resources.py)
        real_contents = []
        for c in contents:
            # Reconstruct object
            obj = TextResourceContents(uri=c["uri"], mimeType=c["mimeType"], text=c["text"])
            # Monkey patch attributes because mcp server seems to want snake_case and 'content'
            setattr(obj, "content", c["text"])
            setattr(obj, "mime_type", c["mimeType"])
            real_contents.append(obj)
        return real_contents

    return server

async def main():
    """Run the MCP server."""
    server = create_server()
    
    # Run using stdio transport
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )

if __name__ == "__main__":
    asyncio.run(main())
