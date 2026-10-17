"""validate: consistency checks on the parsed database before anything is sent to Telegram."""
from __future__ import annotations

import collections
from dataclasses import dataclass

from . import database
from .config import Config
from .media import magic_mismatch
from .models import MEDIA_KINDS_WITH_FILE
from .render import LINE_START, import_name


@dataclass
class Finding:
    level: str  # error | warning | info
    check: str
    count: int
    detail: str = ""


def validate(cfg: Config) -> list[Finding]:
    conn = database.connect(cfg.db_path)
    msgs = list(database.iter_messages(conn))
    if not msgs:
        raise SystemExit("Database is empty; run `python main.py scan` first.")
    real = [m for m in msgs if not m.is_service]
    ids = {m.source_id for m in msgs}
    out: list[Finding] = []

    def add(level: str, check: str, items: list, detail: str = "") -> None:
        out.append(Finding(level if items else "ok", check, len(items), detail))

    n_errors = conn.execute("SELECT COUNT(*) FROM parse_errors").fetchone()[0]
    out.append(Finding("error" if n_errors else "ok", "HTML blocks that failed to parse", n_errors))
    pos = [m.source_id for m in msgs if m.source_id > 0]
    add("error", "message IDs not increasing in file order", [b for a, b in zip(pos, pos[1:]) if b <= a])
    add("error", "timestamps going backwards", [b for a, b in zip(real, real[1:]) if b.ts < a.ts])
    add("error", "messages without timestamp or sender", [m for m in real if m.ts is None or not m.sender])
    senders = collections.Counter(m.sender for m in real)
    out.append(Finding("warning" if len(senders) != 2 else "info", "distinct senders (private chat expects 2)",
                       len(senders), ", ".join(f"{s}: {c}" for s, c in senders.most_common())))
    add("error", "sender names that would break chat-line parsing",
        [s for s in senders if s and (":" in s or "\n" in s or LINE_START.match(s))])
    add("info", "replies whose original is not in the export",
        [m for m in real if m.reply_to_id and m.reply_to_id not in ids])
    add("info", "pins whose message is not in the export",
        [m for m in msgs if m.service_type == "pin" and m.pin_target not in ids])
    add("info", "replies pointing forward in time",
        [m for m in real if m.reply_to_id and m.reply_to_id > m.source_id])

    with_file = [m for m in real if m.media and m.kind in MEDIA_KINDS_WITH_FILE]
    add("warning", "media files missing from the export", [m for m in with_file if not m.media.exists])
    add("warning", "media whose bytes do not match their type", [m for m in with_file if magic_mismatch(m.media)])
    add("warning", "zero-byte media files", [m for m in with_file if m.media.exists and not m.media.size])
    names = collections.Counter(import_name(m) for m in with_file if m.media.exists)
    add("error", "duplicate import file names", [n for n, c in names.items() if c > 1])
    add("info", "media without a file by nature (calls, expired self-destructing)",
        [m for m in real if m.media and m.kind not in MEDIA_KINDS_WITH_FILE and m.kind != "unknown"])
    add("warning", "unsupported/unknown content", [m for m in real if m.kind == "unknown"])
    add("warning", "text longer than 4096 characters", [m for m in real if len(m.text) > 4096])
    shared = collections.Counter(m.media.sha256 for m in with_file if m.media.exists)
    out.append(Finding("info", "media references sharing an identical file", sum(c for c in shared.values() if c > 1),
                       f"{len(shared)} unique files for {len(with_file)} attachments"))
    hashes = collections.Counter(m.content_hash for m in msgs)
    add("error", "duplicate content hashes", [h for h, c in hashes.items() if c > 1])
    add("info", "messages with formatting that plain rendering drops",
        [m for m in real if any(e.type not in ("url", "text_link") for e in m.entities)])
    add("info", "message continuation lines that look like a new chat line (will be escaped)",
        [m for m in real if any(LINE_START.match(line) for line in m.text.split("\n")[1:])])
    return out
