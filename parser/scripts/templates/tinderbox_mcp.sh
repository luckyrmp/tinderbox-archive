#!/bin/bash
# Tinderbox MCP server entry point. Wraps the venv'd Python invocation for
# Claude Desktop / Code config.
#
# In your Claude config, register this server with:
#   "command": "__HOME__/tinderbox/parser/scripts/tinderbox_mcp.sh"

set -e
export PYTHONPATH="__HOME__/tinderbox/parser"
export TINDERBOX_ENV_FILE="${TINDERBOX_ENV_FILE:-__HOME__/.secrets/shared.env}"
cd __HOME__/tinderbox/parser
exec __HOME__/tinderbox/parser/venv/bin/python3.14 \
  -m tinderbox.mcp.server
