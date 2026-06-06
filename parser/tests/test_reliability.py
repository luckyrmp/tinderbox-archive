"""Tinderbox reliability test harness.

Tests embed, search, and MCP interface for 90%+ pass rate.

Run: TINDERBOX_ENV_FILE=~/.secrets/shared.env PYTHONPATH=. python3.14 tests/test_reliability.py

Exit code 0 = all required tests pass (>=90%).
Exit code 1 = below threshold or critical failure.
"""

import io
import json
import os
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = os.environ.get("TINDERBOX_ENV_FILE",
                          str(Path.home() / ".secrets" / "shared.env"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Suite:
    name: str
    results: list[TestResult] = field(default_factory=list)

    def ok(self, name: str, detail: str = "") -> None:
        self.results.append(TestResult(name, True, detail))

    def fail(self, name: str, detail: str = "") -> None:
        self.results.append(TestResult(name, False, detail))

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.ok(name, detail)
        else:
            self.fail(name, detail)
        return condition

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def rate(self) -> float:
        return self.pass_count / self.total if self.total else 0.0


# ---------------------------------------------------------------------------
# Suite 1: Embed worker reliability
# ---------------------------------------------------------------------------

def run_embed_suite() -> Suite:
    suite = Suite("embed")
    sys.path.insert(0, str(REPO_ROOT))

    try:
        from tinderbox import db
        from tinderbox.config import load_config
        from tinderbox.embed.worker import (
            _prefetch_embedded_chunks, chunk_message,
            _existing_chunk_indices, EmbedCounters,
        )

        cfg = load_config()
        db.init(cfg)

        # Test 1: prefetch returns a non-empty set
        embedded = _prefetch_embedded_chunks("message", cfg.embedding_model)
        suite.check(
            "prefetch_returns_data",
            len(embedded) > 0,
            f"got {len(embedded)} embedded chunks",
        )

        # Test 2: prefetch is consistent with per-row query for a sample
        sample = list(embedded)[:5]
        consistent = True
        for src_id, chunk_idx in sample:
            per_row = _existing_chunk_indices("message", src_id, cfg.embedding_model)
            if chunk_idx not in per_row:
                consistent = False
                break
        suite.check("prefetch_consistent_with_per_row", consistent,
                    "sampled 5 entries from bulk set against per-row query")

        # Test 3: chunk_message produces consistent chunks
        test_texts = [
            "how many people on earth",
            "The AdventurerPlanner Business Plan is a SaaS product for trip planning.",
            "x" * 600,  # long text should still chunk
        ]
        all_chunked = True
        for txt in test_texts:
            chunks = chunk_message(txt)
            if not chunks:
                all_chunked = False
        suite.check("chunk_message_never_empty", all_chunked,
                    f"tested {len(test_texts)} texts")

        # Test 4: chunk_message is idempotent
        txt = "some adventure planning content about trips"
        c1 = chunk_message(txt)
        c2 = chunk_message(txt)
        suite.check("chunk_message_idempotent",
                    [c.chunk_text for c in c1] == [c.chunk_text for c in c2])

        # Test 5: AdventurerPlanner messages now have embeddings
        result = db.table("messages").select("id").execute()
        all_msg_ids = {r["id"] for r in (result.data or [])}
        ap_res = (db.get_client()
                  .schema("tinderbox").table("conversations")
                  .select("id").ilike("title", "%adventur%plann%").execute())
        ap_conv_ids = {r["id"] for r in (ap_res.data or [])}

        if ap_conv_ids:
            ap_msg_res = (db.get_client()
                          .schema("tinderbox").table("messages")
                          .select("id")
                          .in_("conversation_id", list(ap_conv_ids))
                          .execute())
            ap_msg_ids = {r["id"] for r in (ap_msg_res.data or [])}
            embedded_ap = sum(1 for mid in ap_msg_ids
                              if any(mid == sid for (sid, _) in embedded))
            suite.check(
                "adventureplanner_messages_embedded",
                embedded_ap > 0,
                f"{embedded_ap}/{len(ap_msg_ids)} AP messages have embeddings",
            )
        else:
            suite.fail("adventureplanner_messages_embedded",
                       "AdventurerPlanner conversation not found in DB")

        # Test 6: embed CLI runs without crashing (limit=1 smoke test)
        env = {**os.environ, "TINDERBOX_ENV_FILE": ENV_FILE,
               "PYTHONPATH": str(REPO_ROOT)}
        proc = subprocess.run(
            [sys.executable, "-m", "tinderbox.cli", "embed", "--limit", "1"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, timeout=60,
        )
        suite.check("embed_cli_no_crash", proc.returncode == 0,
                    f"exit={proc.returncode} stderr={proc.stderr.decode()[-200:]}")

    except Exception as e:
        suite.fail("embed_suite_setup", str(e))

    return suite


# ---------------------------------------------------------------------------
# Suite 2: Search reliability
# ---------------------------------------------------------------------------

def run_search_suite() -> Suite:
    suite = Suite("search")

    try:
        from tinderbox import db
        from tinderbox.config import load_config
        from tinderbox.search.hybrid import hybrid_search

        cfg = load_config()
        db.init(cfg)

        # Test 1: basic search returns results
        results, ms = hybrid_search("business plan startup", cfg, limit=5)
        suite.check("search_returns_results", len(results) > 0,
                    f"got {len(results)} results in {ms}ms")

        # Test 2: results have required fields
        required_fields = {"cosine_sim", "combined_score", "chunk_text"}
        if results:
            has_fields = required_fields.issubset(results[0].keys())
            suite.check("results_have_required_fields", has_fields,
                        f"fields: {list(results[0].keys())}")

        # Test 3: cosine scores are in valid range [0, 1]
        if results:
            valid_range = all(0 <= r["cosine_sim"] <= 1.0 for r in results)
            suite.check("cosine_scores_valid_range", valid_range,
                        f"min={min(r['cosine_sim'] for r in results):.3f} "
                        f"max={max(r['cosine_sim'] for r in results):.3f}")

        # Test 4: results are ordered by combined_score descending
        if len(results) >= 2:
            ordered = all(
                results[i]["combined_score"] >= results[i+1]["combined_score"]
                for i in range(len(results) - 1)
            )
            suite.check("results_ordered_by_score", ordered)

        # Test 5: AdventurerPlanner search returns the right conversation
        ap_results, _ = hybrid_search("AdventurerPlanner business plan trip", cfg, limit=10)
        ap_hit = any(
            "adventur" in (r.get("conversation_title") or "").lower()
            or "adventur" in (r.get("chunk_text") or "").lower()
            for r in ap_results
        )
        suite.check("adventureplanner_search_finds_it", ap_hit,
                    f"top titles: {[r.get('conversation_title','?')[:30] for r in ap_results[:3]]}")

        # Test 6: FTS contributes for term-matching queries
        fts_results, _ = hybrid_search("tinderbox schema migration", cfg, limit=10)
        has_fts = any(r.get("fts_rank", 0) > 0 for r in fts_results)
        suite.check("fts_contributes_when_terms_match", has_fts,
                    f"fts scores: {[round(r.get('fts_rank',0),4) for r in fts_results[:5]]}")

        # Test 7: search with limit respected
        results_limited, _ = hybrid_search("project", cfg, limit=3)
        suite.check("limit_respected", len(results_limited) <= 3,
                    f"got {len(results_limited)} with limit=3")

        # Test 8: empty query handled gracefully
        try:
            _, _ = hybrid_search("", cfg, limit=5)
            suite.ok("empty_query_no_crash")
        except Exception as e:
            suite.fail("empty_query_no_crash", str(e)[:100])

        # Test 9: search latency under 10s (Ollama + Supabase round trip)
        _, ms2 = hybrid_search("software engineering", cfg, limit=5)
        suite.check("search_latency_under_10s", ms2 < 10_000,
                    f"latency={ms2}ms")

        # Test 10: caching works (second call faster)
        query = "machine learning python"
        _, ms_first = hybrid_search(query, cfg, limit=5, use_cache=False)
        _, ms_cached = hybrid_search(query, cfg, limit=5, use_cache=True)
        suite.check("cache_reduces_latency", ms_cached < ms_first,
                    f"first={ms_first}ms cached={ms_cached}ms")

    except Exception as e:
        import traceback
        suite.fail("search_suite_setup", traceback.format_exc()[-300:])

    return suite


# ---------------------------------------------------------------------------
# Suite 3: MCP server reliability
# ---------------------------------------------------------------------------

SERVER_CMD = [sys.executable, "-m", "tinderbox.mcp.server"]


def _mcp_send(proc, msg: dict) -> None:
    proc.stdin.write(json.dumps(msg).encode() + b"\n")
    proc.stdin.flush()


def _mcp_recv(proc, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line.strip():
            return json.loads(line.decode().strip())
    raise TimeoutError("no response from MCP server")


def _spawn_mcp() -> subprocess.Popen:
    env = {**os.environ, "TINDERBOX_ENV_FILE": ENV_FILE,
           "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.Popen(
        SERVER_CMD, cwd=str(REPO_ROOT), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return proc


def _mcp_initialize(proc) -> bool:
    _mcp_send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "reliability-test", "version": "1.0"},
        },
    })
    resp = _mcp_recv(proc, timeout=15)
    _mcp_send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return "serverInfo" in resp.get("result", {})


def run_mcp_suite() -> Suite:
    suite = Suite("mcp")
    proc = None

    try:
        proc = _spawn_mcp()
        time.sleep(1)  # let it start

        # Test 1: server starts and responds to initialize
        init_ok = _mcp_initialize(proc)
        suite.check("mcp_initialize", init_ok)
        if not init_ok:
            suite.fail("mcp_all_skipped", "server failed to initialize")
            return suite

        # Test 2: tools/list returns expected tools
        _mcp_send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        resp = _mcp_recv(proc)
        tools = {t["name"] for t in resp.get("result", {}).get("tools", [])}
        suite.check("mcp_has_search_tool", "tinderbox_search" in tools,
                    f"tools: {tools}")
        suite.check("mcp_has_get_conversation_tool",
                    "tinderbox_get_conversation" in tools, f"tools: {tools}")

        # Test 3: tinderbox_search returns results
        _mcp_send(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "tinderbox_search",
                "arguments": {"query": "business plan startup", "limit": 3},
            },
        })
        resp = _mcp_recv(proc, timeout=30)
        content = resp.get("result", {}).get("content", [])
        suite.check("mcp_search_returns_content", bool(content),
                    f"content blocks: {len(content)}")
        if content:
            text = content[0].get("text", "")
            suite.check("mcp_search_result_has_text", len(text) > 10,
                        f"len={len(text)}")

        # Test 4: search result text contains expected fields
        if content:
            text = content[0].get("text", "")
            has_score = "score=" in text or "score" in text.lower()
            suite.check("mcp_search_result_has_score", has_score,
                        f"text[:100]={text[:100]}")

        # Test 5: AdventurerPlanner search via MCP
        _mcp_send(proc, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "tinderbox_search",
                "arguments": {"query": "AdventurerPlanner business plan trip", "limit": 5},
            },
        })
        resp = _mcp_recv(proc, timeout=30)
        text = "".join(
            b.get("text", "") for b in resp.get("result", {}).get("content", [])
        )
        suite.check("mcp_finds_adventureplanner",
                    "adventur" in text.lower() or "planner" in text.lower(),
                    f"response[:200]={text[:200]}")

        # Test 6: get_conversation with known export_id
        from tinderbox import db
        from tinderbox.config import load_config
        cfg = load_config()
        db.init(cfg)
        conv_res = (db.get_client().schema("tinderbox").table("conversations")
                    .select("export_id").ilike("title", "%adventur%plann%")
                    .limit(1).execute())
        if conv_res.data:
            export_id = conv_res.data[0]["export_id"]
            _mcp_send(proc, {
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {
                    "name": "tinderbox_get_conversation",
                    "arguments": {"export_id": export_id, "max_messages": 5},
                },
            })
            resp = _mcp_recv(proc, timeout=20)
            content = resp.get("result", {}).get("content", [])
            text = "".join(b.get("text", "") for b in content)
            suite.check("mcp_get_conversation_returns_messages",
                        len(text) > 20,
                        f"export_id={export_id} text_len={len(text)}")
        else:
            suite.fail("mcp_get_conversation_returns_messages",
                       "AdventurerPlanner conversation not in DB")

        # Test 7: bad tool name returns error gracefully
        _mcp_send(proc, {
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        })
        resp = _mcp_recv(proc)
        is_err = ("error" in resp or
                  resp.get("result", {}).get("isError") or
                  resp.get("result", {}).get("content", [{}])[0].get("type") == "error")
        suite.check("mcp_bad_tool_returns_error", is_err)

        # Test 8: missing required param returns error
        _mcp_send(proc, {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "tinderbox_search", "arguments": {}},
        })
        resp = _mcp_recv(proc, timeout=15)
        is_err = ("error" in resp or
                  resp.get("result", {}).get("isError") or
                  resp.get("result", {}).get("content", [{}])[0].get("type") == "error")
        suite.check("mcp_missing_param_returns_error", is_err)

        # Test 9: server handles multiple concurrent-ish requests correctly
        for i in range(3):
            _mcp_send(proc, {
                "jsonrpc": "2.0", "id": 10 + i, "method": "tools/call",
                "params": {"name": "tinderbox_search",
                           "arguments": {"query": f"test query {i}", "limit": 2}},
            })
        responses = []
        for _ in range(3):
            try:
                responses.append(_mcp_recv(proc, timeout=30))
            except TimeoutError:
                break
        suite.check("mcp_handles_sequential_requests", len(responses) == 3,
                    f"got {len(responses)}/3 responses")

    except Exception as e:
        import traceback
        suite.fail("mcp_suite_error", traceback.format_exc()[-400:])
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return suite


