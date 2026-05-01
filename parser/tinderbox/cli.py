"""Tinderbox CLI — entry point for ingest, scan, runs management, and diagnostics."""

import json
import logging
import os
import sys
import time
from pathlib import Path

import click

from tinderbox.config import load_config, Config
from tinderbox import db
from tinderbox.ingest.idempotency import compute_zip_sha256


def _setup_logging(config: Config):
    """Configure structured logging."""
    config.logs_dir.mkdir(parents=True, exist_ok=True)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s"
    ))

    file_handler = logging.FileHandler(config.logs_dir / "parser.log")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s"
    ))

    root = logging.getLogger("tinderbox")
    root.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
    root.addHandler(handler)
    root.addHandler(file_handler)


@click.group()
def cli():
    """Tinderbox — personal claude.ai conversation archive."""
    pass


@cli.command()
@click.argument("zip_path", type=click.Path(exists=True))
def ingest(zip_path: str):
    """Ingest a single export ZIP."""
    config = load_config()
    _setup_logging(config)
    db.init(config)

    from tinderbox.ingest.runner import run
    run_id = run(zip_path, config)
    click.echo(f"Run complete: {run_id}")


@cli.command()
@click.option("--messages-only", is_flag=True, help="Only embed messages, skip artifacts.")
@click.option("--artifacts-only", is_flag=True, help="Only embed artifacts, skip messages.")
@click.option("--limit", type=int, default=None,
              help="Cap source rows processed (smoke testing).")
def embed(messages_only: bool, artifacts_only: bool, limit: int | None):
    """Embed messages and artifacts via Ollama. Idempotent — re-running skips
    already-embedded chunks."""
    config = load_config()
    _setup_logging(config)
    db.init(config)

    from tinderbox.embed.worker import embed_messages, embed_artifacts

    if artifacts_only and messages_only:
        click.echo("--messages-only and --artifacts-only are mutually exclusive", err=True)
        sys.exit(2)

    if not artifacts_only:
        click.echo("Embedding messages...")
        msg_counters = embed_messages(config, limit=limit)
        click.echo(
            f"  messages: seen={msg_counters.sources_seen} "
            f"chunks_embedded={msg_counters.chunks_embedded} "
            f"empty={msg_counters.sources_skipped_empty} "
            f"already={msg_counters.sources_skipped_already_embedded}"
        )

    if not messages_only:
        click.echo("Embedding artifacts...")
        art_counters = embed_artifacts(config, limit=limit)
        click.echo(
            f"  artifacts: seen={art_counters.sources_seen} "
            f"chunks_embedded={art_counters.chunks_embedded} "
            f"already={art_counters.sources_skipped_already_embedded}"
        )

    click.echo("Embed pass complete.")


@cli.command()
@click.argument("query")
@click.option("--limit", type=int, default=10, help="Max results to return.")
@click.option("--caller", default="cli", help="Caller identity for query log.")
@click.option("--log/--no-log", default=True, help="Log this query to tinderbox.queries.")
def search(query: str, limit: int, caller: str, log: bool):
    """Hybrid retrieval over the archive (vector + FTS combined ranking)."""
    config = load_config()
    _setup_logging(config)
    db.init(config)

    from tinderbox.search.hybrid import hybrid_search

    results, latency_ms = hybrid_search(query, config, limit=limit)

    if log:
        from tinderbox.search.logging import log_query
        log_query(query, caller, results, latency_ms, "hybrid", config)

    if not results:
        click.echo("(no results)")
        return

    for i, r in enumerate(results, 1):
        click.echo(
            f"\n[{i}] score={r['combined_score']:.3f} "
            f"(cos_sim={r['cosine_sim']:.3f} fts={r['fts_rank']:.3f})"
        )
        click.echo(f"    {r['source_type']}/{r['source_id'][:8]}  "
                   f"chunk={r['chunk_index']}")
        if r.get("conversation_title"):
            click.echo(f"    Conv: {r['conversation_title']}")
        if r.get("snippet"):
            snippet = r["snippet"][:200].replace("\n", " ")
            click.echo(f"    \"{snippet}\"")


@cli.command()
@click.option("--limit", type=int, default=None,
              help="Cap number of conversations enriched (smoke testing).")
@click.option("--conv-id", "conv_ids", multiple=True,
              help="Enrich only these conversation IDs (repeatable). Bypasses limit.")
@click.option("--reenrich", is_flag=True,
              help="Re-enrich even if a row already exists for this model+version.")
@click.option("--retry-failures", is_flag=True,
              help="Re-attempt only conversations with a failure_reason row (skips already-enriched).")
@click.option("--sleep-ms", type=int, default=0,
              help="Sleep between Haiku calls (rate-limit safety).")
