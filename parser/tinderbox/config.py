"""Configuration loader. Reads $TINDERBOX_ENV_FILE then exposes typed config."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    supabase_url: str
    supabase_service_key: str
    schema: str = "tinderbox"

    inbox_dir: Path = field(default_factory=lambda: Path.home() / "tinderbox" / "inbox")
    work_dir: Path = field(default_factory=lambda: Path.home() / "tinderbox" / "work")
    archive_dir: Path = field(default_factory=lambda: Path.home() / "tinderbox" / "archive")
    logs_dir: Path = field(default_factory=lambda: Path.home() / "tinderbox" / "logs")
    blob_root: Path = field(default_factory=lambda: Path.home() / "tinderbox" / "blobs")

    stable_seconds: int = 30
    orphan_minutes: int = 30
    canary_pct: int = 10
    canary_abs: int = 50

    embedding_model: str = "mxbai-embed-large"
    embedding_dim: int = 1024
    ollama_url: str = "http://localhost:11434"

    log_level: str = "INFO"


def load_config() -> Config:
    env_file = os.environ.get("TINDERBOX_ENV_FILE", str(Path.home() / ".secrets" / "shared.env"))
    if Path(env_file).exists():
        load_dotenv(env_file, override=False)

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError(
            f"SUPABASE_URL and SUPABASE_SERVICE_KEY must be set. "
            f"Checked env file: {env_file}"
        )

    return Config(
        supabase_url=url,
        supabase_service_key=key,
        schema=os.environ.get("TINDERBOX_SCHEMA", "tinderbox"),
        inbox_dir=Path(os.environ.get("TINDERBOX_INBOX", str(Path.home() / "tinderbox" / "inbox"))),
        work_dir=Path(os.environ.get("TINDERBOX_WORK", str(Path.home() / "tinderbox" / "work"))),
        archive_dir=Path(os.environ.get("TINDERBOX_ARCHIVE", str(Path.home() / "tinderbox" / "archive"))),
        logs_dir=Path(os.environ.get("TINDERBOX_LOGS", str(Path.home() / "tinderbox" / "logs"))),
        blob_root=Path(os.environ.get("TINDERBOX_BLOB_ROOT", str(Path.home() / "tinderbox" / "blobs"))),
        stable_seconds=int(os.environ.get("TINDERBOX_STABLE_SECONDS", "30")),
        orphan_minutes=int(os.environ.get("TINDERBOX_ORPHAN_MINUTES", "30")),
        canary_pct=int(os.environ.get("TINDERBOX_CANARY_PCT", "10")),
        canary_abs=int(os.environ.get("TINDERBOX_CANARY_ABS", "50")),
        embedding_model=os.environ.get("TINDERBOX_EMBEDDING_MODEL", "mxbai-embed-large"),
        embedding_dim=int(os.environ.get("TINDERBOX_EMBEDDING_DIM", "1024")),
        ollama_url=os.environ.get("TINDERBOX_OLLAMA_URL", "http://localhost:11434"),
        log_level=os.environ.get("TINDERBOX_LOG_LEVEL", "INFO"),
    )
