"""dry-run: build the import artifacts locally. Never contacts Telegram."""
from __future__ import annotations

import bisect
import collections
import datetime as dt
import hashlib
import json
from pathlib import Path

from . import database
from .config import Config
from .media import upload_spec
from .models import Message
from .render import LINE_START, RenderOptions, render

ANDROID_CLIENT_LIMIT = 32 * 2**20


def chunk_bounds(lines: list[str], max_bytes: int) -> list[int]:
    """Start line index of each chunk; splits only at message starts."""
    starts, size = [0], 0
    for i, line in enumerate(lines):
        n = len(line.encode("utf-8")) + 1
        if i > starts[-1] and size + n > max_bytes and LINE_START.match(line):
            starts.append(i)
            size = 0
        size += n
    return starts


def build(messages: list[Message], opts: RenderOptions, out_dir: Path, max_chunk_bytes: int,
          lookup: dict[int, Message] | None = None) -> tuple[dict, list[dict]]:
    """Write chat_NNN.txt + media_manifest.json + plan.json into out_dir; return (plan, manifest).

    `lookup` resolves reply/pin targets that are not part of `messages` (used for partial imports).
    """
    by_id = lookup or {m.source_id: m for m in messages}
    rendered = render(messages, opts, lookup=by_id)
    out_dir.mkdir(parents=True)

    starts = chunk_bounds(rendered.lines, max_chunk_bytes)
    media_per_chunk = collections.Counter(bisect.bisect_right(starts, m["line"]) for m in rendered.media)
    plan_chunks = []
    for i, start in enumerate(starts, 1):
        lines = rendered.lines[start:starts[i] if i < len(starts) else None]
        data = ("\n".join(lines) + "\n").encode("utf-8")
        name = f"chat_{i:03d}.txt"
        (out_dir / name).write_bytes(data)
        plan_chunks.append({"file": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                            "message_lines": sum(1 for line in lines if LINE_START.match(line)),
                            "media_count": media_per_chunk[i]})

    index = {f"chat_{i:03d}.txt": [] for i in range(1, len(starts) + 1)}
    for line, sid in rendered.starts:
        index[f"chat_{bisect.bisect_right(starts, line):03d}.txt"].append([sid, by_id[sid].ts.isoformat()])
    (out_dir / "messages_index.json").write_text(json.dumps(index), encoding="utf-8")

    manifest = [{**item, "chunk": bisect.bisect_right(starts, item["line"]),
                 "upload": upload_spec(by_id[item["source_id"]].media)} for item in rendered.media]
    (out_dir / "media_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    unique = {m["sha256"]: m["size"] for m in rendered.media}
    plan = {
        "format": opts.fmt, "formatting": opts.formatting, "annotations": sorted(opts.annotations),
        "utc_offset": str(opts.utc_offset) if opts.utc_offset else "original (wall clock of export)",
        "output_dir": str(out_dir), "chunks": plan_chunks,
        "message_lines": sum(c["message_lines"] for c in plan_chunks),
        "media_uploads": len(rendered.media),
        "media_by_kind": dict(collections.Counter(m["kind"] for m in rendered.media).most_common()),
        "unique_media_files": len(unique), "unique_media_bytes": sum(unique.values()),
        "needs_live_test": dict(collections.Counter(m["kind"] for m in manifest if m["upload"].get("needs_live_test"))),
        "escaped_continuation_lines": rendered.escaped_lines, "skipped": rendered.skipped,
        "warnings": [f"{c['file']} is {c['bytes'] / 2**20:.1f} MiB, above the 32 MiB Android client limit"
                     for c in plan_chunks if c["bytes"] > ANDROID_CLIENT_LIMIT],
    }
    (out_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return plan, manifest


def load_messages(cfg: Config) -> list[Message]:
    conn = database.connect(cfg.db_path)
    messages = list(database.iter_messages(conn))
    conn.close()
    if not messages:
        raise SystemExit("Database is empty; run `python main.py scan` first.")
    return messages


def dry_run(cfg: Config, opts: RenderOptions, max_chunk_bytes: int = 30 * 2**20) -> dict:
    out_dir = cfg.work_dir / "dry_run" / f"{opts.fmt}-{dt.datetime.now():%Y%m%d-%H%M%S}"
    plan, _ = build(load_messages(cfg), opts, out_dir, max_chunk_bytes)
    return plan