@click.option("--max-tokens", type=int, default=3000,
              help="Haiku max_tokens budget per call (default 3000). Bump to 5000+ for retries on truncated outputs.")
def enrich(limit: int | None, conv_ids: tuple[str, ...], reenrich: bool, retry_failures: bool,
           sleep_ms: int, max_tokens: int):
    """Enrich conversations via Haiku — summary, tags, key decisions, named instances."""
    config = load_config()
    _setup_logging(config)
    db.init(config)

    from tinderbox.enrichment.worker import run_enrichment

    counters = run_enrichment(
        config,
        limit=limit,
        only_conv_ids=list(conv_ids) if conv_ids else None,
        reenrich=reenrich,
        retry_failures=retry_failures,
        sleep_between_ms=sleep_ms,
        max_tokens=max_tokens,
    )

    click.echo(
        f"\nEnrichment pass: enriched={counters.enriched} "
        f"skipped_already={counters.skipped_already} "
        f"skipped_empty={counters.skipped_empty} "
        f"errors={counters.errors} "
        f"failure_rows={counters.failure_rows_written} "
        f"truncated={counters.truncated_for_prompt} "
        f"named_instance_links={counters.named_instance_links}"
    )

    if counters.error_details:
        click.echo("\nFirst few errors:")
        for err in counters.error_details[:5]:
            click.echo(f"  {err['export_id']}: {err['error_class']}: {err['message'][:200]}")


@cli.command("named-clean")
@click.option("--remove", "remove_names", multiple=True,
              help="Names to delete from named_instances (and their join rows). Repeatable.")
@click.option("--dry-run", is_flag=True, help="Show what would be removed; don't change anything.")
def named_clean(remove_names: tuple[str, ...], dry_run: bool):
    """Remove false-positive named_instances by name (case-sensitive)."""
    config = load_config()
    _setup_logging(config)
    db.init(config)

    if not remove_names:
        click.echo("No --remove names given. Pass one or more --remove NAME.")
        return

    for name in remove_names:
        ni = db.table("named_instances").select("id").eq("name", name).limit(1).execute()
        if not ni.data:
            click.echo(f"  {name}: not found")
            continue
        ni_id = ni.data[0]["id"]

        link_count = db.table("conversation_named_instances")\
            .select("conversation_id", count="exact").eq("named_instance_id", ni_id).execute()
        n_links = link_count.count or 0

        if dry_run:
            click.echo(f"  {name}: would delete 1 named_instance + {n_links} link(s)")
            continue

        db.table("conversation_named_instances").delete().eq("named_instance_id", ni_id).execute()
        db.table("named_instances").delete().eq("id", ni_id).execute()
        click.echo(f"  {name}: deleted (1 named_instance + {n_links} links)")


@cli.command("scan-inbox")
def scan_inbox():
    """Scan inbox directory for new ZIPs and ingest them."""
    config = load_config()
    _setup_logging(config)
    db.init(config)

    logger = logging.getLogger("tinderbox.scan")

    config.inbox_dir.mkdir(parents=True, exist_ok=True)
    zips = sorted(config.inbox_dir.glob("*.zip"))

    if not zips:
        logger.info("No ZIPs found in %s", config.inbox_dir)
        return

    for zip_path in zips:
        # File-stable check: size unchanged for stable_seconds
        size1 = zip_path.stat().st_size
        time.sleep(config.stable_seconds)
        if not zip_path.exists():
            logger.info("ZIP disappeared during stable check: %s", zip_path)
            continue
        size2 = zip_path.stat().st_size
        if size1 != size2:
            logger.info("ZIP still growing, skipping: %s (%d -> %d)", zip_path, size1, size2)
            continue

        # Check if already ingested
        sha256 = compute_zip_sha256(str(zip_path))
        from tinderbox.ingest.idempotency import zip_already_ingested
        if zip_already_ingested(sha256):
            logger.info("ZIP already ingested, skipping: %s", zip_path.name)
            continue

        logger.info("Ingesting: %s", zip_path.name)
        from tinderbox.ingest.runner import run
        run(str(zip_path), config)


@cli.group("runs")
def runs():
    """Manage ingest runs."""
    pass


@runs.command("list")
@click.option("--limit", default=10, help="Number of runs to show")
@click.option("--status", default=None, help="Filter by status")
def runs_list(limit: int, status: str | None):
    """List recent ingest runs."""
    config = load_config()
    db.init(config)

    query = (
        db.table("ingest_runs")
        .select("id,status,started_at,completed_at,conversations_seen,conversations_new,messages_new,artifacts_new,attachments_new")
        .order("started_at", desc=True)
        .limit(limit)
    )
    if status:
        query = query.eq("status", status)

    result = query.execute()

    if not result.data:
        click.echo("No runs found.")
        return

    for r in result.data:
        err_count = ""
        click.echo(
            f"  {r['id'][:8]}  {r['status']:<10}  "
            f"started={r['started_at'][:19]}  "
            f"convos={r.get('conversations_new', 0)}  "
            f"msgs={r.get('messages_new', 0)}  "
            f"artifacts={r.get('artifacts_new', 0)}  "
            f"attachments={r.get('attachments_new', 0)}"
        )


