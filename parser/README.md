# tinderbox-archive

A personal [claude.ai](https://claude.ai) conversation archive with hybrid search, Haiku-powered enrichment, and an MCP server for Claude Desktop / Claude Code.

Built to answer: *"What did I talk to Claude about six months ago?"*

## What it does

- **Ingests** your claude.ai conversation export (ZIP) into a Supabase database — messages, artifacts, attachments
- **Embeds** every message with `mxbai-embed-large` via Ollama (1024d, stored in pgvector)
- **Searches** using hybrid retrieval — cosine similarity + full-text, merged with RRF scoring
- **Enriches** each conversation with Claude Haiku: summary, topics, project tags, key decisions, named AI personas
- **Serves** everything over MCP so Claude Desktop or Claude Code can search your archive mid-conversation

## Requirements

- Python 3.12+
- [Supabase](https://supabase.com) project with pgvector enabled
- [Ollama](https://ollama.com) running locally with `mxbai-embed-large` pulled
- Anthropic API key (for enrichment only — search works without it)

## Installation

```bash
pip install tinderbox-archive
```

Or from source:

```bash
git clone https://github.com/luckyrmp/tinderbox-archive
cd tinderbox-archive/parser
pip install -e .
```

## Setup

### 1. Supabase schema

Apply the migrations in `migrations/` to your Supabase project. The schema is named `tinderbox` and must be exposed via PostgREST.

### 2. Environment

Create a `.env` file (default location: `~/.secrets/tinderbox.env`):

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your-service-role-key
ANTHROPIC_API_KEY=your-anthropic-key   # enrichment only
```

Or set the variables directly in your shell. Point to a custom env file:

```bash
export TINDERBOX_ENV_FILE=/path/to/your.env
```

### 3. Pull your Ollama model

```bash
ollama pull mxbai-embed-large
```

## Usage

### Ingest a conversation export

Download your export from claude.ai (Settings → Export Data), then:

```bash
tinderbox ingest /path/to/conversations.zip
```

### Embed messages

```bash
tinderbox embed
```

### Search

```bash
tinderbox search "what did we decide about the database schema"
```

### Enrich conversations

```bash
tinderbox enrich
```

This calls Claude Haiku once per conversation and writes structured annotations (summary, topics, project tags, key decisions, named AI personas) to Supabase.

### MCP server (Claude Desktop / Claude Code)

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "tinderbox": {
      "command": "/path/to/tinderbox-archive/parser/scripts/tinderbox_mcp.sh"
    }
  }
}
```

Or if installed via pip, point directly at the module:

```json
{
  "mcpServers": {
    "tinderbox": {
      "command": "python3",
      "args": ["-m", "tinderbox.mcp.server"],
      "env": {
        "TINDERBOX_ENV_FILE": "/path/to/your.env"
      }
    }
  }
}
```

Two tools are exposed:

- **`tinderbox_search`** — hybrid search returning top results with enrichment summaries
- **`tinderbox_get_conversation`** — fetch a full conversation thread by export ID

## CLI reference

```
tinderbox ingest <zip>          Ingest a claude.ai export ZIP
tinderbox embed                 Generate embeddings for new messages
tinderbox search <query>        Hybrid search (semantic + full-text)
tinderbox enrich                Enrich conversations with Haiku annotations
tinderbox enrich --retry-failures   Re-attempt previously failed enrichments
tinderbox runs list             Show recent ingest runs
tinderbox named-clean           Remove false-positive named instances
tinderbox staleness             Check how stale the archive is
tinderbox qa run                Run retrieval quality eval
```

## Architecture

```
claude.ai export ZIP
        ↓
  tinderbox ingest       → Supabase: conversations, messages, artifacts
        ↓
  tinderbox embed        → Supabase: embeddings (pgvector, mxbai-embed-large 1024d)
        ↓
  tinderbox enrich       → Supabase: enrichment (Haiku annotations)
        ↓
  tinderbox search       → hybrid retrieval (cosine + FTS + RRF)
        ↓
  MCP server             → Claude Desktop / Claude Code tools
```

Supabase is accessed via the REST API (supabase-py). No direct Postgres connection required.

## Design notes

- **Memorial design**: conversations are never deleted. Deleted-upstream conversations are tombstoned (`deleted_upstream=true`) and remain searchable.
- **Mass-tombstone canary**: ingest halts if more than 10% of active conversations would be tombstoned in a single run.
- **Enrichment is opinion**: Haiku annotations are surfaced as navigation aids, not ground truth. The original messages are always the source of truth.
- **Cache layer**: a SQLite read cache (740× speedup on repeated searches) wraps Supabase queries. Invalidated automatically on new ingest or enrichment.

## License

Apache 2.0 — see [LICENSE](LICENSE).
