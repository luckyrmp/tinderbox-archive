"""Minimal MCP (Model Context Protocol) server over stdio.

We implement just enough of the JSON-RPC 2.0 protocol to expose tools to a
Claude client. Reference: https://modelcontextprotocol.io/specification

Methods we handle:
  - initialize           handshake
  - notifications/initialized   (notification; no response expected)
  - tools/list           list available tools + JSON schemas
  - tools/call           execute a tool by name, return content blocks

We don't implement: prompts, resources, sampling, roots — none are needed
for a search-only server.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

# Logging goes to stderr (stdout is the JSON-RPC channel)
logger = logging.getLogger("tinderbox.mcp")

PROTOCOL_VERSION = "2025-03-26"


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], list[dict[str, Any]]]
    """handler: takes the args dict, returns a list of MCP content blocks
    (each block is {'type': 'text', 'text': '...'} or similar)."""


class MCPServer:
    def __init__(self, server_name: str, server_version: str):
        self.server_name = server_name
        self.server_version = server_version
        self.tools: dict[str, Tool] = {}
        self.initialized = False

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self.tools[tool.name] = tool

    # ----- IO -----

    def _write(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, separators=(",", ":"))
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def _read_message(self) -> dict[str, Any] | None:
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning("Bad JSON from client: %s", e)
            return None

    # ----- Response builders -----

    def _result(self, request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id: Any, code: int, message: str,
               data: Any | None = None) -> dict[str, Any]:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": err}

    # ----- Method handlers -----

    def _handle_initialize(self, request_id: Any, params: dict) -> dict[str, Any]:
        client_info = params.get("clientInfo", {})
        logger.info("Client initialize: %s", client_info)
        return self._result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": self.server_name,
                "version": self.server_version,
            },
        })

    def _handle_tools_list(self, request_id: Any, params: dict) -> dict[str, Any]:
        tools = [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in self.tools.values()
        ]
        return self._result(request_id, {"tools": tools})

    def _handle_tools_call(self, request_id: Any, params: dict) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in self.tools:
            return self._error(request_id, -32602, f"unknown tool: {name}")
        tool = self.tools[name]
        try:
            content = tool.handler(args)
        except Exception as e:
            tb = traceback.format_exc()
            logger.warning("Tool %s raised: %s\n%s", name, e, tb)
            return self._result(request_id, {
                "content": [
                    {"type": "text",
                     "text": f"Tool error: {type(e).__name__}: {e}"}
                ],
                "isError": True,
            })
        return self._result(request_id, {"content": content})

    # ----- Run loop -----

    def run(self) -> None:
        logger.info("MCP server %s/%s starting", self.server_name, self.server_version)
        while True:
            msg = self._read_message()
            if msg is None:
                logger.info("EOF on stdin; exiting")
                return

            method = msg.get("method")
            request_id = msg.get("id")
            params = msg.get("params") or {}

            # Notifications have no `id`; we don't respond.
            is_notification = "id" not in msg

            if method == "initialize":
                resp = self._handle_initialize(request_id, params)
                self._write(resp)
            elif method == "notifications/initialized":
                self.initialized = True
                logger.info("Client signaled initialized")
                # No response for notifications.
            elif method == "tools/list":
                resp = self._handle_tools_list(request_id, params)
                self._write(resp)
            elif method == "tools/call":
                resp = self._handle_tools_call(request_id, params)
                self._write(resp)
            elif method in ("ping",):
                self._write(self._result(request_id, {}))
            else:
                if is_notification:
                    logger.debug("Ignoring unknown notification: %s", method)
                else:
                    self._write(self._error(request_id, -32601, f"method not found: {method}"))


def text_block(text: str) -> dict[str, Any]:
    """Convenience for building a text content block."""
    return {"type": "text", "text": text}