@runs.command("show")
@click.argument("run_id")
def runs_show(run_id: str):
    """Show full details of an ingest run."""
    config = load_config()
    db.init(config)

    result = (
        db.table("ingest_runs")
        .select("*")
        .eq("id", run_id)
        .limit(1)
        .execute()
    )

    if not result.data:
        click.echo(f"Run not found: {run_id}")
        sys.exit(1)

    click.echo(json.dumps(result.data[0], indent=2, default=str))


@cli.command("override-canary")
@click.argument("run_id")
def override_canary(run_id: str):
    """Re-run a canary-tripped failed run, forcing tombstone application."""
    config = load_config()
    _setup_logging(config)
    db.init(config)

    # Look up the failed run
    result = (
        db.table("ingest_runs")
        .select("id,status,export_path,export_sha256,errors")
        .eq("id", run_id)
        .limit(1)
        .execute()
    )

    if not result.data:
        click.echo(f"Run not found: {run_id}")
        sys.exit(1)

    run_data = result.data[0]

    if run_data["status"] != "failed":
        click.echo(f"Run {run_id} is not in 'failed' status (currently: {run_data['status']})")
        sys.exit(1)

    # Check that it was specifically a canary trip
    errors = run_data.get("errors", [])
    canary_tripped = any(
        e.get("error_class") == "mass_tombstone_canary_tripped"
        for e in errors
    )
    if not canary_tripped:
        click.echo(f"Run {run_id} did not fail due to canary — cannot override.")
        sys.exit(1)

    zip_path = run_data["export_path"]
    if not Path(zip_path).exists():
        # Try inbox
        inbox_path = config.inbox_dir / Path(zip_path).name
        if inbox_path.exists():
            zip_path = str(inbox_path)
        else:
            click.echo(f"ZIP not found at {zip_path} or {inbox_path}")
            sys.exit(1)

    click.echo(f"Overriding canary for run {run_id}. Re-ingesting {zip_path}...")

    # Delete the old sha256 entry so re-ingest doesn't no-op
    # We do this by updating the old run's sha256 to a prefixed version
    db.table("ingest_runs").update(
        {"export_sha256": f"overridden_{run_data['export_sha256']}"}
    ).eq("id", run_id).execute()

    from tinderbox.ingest.runner import run as run_ingest
    new_run_id = run_ingest(
        zip_path, config,
        canary_override=True,
        override_run_id=run_id,
    )
    click.echo(f"Override complete. New run: {new_run_id}")


@cli.command("doctor")
def doctor():
    """Sanity-check environment, DB, Ollama, and directories."""
    config = load_config()
    all_ok = True

    def check(name: str, ok: bool, detail: str = ""):
        nonlocal all_ok
        status = "OK" if ok else "FAIL"
        if not ok:
            all_ok = False
        msg = f"  [{status}] {name}"
        if detail:
            msg += f" — {detail}"
        click.echo(msg)

    click.echo("Tinderbox Doctor")
    click.echo("=" * 40)

    # Env file
    env_file = os.environ.get("TINDERBOX_ENV_FILE", str(Path.home() / ".secrets" / "shared.env"))
    check("Env file", Path(env_file).exists(), env_file)

    # Supabase
    check("SUPABASE_URL", bool(config.supabase_url), config.supabase_url[:40] + "...")

    # DB connection
    try:
        db.init(config)
        result = db.table("schema_migrations").select("version").order("version", desc=True).limit(1).execute()
        latest = result.data[0]["version"] if result.data else "none"
        check("DB connection", True, f"latest migration: {latest}")
    except Exception as e:
        check("DB connection", False, str(e)[:80])

    # Directories
    for name, path in [
        ("Inbox", config.inbox_dir),
        ("Work", config.work_dir),
        ("Archive", config.archive_dir),
        ("Logs", config.logs_dir),
        ("Blob root", config.blob_root),
    ]:
        exists = path.exists()
        writable = os.access(str(path), os.W_OK) if exists else False
        check(f"{name} dir", exists and writable, str(path))

    # Ollama
    try:
        import httpx
        resp = httpx.get(f"{config.ollama_url}/api/version", timeout=5.0)
        version = resp.json().get("version", "?")
        check("Ollama", resp.status_code == 200, f"version {version}")
    except Exception as e:
        check("Ollama", False, str(e)[:80])

    # Embedding model
    try:
        import httpx
        resp = httpx.post(
            f"{config.ollama_url}/api/embeddings",
            json={"model": config.embedding_model, "prompt": "test"},
            timeout=30.0,
        )
        data = resp.json()
        dim = len(data.get("embedding", []))
        check("Embedding model", dim == config.embedding_dim,
              f"{config.embedding_model} dim={dim} (expected {config.embedding_dim})")
    except Exception as e:
        check("Embedding model", False, str(e)[:80])

    click.echo("=" * 40)
    click.echo("All checks passed." if all_ok else "Some checks failed.")
    sys.exit(0 if all_ok else 1)


