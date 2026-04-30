"""Integration test for done-when #4: re-running a modified ZIP adds exactly 1 message.

Strategy: synthesize tiny ZIPs against the live tinderbox schema using a
test-specific conversation_export_id prefix. Cleans up before and after so
the test doesn't pollute the production archive.

Run: PYTHONPATH=. python3.14 tests/test_modified_zip.py
"""

import io
import json
import sys
import time
import unittest
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tinderbox.config import load_config
from tinderbox import db
from tinderbox.ingest.runner import run as ingest_run


TEST_PREFIX = "ztb_test_"


def _make_conv(uuid_str: str, name: str, messages: list[dict]) -> dict:
    """Build a conversation envelope matching claude.ai's export format."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "uuid": uuid_str,
        "name": name,
        "summary": "",
        "created_at": now,
        "updated_at": now,
        "account": {"uuid": "test-account"},
        "chat_messages": messages,
    }


def _make_msg(uuid_str: str, sender: str, text: str, position_ts: str) -> dict:
    return {
        "uuid": uuid_str,
        "text": text,
        "content": [{"type": "text", "text": text}],
        "sender": sender,
        "created_at": position_ts,
        "updated_at": position_ts,
        "attachments": [],
        "files": [],
        "parent_message_uuid": None,
    }


def _make_zip(convos: list[dict], path: Path) -> Path:
    """Write a minimal-but-valid Tinderbox export ZIP to `path`."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("conversations.json", json.dumps(convos))
        zf.writestr("users.json", json.dumps([{"uuid": "test-account", "email_address": "test@example.invalid"}]))
        zf.writestr("projects.json", json.dumps([]))
        zf.writestr("memories.json", json.dumps({}))
    return path


def _cleanup_test_data():
    """Delete all rows from tinderbox.* whose conversation export_id starts with TEST_PREFIX."""
    tc = db.table("conversations").select("id").like("export_id", f"{TEST_PREFIX}%").execute()
    ids = [r["id"] for r in (tc.data or [])]
    if ids:
        # Cascade through messages/artifacts/attachments via ON DELETE CASCADE
        for batch_start in range(0, len(ids), 100):
            batch = ids[batch_start:batch_start + 100]
            db.table("conversations").delete().in_("id", batch).execute()

    # Delete ingest_runs whose export_path contains the test sentinel
    db.table("ingest_runs").delete().like("export_path", f"%{TEST_PREFIX}%").execute()


class ModifiedZipTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        db.init(cls.config)
        _cleanup_test_data()

    @classmethod
    def tearDownClass(cls):
        _cleanup_test_data()

    def test_re_running_modified_zip_adds_exactly_one_message(self):
        # ===== Setup =====
        run_token = uuid.uuid4().hex[:8]
        conv_uuid = f"{TEST_PREFIX}{run_token}_conv"
        msg_a_uuid = f"{TEST_PREFIX}{run_token}_msg_a"
        msg_b_uuid = f"{TEST_PREFIX}{run_token}_msg_b"
        msg_new_uuid = f"{TEST_PREFIX}{run_token}_msg_new"

        # ===== Round 1: ingest 1 conv with 2 messages =====
        v1 = _make_conv(conv_uuid, "Test conversation", [
            _make_msg(msg_a_uuid, "human", "hello", "2026-04-29T10:00:00Z"),
            _make_msg(msg_b_uuid, "assistant", "hi", "2026-04-29T10:00:01Z"),
        ])
        zip_v1 = Path(f"/tmp/{TEST_PREFIX}{run_token}_v1.zip")
        _make_zip([v1], zip_v1)
        # canary_override=True: the test ZIP only contains the test conv,
        # which would otherwise trip the canary against the 676 prod convos
        # already in the schema. The canary itself is tested in test_canary.py.
        run1_id = ingest_run(str(zip_v1), self.config, canary_override=True)

        # Sanity: confirm round 1 inserted what we expect
        run1 = db.table("ingest_runs").select("messages_new").eq("id", run1_id).single().execute()
        self.assertEqual(run1.data["messages_new"], 2,
                         "Round 1 should insert exactly 2 messages")

        # ===== Round 2: same conv + 1 new message at position 2 =====
        v2 = _make_conv(conv_uuid, "Test conversation", [
            _make_msg(msg_a_uuid, "human", "hello", "2026-04-29T10:00:00Z"),
            _make_msg(msg_b_uuid, "assistant", "hi", "2026-04-29T10:00:01Z"),
            _make_msg(msg_new_uuid, "human", "follow-up", "2026-04-29T10:00:02Z"),
        ])
        zip_v2 = Path(f"/tmp/{TEST_PREFIX}{run_token}_v2.zip")
        _make_zip([v2], zip_v2)
        run2_id = ingest_run(str(zip_v2), self.config, canary_override=True)

        # The runner archives the ZIP after a successful run, so re-archive may not exist —
        # we look up the run by its returned id.
        run2 = db.table("ingest_runs").select("messages_new,conversations_new,conversations_updated") \
                                       .eq("id", run2_id).single().execute()

        # ===== Assertions =====
        self.assertEqual(run2.data["messages_new"], 1,
                         "Round 2 should insert EXACTLY 1 new message")
        self.assertEqual(run2.data["conversations_new"], 0,
                         "Conversation already exists; conversations_new should be 0")
        self.assertEqual(run2.data["conversations_updated"], 1,
                         "Conversation should be marked updated")

        # And the conversation now has 3 total messages
        conv_row = db.table("conversations").select("id").eq("export_id", conv_uuid).single().execute()
        msg_count_q = db.table("messages").select("id", count="exact") \
                                           .eq("conversation_id", conv_row.data["id"]).execute()
        self.assertEqual(msg_count_q.count, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
