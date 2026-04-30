#!/bin/bash
# Tinderbox first-time setup. Reads $HOME and $USER from your shell, fills in
# the templated launchd plists + launcher, drops them in the right places.
#
# Idempotent — re-running just refreshes the rendered files.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"

echo "Tinderbox setup"
echo "  HOME = $HOME"
echo "  USER = $USER"
echo "  REPO = $REPO_ROOT"
echo

# 1. Fill the launcher
echo "[1/3] Rendering launcher script..."
mkdir -p "$REPO_ROOT/parser/scripts"
sed -e "s|__HOME__|$HOME|g" \
    "$REPO_ROOT/parser/scripts/templates/tinderbox_mcp.sh" \
    > "$REPO_ROOT/parser/scripts/tinderbox_mcp.sh"
chmod +x "$REPO_ROOT/parser/scripts/tinderbox_mcp.sh"

# 2. Fill the launchd plists
echo "[2/3] Rendering launchd plists into $LAUNCHD_DIR..."
mkdir -p "$LAUNCHD_DIR"
for tpl in "$REPO_ROOT/parser/launchd/templates/"com.USER.tinderbox.*.plist; do
    base=$(basename "$tpl")
    rendered_name="${base/com.USER./com.$USER.}"
    sed -e "s|__HOME__|$HOME|g" -e "s|__USER__|$USER|g" \
        "$tpl" > "$LAUNCHD_DIR/$rendered_name"
    echo "  $rendered_name"
done

# 3. Tell the user what to do next
echo "[3/3] To activate the daemons, run:"
echo "    launchctl load \"$LAUNCHD_DIR/com.$USER.tinderbox.scan.plist\""
echo "    launchctl load \"$LAUNCHD_DIR/com.$USER.tinderbox.qa.plist\""
echo "    launchctl load \"$LAUNCHD_DIR/com.$USER.tinderbox.staleness.plist\""
echo
echo "MCP launcher is at:"
echo "    $REPO_ROOT/parser/scripts/tinderbox_mcp.sh"
echo
echo "Add to your Claude config (Code or Desktop):"
echo "    \"tinderbox\": { \"command\": \"$REPO_ROOT/parser/scripts/tinderbox_mcp.sh\" }"
echo
echo "Done."
