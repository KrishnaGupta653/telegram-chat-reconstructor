"""scan: export -> SQLite, with a forensic summary."""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from rich.progress import Progress

from . import database, parser
from .config import Config
from .media import Inspector, magic_mismatch
from .models import MEDIA_KINDS_WITH_FILE, Message

ALBUM_KINDS = {"photo", "video"}


def content_hash(m: Message) -> str:
    payload = [m.source_id, m.sender, m.ts.isoformat() if m.ts else None, m.kind, m.text,
               m.media.sha256 if m.media else None, m.media.rel_path if m.media else None,
               m.reply_to_id, m.forward, m.service_type, m.pin_target]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, default=str).encode()).hexdigest()


def mark_sent_together(messages: list[Message]) -> int:
    """Consecutive photo/video messages from one sender in the same second.

    The HTML export does not record album membership; this marks what the export does show
    (same sender, same second, adjacent, continuation blocks) and is labelled as such downstream.
    """
    groups, cur = 0, []

    def flush() -> None:
        nonlocal groups
        if len(cur) > 1:
            groups += 1
            for i, m in enumerate(cur, 1):
                m.group_key, m.group_pos, m.group_size = f"g{cur[0].source_id}", i, len(cur)

    for m in messages:
        if (m.kind in ALBUM_KINDS and cur and m.joined and m.sender == cur[-1].sender and m.ts == cur[-1].ts):
            cur.append(m)
        else:
            flush()
            cur = [m] if m.kind in ALBUM_KINDS else []
    flush()
    return groups


def scan(cfg: Config) -> dict:
    errors: list[tuple[str, int | None, str]] = []
    files = parser.html_files(cfg.export_dir)
    if not files:
        raise SystemExit(f"No messages*.html files in {cfg.export_dir}")
    inspector = Inspector(cfg.export_dir)
    messages: list[Message] = []
    with Progress() as progress:
        task = progress.add_task("Parsing HTML", total=len(files))
        for m in parser.iter_messages(cfg.export_dir, on_error=lambda f, i, e: errors.append((f, i, e)),
                                      on_file=lambda f: progress.advance(task)):
            messages.append(m)
        media_msgs = [m for m in messages if m.media]
        task2 = progress.add_task("Inspecting media", total=len(media_msgs))
        for m in media_msgs:
            inspector.inspect(m.media)
            progress.advance(task2)
    groups = mark_sent_together(messages)
    for m in messages:
        m.content_hash = content_hash(m)
    meta = {"export_dir": str(cfg.export_dir), "scanned_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "html_files": str(len(files))}
    conn = database.connect(cfg.db_path)
    database.replace_source(conn, messages, errors, meta)
    summary = summarize(messages, errors, files, groups)
    (cfg.work_dir / "scan_report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def summarize(messages: list[Message], errors: list, files: list[Path], groups: int) -> dict:
    real = [m for m in messages if not m.is_service]
    ids = {m.source_id for m in messages}
    media = [m.media for m in real if m.media]
    with_file = [d for d in media if d.kind in MEDIA_KINDS_WITH_FILE]
    unique = {d.sha256: d.size for d in with_file if d.exists}
    ts = [m.ts for m in real]
    return {
        "html_files": [f.name for f in files][:3] + ["..."] + [files[-1].name] if len(files) > 4 else [f.name for f in files],
        "html_file_count": len(files),
        "message_blocks": len(messages),
        "messages": len(real),
        "service": dict(collections.Counter(m.service_type for m in messages if m.is_service)),
        "kinds": dict(collections.Counter(m.kind for m in real).most_common()),
        "senders": dict(collections.Counter(m.sender for m in real).most_common()),
        "first": min(ts).isoformat() if ts else None,
        "last": max(ts).isoformat() if ts else None,
        "chronological": all(a <= b for a, b in zip(ts, ts[1:])),
        "replies": sum(1 for m in real if m.reply_to_id),
        "replies_target_missing": sum(1 for m in real if m.reply_to_id and m.reply_to_id not in ids),
        "forwards": sum(1 for m in real if m.forward),
        "messages_with_reactions": sum(1 for m in real if m.reactions),
        "messages_with_formatting": sum(1 for m in real if any(e.type not in ("url",) for e in m.entities)),
        "sent_together_groups": groups,
        "media_refs_with_file": len(with_file),
        "media_missing": sum(1 for d in with_file if not d.exists),
        "media_magic_mismatch": sum(1 for d in with_file if magic_mismatch(d)),
        "media_unique_files": len(unique),
        "media_unique_bytes": sum(unique.values()),
        "media_no_file_in_export": dict(collections.Counter(d.kind for d in media if d.kind not in MEDIA_KINDS_WITH_FILE)),
        "unsupported": [asdict(d) for d in media if d.kind == "unknown"][:20],
        "parse_errors": len(errors),
        "parse_error_examples": errors[:10],
    }
