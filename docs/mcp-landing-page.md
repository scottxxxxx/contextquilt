# ContextQuilt MCP Server

> Give your AI agent persistent memory in one line.

## What is it?

ContextQuilt is an [MCP](https://modelcontextprotocol.io) server that gives any AI agent persistent cognitive memory. Your agent remembers user preferences, decisions, commitments, and relationships — across sessions, across platforms, across conversations.

## Connect in 30 seconds

### Claude Code

```bash
claude mcp add contextquilt --transport sse \
  --url https://mcp.contextquilt.com/sse \
  --header "Authorization: Bearer YOUR_API_KEY"
```

### Claude Desktop

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "contextquilt": {
      "command": "npx",
      "args": [
        "-y", "@anthropic-ai/mcp-remote",
        "https://mcp.contextquilt.com/sse",
        "--header", "Authorization: Bearer YOUR_API_KEY"
      ]
    }
  }
}
```

### Any MCP Client

Add to `.mcp.json`:

```json
{
  "mcpServers": {
    "contextquilt": {
      "type": "sse",
      "url": "https://mcp.contextquilt.com/sse",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

## What can your agent do?

| Tool | What it does | Speed |
|------|-------------|-------|
| `recall_context` | "What do I know about this user?" — retrieves relevant memory from the knowledge graph | <50ms |
| `store_memory` | "Remember this conversation" — queues content for async extraction into structured patches | <1ms |
| `get_quilt` | "Show me everything" — returns all patches for a user | <100ms |
| `delete_patch` | "Forget this" — removes a specific memory | <50ms |

## How it works

```
Your Agent → store_memory("User prefers dark mode and uses TypeScript")
                ↓
        ContextQuilt Worker (async)
                ↓
        Extracts: [preference] "Prefers dark mode"
                  [preference] "Uses TypeScript"
                ↓
Your Agent → recall_context("Set up the user's IDE")
                ↓
        Returns: "About you: Prefers dark mode, Uses TypeScript"
```

No LLM call on the read path. Pure graph traversal.

## Get an API Key

Email [scott@contextquilt.com](mailto:scott@contextquilt.com) to get your MCP API key.

## Self-Host

Don't want to use our hosted server? Run your own:

```bash
git clone https://github.com/scottxxxxx/contextquilt.git
cd contextquilt
cp .env.example .env  # Add your LLM API key
docker compose up -d
# MCP server at http://localhost:8001/sse
```

## Open Source

ContextQuilt is Apache 2.0 licensed. [View on GitHub](https://github.com/scottxxxxx/contextquilt).
