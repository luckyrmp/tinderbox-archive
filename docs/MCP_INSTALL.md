# Installing the Tinderbox MCP server

The MCP server is a stdio process exposing two tools:

- `tinderbox_search(query, limit)` — hybrid retrieval over the archive
- `tinderbox_get_conversation(export_id, max_messages)` — fetch a full thread

Both tools log every call to `tinderbox.queries` (provenance + future eval data).

---

## 1. Claude Code (terminal)

Add to your global Claude Code MCP config (`~/.claude/mcp.json`) or project-scoped (`.claude/mcp.json` in your repo):

```json
{
  "mcpServers": {
    "tinderbox": {
      "command": "/Users/song/tinderbox/parser/scripts/tinderbox_mcp.sh"
    }
  }
}
```

That's it. Next `claude` session will list `tinderbox_search` + `tinderbox_get_conversation` in available tools.

## 2. Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tinderbox": {
      "command": "/Users/song/tinderbox/parser/scripts/tinderbox_mcp.sh"
    }
  }
}
```

Restart Claude Desktop. The tools appear in the tool picker.

## 3. claude.ai web

The web app supports remote MCP servers (HTTP/SSE), not stdio. Tinderbox runs locally on the Mac Studio and is stdio-only for stage 1. To expose it to claude.ai web you'd need a Tailscale Funnel or similar HTTP proxy in front — out of scope for stage 5.

---

## Smoke test from a Claude session

After install, ask Claude something that should hit the archive:

> "Search Tinderbox for the conversation where we decided on the 5MB raw_content limit."

Claude should call `tinderbox_search` with your query and surface the conversation. If the tool isn't visible, double-check the config path and restart the client.

## Verify the server runs standalone

```bash
/Users/song/tinderbox/parser/scripts/tinderbox_mcp.sh
```

It will read JSON-RPC on stdin and write to stdout. Press Ctrl+D to exit. (Logs go to stderr.)

Or run the smoke test:

```bash
cd /Users/song/tinderbox/parser
PYTHONPATH=. ./venv/bin/python3.14 tests/test_mcp_smoke.py
```

Should print `✓ All smoke tests passed.`
