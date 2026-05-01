#!/bin/bash
# Tinderbox MCP server entry point for Claude Desktop / Claude Code.
#
# Usage in claude_desktop_config.json:
#   "tinderbox": {
#     "command": "/path/to/tinderbox-archive/scripts/tinderbox_mcp.sh"
#   }
#
# Required environment variables (set in your shell or via TINDERBOX_ENV_FILE):
#   SUPABASE_URL          — your Supabase project URL
#   SUPABASE_SERVICE_KEY  — service role key
#   ANTHROPIC_API_KEY     — for enrichment (not needed for search-only use)
#
# Optional:
#   TINDERBOX_ENV_FILE    — path to a .env file (default: ~/.secrets/tinderbox.env)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export PYTHONPATH="$REPO_ROOT"
export TINDERBOX_ENV_FILE="${TINDERBOX_ENV_FILE:-$HOME/.secrets/tinderbox.env}"

exec python3 -m tinderbox.mcp.server
