"""Smoke test: spawn the MCP server as a subprocess, exchange minimal protocol
messages, verify it responds correctly to initialize / tools/list / tools/call.

Run: PYTHONPATH=. python3.14 tests/test_mcp_smoke.py
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


SERVER = [sys.executable, "-m", "tinderbox.mcp.server"]


def _send(proc, msg):
    proc.stdin.write(json.dumps(msg).encode() + b"\n")
    proc.stdin.flush()


def _recv(proc, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line:
            return json.loads(line.decode().strip())
    raise TimeoutError("no response from MCP server")


def main():
    repo_root = str(Path(__file__).resolve().parent.parent)
    env_extras = {
        "PYTHONPATH": repo_root,
        "TINDERBOX_ENV_FILE": os.environ.get(
            "TINDERBOX_ENV_FILE",
            str(Path.home() / ".secrets" / "tinderbox.env"),
        ),
    }
    env = {**os.environ, **env_extras}

    print("Spawning MCP server...")
    proc = subprocess.Popen(
        SERVER,
        cwd=repo_root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # 1. initialize
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "0.0.1"},
            },
        })
        resp = _recv(proc)
        print("initialize result:", json.dumps(resp.get("result", {}), indent=2))
        assert "serverInfo" in resp.get("result", {}), "missing serverInfo"

        # 2. notifications/initialized (no response expected)
        _send(proc, {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })

        # 3. tools/list
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        })
        resp = _recv(proc)
        tools = resp.get("result", {}).get("tools", [])
        print(f"\ntools/list returned {len(tools)} tools:")
        for t in tools:
            print(f"  - {t['name']}: {t['description'][:80]}")
        assert any(t["name"] == "tinderbox_search" for t in tools)
        assert any(t["name"] == "tinderbox_get_conversation" for t in tools)

        # 4. tools/call tinderbox_search
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "tinderbox_search",
                "arguments": {"query": "Project Tinderbox schema design", "limit": 3},
            },
        })
        resp = _recv(proc, timeout=30)
        result = resp.get("result", {})
        content = result.get("content", [])
        print(f"\ntools/call tinderbox_search returned {len(content)} content blocks")
        for block in content:
            if block.get("type") == "text":
                txt = block.get("text", "")
                print("---")
                print(txt[:600])
        assert content and content[0].get("type") == "text"
        assert "Project Tinderbox" in content[0].get("text", "")

        # 5. tools/call with bad tool name -> isError
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "nope_not_a_tool", "arguments": {}},
        })
        resp = _recv(proc)
        # Should get either error or isError content
        is_err = "error" in resp or resp.get("result", {}).get("isError")
        print(f"\nbad-tool-name handled gracefully: error_field_present={is_err}")
        assert is_err

        print("\n✓ All smoke tests passed.")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
