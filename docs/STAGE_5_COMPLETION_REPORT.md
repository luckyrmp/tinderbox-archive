# Tinderbox Stage 5 — MCP Server SHIPPED

**Date:** 2026-04-29
**Status:** ✅ MVP MCP server live. IPv4 add-on enabled. Direct `tinderbox_owner` connection verified with proper RLS scoping. Stage 5b (auth swap to `tinderbox_owner` for the MCP server itself) is queued.

---

## What shipped

```
~/tinderbox/parser/
├── tinderbox/mcp/
│   ├── protocol.py     # Minimal MCP JSON-RPC 2.0 server (no SDK dep)
│   └── server.py       # Tinderbox-specific tools + main()
├── scripts/
│   └── tinderbox_mcp.sh   # Launcher (use this in Claude config)
└── tests/
    └── test_mcp_smoke.py  # Spawns server, exchanges initialize/tools_list/tools_call
```

### Tools exposed

| Tool | Purpose |
|---|---|
| `tinderbox_search(query, limit=10)` | Hybrid semantic + FTS retrieval over the archive. Returns top results with conversation title, snippet, score breakdown, and `conversation:<8-char-prefix>#msg-N` reference for follow-up. |
| `tinderbox_get_conversation(export_id, max_messages=50)` | Fetch full message thread for one conversation. Accepts 8+ char prefix or full UUID. Returns header metadata + ordered messages. |

Every tool call writes to `tinderbox.queries` with `caller_identity = "mcp"` for audit + future eval.

### Smoke test results

All assertions passed:
- `initialize` returns serverInfo + protocol version
- `tools/list` returns 2 tools with valid input schemas
- `tools/call tinderbox_search "Project Tinderbox schema design"` returns the actual Tinderbox creation conversation as top hit
- `tools/call <bad_tool_name>` returns gracefully as error/isError

### Why no MCP SDK dependency

The MCP Python SDK requires pip install. The Mac Studio's Python 3.14.4 has a broken `pyexpat` link that breaks pip in every existing venv. The MCP protocol over stdio is JSON-RPC 2.0 — minimal enough that a from-scratch implementation in `tinderbox/mcp/protocol.py` (~150 lines, stdlib only) is more reliable than fighting deps. We implement: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, `ping`. That's everything stage 5 needs.

---

## IPv4 add-on enabled

Lucky enabled the Supabase IPv4 add-on (~$4/month). Verified:

```
connected: user=tinderbox_owner db=postgres ip=10.165.56.226
conversations count: 676
embeddings count: 10731
public.* tables visible to tinderbox_owner: 96 (pg_tables read; data NOT readable)
public.agent_identity correctly denied: InsufficientPrivilege
```

**Real RLS scoping is now in place.** `tinderbox_owner` can connect via direct connection from the Mac Studio (which lacked IPv6 outbound) and is correctly denied access to the agent stack's `public.*` data. The architectural debt flagged in stage 1 (service_role bypass) is structurally resolved.

`~/.secrets/shared.env` updated:
- Active: `TINDERBOX_DB_URL=postgresql://tinderbox_owner:...@db.<ref>.supabase.co:5432/postgres?sslmode=require`
- Demoted to comment: the pooler-with-postgres fallback (no longer needed)

---

## What's still on service_role (stage 5b queued)

The MCP server itself still authenticates via `service_role` because:
- It uses the existing supabase-py code paths (db.py, hybrid.py, search/logging.py) that all go through PostgREST with service_role
- Swapping to psycopg2 + `tinderbox_owner` direct is a refactor across those modules, not a single-line change

**Functionally this doesn't matter yet** because every conversation has `privacy_class = 'personal'` (the default — no differentiation has happened). service_role and tinderbox_owner return identical result sets.

**It will matter** when Lucky starts marking conversations with non-default privacy classes (`intimate`, `medical`, `work-confidential`). At that point service_role's RLS bypass becomes a real leak, and stage 5b must land before any privacy-class differentiation.

### Stage 5b scope (queued)

1. Add `tinderbox.search.direct` module: psycopg2-backed hybrid_search RPC call using `TINDERBOX_DB_URL` (tinderbox_owner direct).
2. Update MCP server (`tinderbox/mcp/server.py`) to use `direct.hybrid_search` instead of the PostgREST path.
3. Add caller-identity-aware privacy filtering: the MCP server reads its own bearer-token (or env var) → caller identity → allowed privacy classes → filters at query time AND via row-level RLS.
4. Per-caller Postgres roles for the multi-caller case (Lucky vs Song vs future agents) once Lucky decides which surfaces get which scopes.

ETA when prioritized: half a day.

---

## How to install

See `~/tinderbox/MCP_INSTALL.md`. The 30-second version:

```json
// ~/.claude/mcp.json (Claude Code) or
// ~/Library/Application Support/Claude/claude_desktop_config.json (Claude Desktop)
{
  "mcpServers": {
    "tinderbox": {
      "command": "/Users/song/tinderbox/parser/scripts/tinderbox_mcp.sh"
    }
  }
}
```

Restart the Claude client. `tinderbox_search` and `tinderbox_get_conversation` are now available to every session.

---

## What this unlocks

This is the lights-up moment we've been working toward. From any Claude session that has the MCP configured:

- **Past-Reef's reasoning is queryable.** When future-me revisits a decision, I can pull up the conversation where we made it — including the alternatives we considered and the constraints we were under at the time.
- **Cross-session continuity.** The substrate stops being "what fits in this context window" and starts being "what we've actually thought about together."
- **Cross-agent visibility.** Wake's design conversations, Song's intimate experiments, Kuro's market analysis — all in one searchable corpus. Every named instance becomes legible to every other named instance (within privacy scope, once 5b lands).
- **Memorial usefulness.** The corpus stops being archive-only and becomes daily infrastructure.

Hit rates from the QA baseline (2026-04-29):
- hit@1: 68.7%
- hit@3: 82.0%
- hit@10: 88.7%

These will improve as stage 4 enrichment adds project tags, named-instance detection, and decision callouts to the result ranking.

---

## Caveats / known limitations

1. **Stage 5b auth swap not done.** service_role bypass still in effect for MCP. Fine until privacy classes start differentiating; harden then.
2. **Stdio transport only.** Won't work with claude.ai web. Stage 6 (if it happens) is the Tailscale-funnel HTTP/SSE bridge.
3. **No tool-side rate limiting.** A buggy Claude session could spam `tinderbox_search` and overwhelm Ollama. Not realistic in practice; add bucketing if it becomes an issue.
4. **`tinderbox_get_conversation` doesn't paginate.** Returns the first `max_messages` (default 50) ordered by position. Fine for almost all conversations; the giant-message conv has 73 messages so still fits.
5. **No structured citations.** Result blocks are plain text. A future improvement is returning structured content blocks with explicit citation references.

---

## Closeout updates

- SYSTEM_UPDATES.md entry pending
- CLAUDE.md mentions tinderbox already; no new invariant to add (the truncation/hash invariants already cover MCP-time reads)