@cli.command("staleness")
@click.option("--days", type=int, default=30, help="Stale-after-N-days threshold.")
@click.option("--cooldown-days", type=int, default=7,
              help="Skip alerts for this many days after a fresh import.")
@click.option("--debounce-hours", type=int, default=22,
              help="Min hours between consecutive stale alerts.")
@click.option("--alert/--no-alert", default=True,
              help="Post to agent_comms if alert conditions met.")
@click.option("--force", is_flag=True,
              help="Bypass cooldown + debounce; alert if stale (manual run).")
def staleness_check(days: int, cooldown_days: int, debounce_hours: int,
                    alert: bool, force: bool):
    """Check archive freshness vs claude.ai re-export cadence.

    Cadence (daily via launchd): silent during cooldown, silent while fresh,
    alerts when stale, debounced so it doesn't double-fire.
    """
    config = load_config()
    _setup_logging(config)
    db.init(config)

    from tinderbox.staleness import check, post_to_agent_comms, record_alert_sent

    if force:
        # Force = ignore cooldown/debounce; just check stale_days.
        should_alert, days_since, message = check(
            stale_days=days, cooldown_days=0, debounce_hours=0,
        )
    else:
        should_alert, days_since, message = check(
            stale_days=days,
            cooldown_days=cooldown_days,
            debounce_hours=debounce_hours,
        )

    click.echo(message)
    if should_alert and alert:
        ok = post_to_agent_comms(f"⚠️ Tinderbox: {message}")
        click.echo(f"agent_comms alert posted: {ok}")
        if ok:
            record_alert_sent()
    sys.exit(1 if should_alert else 0)


@cli.group("qa")
def qa_group():
    """Retrieval QA: scheduled hit@K measurement against a frozen query set."""
    pass


@qa_group.command("generate")
@click.option("--target", type=int, default=50,
              help="Number of conversations to sample (3 queries each).")
@click.option("--seed", type=int, default=42)
def qa_generate(target: int, seed: int):
    """Generate the frozen eval query set via Haiku. Idempotent: only adds
    queries for conversations not already covered."""
    config = load_config()
    _setup_logging(config)
    db.init(config)

    from tinderbox.qa.generate import generate_eval_set
    counters = generate_eval_set(config, target_total=target, seed=seed)
    click.echo(json.dumps(counters, indent=2))


@qa_group.command("run")
@click.option("--limit", type=int, default=None,
              help="Cap queries run (smoke testing).")
@click.option("--cosine-filter", type=float, default=0.6)
@click.option("--report/--no-report", default=True,
              help="Write markdown report to ~/tinderbox/eval/latest.md")
def qa_run(limit: int | None, cosine_filter: float, report: bool):
    """Run the frozen QA query set, score hit@1/3/10, write eval_runs row."""
    config = load_config()
    _setup_logging(config)
    db.init(config)

    from tinderbox.qa.runner import run_qa
    run_id = run_qa(config, limit=limit, cosine_filter=cosine_filter)
    click.echo(f"QA run complete: {run_id}")

    if report:
        from tinderbox.qa.report import render_report
        out = Path.home() / "tinderbox" / "eval" / "latest.md"
        path = render_report(run_id, out)
        click.echo(f"Report: {path}")
        # Also write a timestamped copy alongside
        ts_path = out.parent / f"{run_id}.md"
        ts_path.write_text(out.read_text())
        click.echo(f"Archive: {ts_path}")


@qa_group.command("report")
@click.argument("run_id", required=False)
def qa_report(run_id: str | None):
    """Render the markdown report for a QA run. Defaults to most recent."""
    config = load_config()
    db.init(config)

    if not run_id:
        latest = (
            db.table("eval_runs")
            .select("id")
            .order("started_at", desc=True)
            .limit(1)
            .execute()
        )
        if not latest.data:
            click.echo("No QA runs found.")
            sys.exit(1)
        run_id = latest.data[0]["id"]

    from tinderbox.qa.report import render_report
    out = Path.home() / "tinderbox" / "eval" / "latest.md"
    path = render_report(run_id, out)
    click.echo(f"Report: {path}")


def main():
    cli()


if __name__ == "__main__":
    main()
