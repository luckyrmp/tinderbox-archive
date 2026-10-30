#!/bin/bash
# Read-only Tinderbox MCP server for the Tailscale bridge (com.song.tinderbox.mcp-bridge).
# Uses ONLY ~/.secrets/tinderbox-reader.env (TINDERBOX_DB_DSN, role tinderbox_reader).
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
unset SUPABASE_URL SUPABASE_SERVICE_KEY TINDERBOX_MCP_TOKEN
export PYTHONPATH="$REPO_ROOT"
export TINDERBOX_ENV_FILE="$HOME/.secrets/tinderbox-reader.env"
exec "$REPO_ROOT/venv/bin/python3.14" -m tinderbox.mcp.server
