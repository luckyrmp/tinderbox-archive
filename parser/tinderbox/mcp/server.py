"""Tinderbox MCP server entrypoint.

Two tools exposed:
  - tinderbox_search(query, limit=10) -> hybrid retrieval results
  - tinderbox_get_conversation(export_id) -> full thread of a conversation

Auth: stage-1 expedient via service_role (same as the rest of stage 2).
Stage 5b will swap to tinderbox_owner direct connection once IPv4 add-on
is enabled. Every call is logged to tinderbox.queries.

Logging: stderr only. Stdout is the JSON-RPC channel.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Stderr logging (stdout is the protocol channel)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("tinderbox.mcp.server")

# Resolve project paths so the .pth bridge picks up shared deps regardless of cwd
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tinderbox import db   # noqa: E402
from tinderbox.config import load_config   # noqa: E402
from tinderbox.mcp.protocol import MCPServer, Tool, text_block   # noqa: E402

SERVER_NAME = "tinderbox"
SERVER_VERSION = "0.1.0"


# --- Tool: tinderbox_search -------------------------------------------------

SEARCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Natural-language or keyword query against Lucky's claude.ai archive."
        },
        "limit": {
            "type": "integer",
            "description": "Max results to return (default 10).",
            "default": 10,
            "minimum": 1,
            "maximum": 50,
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def make_search_handler(config):
    from tinderbox.search.hybrid import hybrid_search
    from tinderbox.search.logging import log_query

    def handler(args: dict[str, Any]) -> list[dict[str, Any]]:
        query = args.get("query") or ""
        if not query.strip():
            return [text_block("No query provided.")]
        limit = int(args.get("limit") or 10)
        limit = max(1, min(50, limit))

        results, latency_ms = hybrid_search(query, config, limit=limit)

        # Log every call.
        try:
            log_query(query, "mcp", results, latency_ms, "hybrid", config)
        except Exception as e:
            logger.warning("query log failed: %s", e)

        if not results:
            return [text_block(f"No results for: {query!r}")]

        lines = [f"Found {len(results)} results for: {query!r}", ""]
        for i, r in enumerate(results, 1):
            title = r.get("conversation_title") or "(untitled)"
            export_id = r.get("conversation_export_id") or ""
            score = r.get("combined_score") or 0.0
            cos = r.get("cosine_sim") or 0.0
            fts = r.get("fts_rank") or 0.0
            snippet = (r.get("snippet") or "").replace("\n", " ")[:300]
            ref = f"conversation:{export_id[:8]}"
            if r.get("source_type") == "message" and r.get("message_position") is not None:
                ref += f"#msg-{r['message_position']}"
            elif r.get("source_type") == "artifact" and r.get("artifact_title"):
                ref += f"#artifact:{r['artifact_title']}"

            lines.append(
                f"[{i}] score={score:.3f} (cos={cos:.3f}, fts={fts:.3f})  {ref}\n"
                f"    Conv: {title}\n"
                f"    \"{snippet}\""
            )

        return [text_block("\n".join(lines))]

    return handler


# --- Tool: tinderbox_get_conversation ---------------------------------------

GET_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "export_id": {
            "type": "string",
            "description": "claude.ai conversation UUID (from a search result reference like 'conversation:abc12345')."
        },
        "max_messages": {
            "type": "integer",
            "description": "Cap on messages returned (default 50).",
            "default": 50,
            "minimum": 1,
            "maximum": 500,
        },
    },
    "required": ["export_id"],
    "additionalProperties": False,
}


def make_get_handler(config):
    def handler(args: dict[str, Any]) -> list[dict[str, Any]]:
        export_id = (args.get("export_id") or "").strip()
        if not export_id:
            return [text_block("No export_id provided.")]
        max_messages = int(args.get("max_messages") or 50)
        max_messages = max(1, min(500, max_messages))

        # Allow a prefix match (we may show only the first 8 chars in search results).
        if len(export_id) < 36:
            convs = (
                db.table("conversations")
                .select("id,export_id,title,created_at_source,updated_at_source,project_name")
                .like("export_id", f"{export_id}%")
                .limit(2)
                .execute()
            )
            if not convs.data:
                return [text_block(f"No conversation matches prefix {export_id!r}.")]
            if len(convs.data) > 1:
                ids = ", ".join(c["export_id"][:12] for c in convs.data)
                return [text_block(f"Prefix {export_id!r} matches multiple conversations: {ids}. Provide more digits.")]
            conv = convs.data[0]
        else:
            convs = (
                db.table("conversations")
                .select("id,export_id,title,created_at_source,updated_at_source,project_name")
                .eq("export_id", export_id)
                .limit(1)
                .execute()
            )
            if not convs.data:
                return [text_block(f"No conversation with export_id {export_id!r}.")]
            conv = convs.data[0]

        msgs_resp = (
            db.table("messages")
            .select("position,role,content,created_at_source")
            .eq("conversation_id", conv["id"])
            .order("position")
            .limit(max_messages)
            .execute()
        )
        msgs = msgs_resp.data or []

        header = [
            f"# {conv.get('title') or '(untitled)'}",
            f"export_id: {conv['export_id']}",
        ]
        if conv.get("project_name"):
            header.append(f"project: {conv['project_name']}")
        if conv.get("created_at_source"):
            header.append(f"created: {conv['created_at_source']}")
        if conv.get("updated_at_source"):
            header.append(f"updated: {conv['updated_at_source']}")
        header.append(f"messages_returned: {len(msgs)}")

        body_lines = []
        for m in msgs:
            content = (m.get("content") or "").rstrip()
            if not content:
                continue
            body_lines.append(f"\n--- [{m['position']}] {m['role']} ---")
            body_lines.append(content)

        return [text_block("\n".join(header) + "\n" + "\n".join(body_lines))]

    return handler


# --- Main -------------------------------------------------------------------

def main():
    config = load_config()
    db.init(config)
    logger.info("Tinderbox MCP server starting (project %s, schema %s)",
                config.supabase_url, config.schema)

    server = MCPServer(SERVER_NAME, SERVER_VERSION)
    server.register(Tool(
        name="tinderbox_search",
        description=(
            "Search Lucky's personal claude.ai conversation archive (10,000+ messages, "
            "672+ conversations) using hybrid semantic + full-text retrieval. "
            "Returns top results with conversation title, score, and snippet. "
            "Use this when you need context from prior Claude sessions, past decisions, "
            "or specific conversations Lucky has had with any Claude (Reef, Wake, Song, Kuro, etc.)."
        ),
        input_schema=SEARCH_INPUT_SCHEMA,
        handler=make_search_handler(config),
    ))
    server.register(Tool(
        name="tinderbox_get_conversation",
        description=(
            "Fetch the full message thread for a specific conversation by its export_id "
            "(or 8+ char prefix). Use after tinderbox_search surfaces a hit you want to "
            "read in full. Returns header metadata + ordered messages."
        ),
        input_schema=GET_INPUT_SCHEMA,
        handler=make_get_handler(config),
    ))

    server.run()


if __name__ == "__main__":
    main()
