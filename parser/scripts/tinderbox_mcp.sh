#!/bin/bash
# Tinderbox MCP server entry point. Wraps the venv'd Python invocation for
# Claude Desktop / Code config.
#
# In your Claude config, register this server with:
#   "command": "/Users/song/tinderbox/parser/scripts/tinderbox_mcp.sh"

set -e
export PYTHONPATH="/Users/song/tinderbox/parser"
export TINDERBOX_ENV_FILE="${TINDERBOX_ENV_FILE:-/Users/song/.secrets/shared.env}"
cd /Users/song/tinderbox/parser
exec /Users/song/tinderbox/parser/venv/bin/python3.14 \
  -m tinderbox.mcp.server
