"""Render an eval_runs row as a markdown report at ~/tinderbox/eval/latest.md."""

import json
import logging
from collections import defaultdict
from pathlib import Path

from tinderbox import db
from tinderbox.config import Config

logger = logging.getLogger("tinderbox.qa.report")


def _hit_rate(num: int, denom: int) -> float:
    return (num / denom * 100) if denom else 0.0


def render_report(run_id: str, output_path: Path) -> Path:
    run = (
        db.table("eval_runs")
        .select("*")
        .eq("id", run_id)
        .single()
        .execute()
    )
    r = run.data
    if not r:
        raise RuntimeError(f"eval_runs row not found: {run_id}")

    queries_run = r["queries_run"] or 0
    hit1 = _hit_rate(r["hit_at_1"], queries_run)
    hit3 = _hit_rate(r["hit_at_3"], queries_run)
    hit10 = _hit_rate(r["hit_at_10"], queries_run)

    per_query = r.get("per_query_results") or []

    by_flavor: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n": 0, "hit1": 0, "hit3": 0, "hit10": 0, "miss": 0}
    )
    for pq in per_query:
        flav = pq.get("flavor", "unknown")
        by_flavor[flav]["n"] += 1
        hp = pq.get("hit_position")
        if hp == 1:
            by_flavor[flav]["hit1"] += 1
        if hp and hp <= 3:
            by_flavor[flav]["hit3"] += 1
        if hp and hp <= 10:
            by_flavor[flav]["hit10"] += 1
        if hp is None:
            by_flavor[flav]["miss"] += 1

    misses = [pq for pq in per_query if pq.get("hit_position") is None]
    misses.sort(key=lambda x: x.get("flavor", ""))

    lines: list[str] = []
    lines.append(f"# Tinderbox Retrieval QA — {r['started_at']}")
    lines.append("")
    lines.append(f"**Run:** {run_id}")
    if r.get("completed_at"):
        lines.append(f"**Completed:** {r['completed_at']}")
    lines.append(f"**Queries run:** {queries_run}")
    lines.append(f"**Failures:** {r.get('notes', '')}")
    lines.append("")
    lines.append("## Aggregate hit rates")
    lines.append("")
    lines.append(f"- **hit@1:** {r['hit_at_1']}/{queries_run} = **{hit1:.1f}%**")
    lines.append(f"- **hit@3:** {r['hit_at_3']}/{queries_run} = **{hit3:.1f}%**")
    lines.append(f"- **hit@10:** {r['hit_at_10']}/{queries_run} = **{hit10:.1f}%**")
    lines.append("")
    lines.append("## Per-flavor breakdown")
    lines.append("")
    lines.append("| Flavor | n | hit@1 | hit@3 | hit@10 | miss |")
    lines.append("|---|---|---|---|---|---|")
    for flav in ("vague_semantic", "specific_factual", "fragmentary"):
        b = by_flavor.get(flav, {"n": 0, "hit1": 0, "hit3": 0, "hit10": 0, "miss": 0})
        n = b["n"] or 1
        lines.append(
            f"| {flav} | {b['n']} | "
            f"{b['hit1']} ({b['hit1']/n*100:.0f}%) | "
            f"{b['hit3']} ({b['hit3']/n*100:.0f}%) | "
            f"{b['hit10']} ({b['hit10']/n*100:.0f}%) | "
            f"{b['miss']} |"
        )
    lines.append("")
    lines.append(f"## Misses ({len(misses)})")
    lines.append("")
    if not misses:
        lines.append("(no misses)")
    else:
        for pq in misses[:30]:
            qt = (pq.get("query_text") or "").replace("\n", " ").strip()
            lines.append(f"- _[{pq.get('flavor')}]_ {qt}")
        if len(misses) > 30:
            lines.append(f"- … and {len(misses) - 30} more")
    lines.append("")
    lines.append("## Config snapshot")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(r.get("config_snapshot", {}), indent=2))
    lines.append("```")
    lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines))
    logger.info("Wrote eval report to %s", output_path)
    return output_path
