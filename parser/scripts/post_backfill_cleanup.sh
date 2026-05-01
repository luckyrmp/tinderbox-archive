#!/bin/bash
# Post-enrichment-backfill cleanup. Run AFTER `tinderbox enrich` completes.
#
# Two passes:
#   1. Remove false-positive named_instances (humans/projects/products mistagged as AI personas)
#   2. Re-attempt failed enrichments with larger max_tokens
#
# Both are idempotent — safe to re-run.

set -e

cd /Users/song/tinderbox/parser
export PYTHONPATH=/Users/song/tinderbox/parser
PY=/Users/song/tinderbox/parser/venv/bin/python3.14

echo "=== Pass 1: Cleaning false-positive named_instances ==="
$PY -m tinderbox.cli named-clean \
  --remove "Lucky" \
  --remove "Sonnet" \
  --remove "Joshua" \
  --remove "SecureIT" \
  --remove "Todyl"

echo
echo "=== Pass 2: Retrying failed enrichments ==="
echo "(Inspect failures first; manual retry strategy depends on the error class.)"

# Show what failed
$PY -c "
import sys; sys.path.insert(0, '/Users/song/tinderbox/parser')
from tinderbox.config import load_config
from tinderbox import db
db.init(load_config())

# Conversations seen by enrichment runner that have NO enrichment row
result = db.table('conversations').select('id,export_id,title,deleted_upstream').eq('deleted_upstream', False).execute()
all_ids = {c['id']: c for c in (result.data or [])}

enr = db.table('enrichment').select('conversation_id').execute()
enriched_ids = {r['conversation_id'] for r in (enr.data or [])}

missing = [c for cid, c in all_ids.items() if cid not in enriched_ids]
print(f'Conversations without enrichment: {len(missing)}')
for m in missing[:20]:
    print(f\"  {m['export_id'][:8]}  {m['title']}\")"
