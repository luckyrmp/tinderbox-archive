"""Query logging — write every hybrid_search call to tinderbox.queries.

Stage 5 §3.5: this becomes the eval set after Lucky has been using the system
for a couple of weeks (option A from the handback).
"""

import logging
from typing import Any

from tinderbox import db
from tinderbox.config import Config

logger = logging.getLogger("tinderbox.search.logging")


def log_query(query_text: str, caller: str, results: list[dict[str, Any]],
              latency_ms: int, query_kind: str, config: Config) -> None:
    """Best-effort write to tinderbox.queries. Logging failures don't propagate."""
    try:
        result_conv_ids = list({r["conversation_id"] for r in results
                                if r.get("conversation_id")})
        # message_id surrogate: when source_type=message, source_id == message id
        result_msg_ids = list({r["source_id"] for r in results
                               if r.get("source_type") == "message"})

        db.table("queries").insert({
            "caller_identity": caller,
            "query_text": query_text,
            "query_kind": query_kind,
            "allowed_classes": ["personal"],   # stage 1 default; stage 5 wires real scoping
            "intimate_unlocked": False,
            "result_conversation_ids": result_conv_ids,
            "result_message_ids": result_msg_ids,
            "result_count": len(results),
            "latency_ms": latency_ms,
        }).execute()
    except Exception as e:
        logger.warning("Query logging failed (non-fatal): %s", e)
