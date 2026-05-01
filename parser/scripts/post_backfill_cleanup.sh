#!/bin/bash
# Post-enrichment-backfill cleanup. Run AFTER `tinderbox enrich` completes.
#
# Two passes:
#   1. Remove false-positive named_instances (humans/projects/products mistagged as AI personas)
#   2. Re-attempt failed enrichments with larger max_tokens
#
# Both are idempotent — safe to re-run.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

export PYTHONPATH="$REPO_ROOT"

echo "=== Pass 1: Cleaning false-positive named_instances ==="
python3 -m tinderbox.cli named-clean \
  --remove "Lucky" \
  --remove "Sonnet" \
  --remove "Joshua" \
  --remove "SecureIT" \
  --remove "Todyl"

echo
echo "=== Pass 2: Retrying failed enrichments ==="
echo "(Inspect failures first; manual retry strategy depends on the error class.)"

# Show what failed
python3 -c "
import sys; sys.path.insert(0, '$REPO_ROOT')
from tinderbox.config import load_config
from tinderbox import db
db.init(load_config())

result = db.table('conversations').select('id,export_id,title,deleted_upstream').eq('deleted_upstream', False).execute()
all_ids = {c['id']: c for c in (result.data or [])}

enr = db.table('enrichment').select('conversation_id,failure_reason').execute()
enriched_ids = {r['conversation_id'] for r in (enr.data or []) if r.get('failure_reason') is None}

missing = [c for cid, c in all_ids.items() if cid not in enriched_ids]
print(f'Conversations without successful enrichment: {len(missing)}')
for m in missing[:20]:
    print(f\"  {m['export_id'][:8]}  {m['title']}\")"
