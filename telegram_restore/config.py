"""Configuration from .env / environment. Credentials are never hardcoded."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ALL_ANNOTATIONS = ("replies", "forwards", "reactions", "pins", "service", "calls", "ttl", "albums")
PROJECT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    export_dir: Path
    work_dir: Path
    api_id: int | None
    api_hash: str | None
    session: Path
    destination: str | None
    annotations: frozenset[str]
    reply_quote_chars: int

    @property
    def db_path(self) -> Path:
        return self.work_dir / "restore.sqlite3"

    def require_telegram(self) -> None:
        if not self.api_id or not self.api_hash:
            raise SystemExit("TG_API_ID and TG_API_HASH must be set in .env (see .env.example).")


def load(env_file: str | os.PathLike = PROJECT_DIR / ".env") -> Config:
    """Relative paths in .env are resolved against the .env file's directory, not the shell's cwd."""
    env_path = Path(env_file).resolve()
    if not env_path.is_file():
        raise SystemExit(f"{env_path} not found; copy .env.example to .env and fill it in.")
    load_dotenv(env_path)
    base = env_path.parent

    def path(value: str) -> Path:
        p = Path(value)
        return (p if p.is_absolute() else base / p).resolve()

    export = os.getenv("EXPORT_DIR")
    if not export:
        raise SystemExit("EXPORT_DIR is not set (see .env.example).")
    export_dir = path(export)
    if not export_dir.is_dir():
        raise SystemExit(f"EXPORT_DIR does not exist: {export_dir}")
    work_dir = path(os.getenv("WORK_DIR", "work"))
    if work_dir == export_dir or export_dir in work_dir.parents:
        raise SystemExit("WORK_DIR must be outside EXPORT_DIR; the export is treated as read-only.")
    work_dir.mkdir(parents=True, exist_ok=True)
    raw = os.getenv("ANNOTATE", ",".join(ALL_ANNOTATIONS))
    annotations = frozenset(a.strip() for a in raw.split(",") if a.strip())
    unknown = annotations - set(ALL_ANNOTATIONS)
    if unknown:
        raise SystemExit(f"Unknown ANNOTATE values: {sorted(unknown)}; allowed: {ALL_ANNOTATIONS}")
    quote_chars = os.getenv("REPLY_QUOTE_CHARS", "80").strip()
    if not quote_chars.isdigit():
        raise SystemExit("REPLY_QUOTE_CHARS must be a non-negative number.")
    api_id = os.getenv("TG_API_ID", "").strip()
    if api_id and not api_id.isdigit():
        raise SystemExit("TG_API_ID must be a number.")
    return Config(
        export_dir=export_dir,
        work_dir=work_dir,
        api_id=int(api_id) if api_id else None,
        api_hash=os.getenv("TG_API_HASH", "").strip() or None,
        session=path(os.getenv("TG_SESSION") or str(work_dir / "telegram")),
        destination=os.getenv("DESTINATION") or None,
        annotations=annotations,
        reply_quote_chars=int(quote_chars),
    )