# ---------------------------------------------------------------------------
# Suite 4: Ingest reliability (nested ZIP handling)
# ---------------------------------------------------------------------------

def run_ingest_suite() -> Suite:
    suite = Suite("ingest")

    try:
        from tinderbox.parser.streaming import stream_conversations, list_zip_contents

        # Build a minimal flat ZIP
        flat_buf = io.BytesIO()
        convos = [{"uuid": "abc", "name": "Test", "created_at": "2026-01-01",
                   "updated_at": "2026-01-01", "chat_messages": []}]
        with zipfile.ZipFile(flat_buf, "w") as zf:
            zf.writestr("conversations.json", json.dumps(convos))
            zf.writestr("users.json", "[]")
        flat_bytes = flat_buf.getvalue()

        # Build macOS-nested ZIP (conversations.json inside a subdirectory)
        nested_buf = io.BytesIO()
        with zipfile.ZipFile(nested_buf, "w") as zf:
            zf.writestr("data-abc123/conversations.json", json.dumps(convos))
            zf.writestr("data-abc123/users.json", "[]")
            zf.writestr("__MACOSX/._data-abc123", "")
        nested_bytes = nested_buf.getvalue()

        import tempfile

        # Test 1: flat ZIP streams conversations correctly
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(flat_bytes)
            flat_path = f.name
        try:
            convs = list(stream_conversations(flat_path))
            suite.check("flat_zip_streams", len(convs) == 1,
                        f"got {len(convs)} convs")
            contents = list_zip_contents(flat_path)
            suite.check("flat_zip_list_contents",
                        "conversations.json" in contents,
                        f"contents={contents}")
        finally:
            os.unlink(flat_path)

        # Test 2: macOS-nested ZIP streams conversations correctly
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(nested_bytes)
            nested_path = f.name
        try:
            convs = list(stream_conversations(nested_path))
            suite.check("nested_zip_streams", len(convs) == 1,
                        f"got {len(convs)} convs")
            contents = list_zip_contents(nested_path)
            suite.check("nested_zip_list_contents",
                        "conversations.json" in contents,
                        f"contents={contents}")
        finally:
            os.unlink(nested_path)

        # Test 3: ZIP missing conversations.json raises KeyError
        bad_buf = io.BytesIO()
        with zipfile.ZipFile(bad_buf, "w") as zf:
            zf.writestr("users.json", "[]")
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(bad_buf.getvalue())
            bad_path = f.name
        try:
            raised = False
            try:
                list(stream_conversations(bad_path))
            except KeyError:
                raised = True
            suite.check("missing_conversations_json_raises", raised)
        finally:
            os.unlink(bad_path)

    except Exception as e:
        import traceback
        suite.fail("ingest_suite_error", traceback.format_exc()[-300:])

    return suite


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

PASS_THRESHOLD = 0.90


def main() -> int:
    print("=" * 60)
    print("Tinderbox Reliability Test Harness")
    print("=" * 60)

    suites = [
        run_embed_suite,
        run_search_suite,
        run_mcp_suite,
        run_ingest_suite,
    ]

    all_results: list[TestResult] = []

    for suite_fn in suites:
        print(f"\n--- {suite_fn.__name__.replace('run_', '').replace('_suite', '').upper()} SUITE ---")
        suite = suite_fn()
        for r in suite.results:
            status = "PASS" if r.passed else "FAIL"
            detail = f"  [{r.detail}]" if r.detail else ""
            print(f"  [{status}] {r.name}{detail}")
        print(f"  {suite.pass_count}/{suite.total} passed ({suite.rate:.0%})")
        all_results.extend(suite.results)

    total = len(all_results)
    passed = sum(1 for r in all_results if r.passed)
    rate = passed / total if total else 0.0

    print(f"\n{'=' * 60}")
    print(f"OVERALL: {passed}/{total} passed ({rate:.0%})")
    print(f"THRESHOLD: {PASS_THRESHOLD:.0%}")

    if rate >= PASS_THRESHOLD:
        print("✓ PASS — reliability target met.")
        return 0
    else:
        print("✗ FAIL — below reliability target.")
        failed = [r for r in all_results if not r.passed]
        print(f"  Failed tests ({len(failed)}):")
        for r in failed:
            print(f"    - {r.name}: {r.detail[:80]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
