"""Normalized messages -> WhatsApp-style chat text for Telegram's history import.

Telegram's import parser grammar is undocumented; both candidate formats are emitted so the
read-only `probe` command (messages.checkHistoryImport) can decide which one the server accepts.
Everything Telegram cannot represent natively is written as a bracketed, clearly-labelled note.
"""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .models import MEDIA_KINDS_WITH_FILE, Message

FORMATS = ("ios", "android")
LINE_START = re.compile(r"^\[?\d{1,2}/\d{1,2}/\d{2,4},? \d{1,2}:\d{2}")
WORD_JOINER = "\u2060"
MAX_MESSAGE_CHARS = 2000  # history import silently drops messages of ~2800+ characters (2000 verified)
MAX_MULTILINE_CHARS = 1200  # multi-line messages of 1765+ characters are dropped; 1301 verified
EXT_OVERRIDE = {"sticker_animated": ".webm"}  # .tgs is not accepted by history import; uploaded as a VP9 sticker
KIND_LABEL = {
    "photo": "Photo", "video": "Video", "gif": "GIF", "sticker": "Sticker", "sticker_animated": "Sticker",
    "sticker_video": "Sticker", "document": "File", "voice": "Voice message", "audio": "Audio",
    "call": "Call", "ttl_photo": "Self-destructing photo", "ttl_video": "Self-destructing video",
}
WA_MARK = {"bold": "*", "italic": "_", "strike": "~", "code": "```", "pre": "```"}
MEDIA_TAG = {
    "photo": "PHOTO", "video": "VIDEO", "gif": "GIF", "sticker": "STICKER", "sticker_animated": "STICKER",
    "sticker_video": "STICKER", "document": "DOCUMENT", "voice": "AUDIO", "audio": "AUDIO",
}


@dataclass
class RenderOptions:
    fmt: str = "ios"
    annotations: frozenset[str] = frozenset()
    formatting: str = "plain"  # plain | whatsapp
    utc_offset: dt.timezone | None = None  # None: keep each message's original offset (wall clock)
    reply_quote_chars: int = 80
    group_header: tuple[str, str] | None = None  # (title, creator): makes Telegram parse the file as a group chat


@dataclass
class Rendered:
    lines: list[str] = field(default_factory=list)
    media: list[dict] = field(default_factory=list)  # {source_id, import_name, rel_path, sha256, size, kind, line}
    escaped_lines: int = 0
    starts: list[tuple[int, int]] = field(default_factory=list)  # (line index, source_id) of every message line
    skipped: dict[str, int] = field(default_factory=dict)


def parse_offset(value: str | None) -> dt.timezone | None:
    if not value:
        return None
    m = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
    if not m:
        raise ValueError(f"offset must look like +05:30, got {value!r}")
    delta = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return dt.timezone(delta if m.group(1) == "+" else -delta)


def stamp(ts: dt.datetime, opts: RenderOptions) -> str:
    t = ts.astimezone(opts.utc_offset) if opts.utc_offset else ts
    return f"[{t:%d/%m/%Y, %H:%M:%S}]" if opts.fmt == "ios" else f"{t:%d/%m/%Y, %H:%M} -"


def human_ts(ts: dt.datetime | None) -> str:
    return f"{ts:%d.%m.%Y %H:%M:%S}" if ts else "unknown time"


def import_name(m: Message) -> str:
    """Unique per message so every attachment line maps to exactly one uploaded file."""
    ext = EXT_OVERRIDE.get(m.kind) or Path(urllib.parse.unquote(m.media.rel_path)).suffix.lower()
    return f"{m.source_id:08d}-{MEDIA_TAG[m.kind]}-{m.ts:%Y-%m-%d-%H-%M-%S}{ext}"


def preview(m: Message | None, limit: int) -> str:
    if m is None:
        return ""
    label = KIND_LABEL.get(m.kind)
    if m.kind.startswith("sticker") and m.media and m.media.emoji:
        label = f"Sticker {m.media.emoji}"
    text = re.sub(r"\s+", " ", m.text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    if label and text:
        return f"[{label}] {text}"
    return f"[{label}]" if label else text


def apply_formatting(m: Message, opts: RenderOptions) -> str:
    """plain: original text, link targets appended when hidden. whatsapp: also *bold* _italic_ ~strike~."""
    inserts: list[tuple[int, int, str]] = []  # (position, order, marker)
    for e in m.entities:
        end = e.offset + e.length
        if e.type == "text_link" and e.url:
            inserts.append((end, 0, f" ({e.url})"))
        elif opts.formatting == "whatsapp" and e.type in WA_MARK:
            inserts.append((e.offset, 2, WA_MARK[e.type]))
            inserts.append((end, 1, WA_MARK[e.type]))
    text = m.text
    for pos, _, marker in sorted(inserts, key=lambda x: (x[0], x[1]), reverse=True):
        text = text[:pos] + marker + text[pos:]
    return text


def _call_note(m: Message) -> str:
    status = m.media.status if m.media else ""
    secs = re.search(r"\((\d+) seconds\)", status)
    if secs:
        s = int(secs.group(1))
        status = re.sub(r"\s*\(\d+ seconds\)", f" ({s // 3600}h {s % 3600 // 60:02d}m {s % 60:02d}s)"
                        if s >= 3600 else f" ({s // 60}m {s % 60:02d}s)", status)
    return f"[📞 Call: {status or 'unknown status'}]"


def render(messages: list[Message], opts: RenderOptions, lookup: dict[int, Message] | None = None) -> Rendered:
    """`lookup` resolves reply/pin targets outside `messages` (defaults to `messages` itself)."""
    if opts.fmt not in FORMATS:
        raise ValueError(f"format must be one of {FORMATS}")
    by_id = lookup if lookup is not None else {m.source_id: m for m in messages}
    out = Rendered()
    ann = opts.annotations
    if opts.group_header:
        title, creator = opts.group_header
        first_ts = next(m.ts for m in messages if m.ts) - dt.timedelta(seconds=1)
        out.lines.append(f"{stamp(first_ts, opts)} {title}: \u200e{creator} created group \u201c{title}\u201d"
                         if opts.fmt == "ios" else f"{stamp(first_ts, opts)} {creator} created group \"{title}\"")
    for m in messages:
        if m.service_type == "date_separator":
            out.skipped["date_separator"] = out.skipped.get("date_separator", 0) + 1
            continue
        if m.is_service and ("pins" if m.service_type == "pin" else "service") not in ann:
            out.skipped[m.service_type] = out.skipped.get(m.service_type, 0) + 1
            continue
        if m.ts is None or m.sender is None:
            out.skipped["no_time_or_sender"] = out.skipped.get("no_time_or_sender", 0) + 1
            continue

        head: str | None = None
        body: list[str] = []
        if m.service_type == "pin":
            target = by_id.get(m.pin_target)
            body.append(f"[📌 Pinned a message from {human_ts(target.ts)}: {preview(target, opts.reply_quote_chars)}]"
                        if target else "[📌 Pinned a message that is not in this export]")
        elif m.is_service:
            body.append(f"[ℹ️ {m.text}]")
        else:
            if m.media and m.kind in MEDIA_KINDS_WITH_FILE:
                if m.media.exists:
                    name = import_name(m)
                    head = f"\u200e<attached: {name}>" if opts.fmt == "ios" else f"{name} (file attached)"
                    out.media.append({"source_id": m.source_id, "import_name": name, "rel_path": m.media.rel_path,
                                      "sha256": m.media.sha256, "size": m.media.size, "kind": m.kind,
                                      "line": len(out.lines)})
                else:
                    body.append(f"[{KIND_LABEL[m.kind]}: file missing from the export]")
            if m.forward and "forwards" in ann:
                who = m.forward.get("from_name") or "unknown sender"
                when = m.forward.get("date")
                body.append(f"[↪ Forwarded from {who}" + (f", {human_ts(dt.datetime.fromisoformat(when))}]" if when else "]"))
            if m.reply_to_id and "replies" in ann:
                target = by_id.get(m.reply_to_id)
                body.append(f"[↩ Reply to {target.sender}, {human_ts(target.ts)}: {preview(target, opts.reply_quote_chars)}]"
                            if target else "[↩ Reply to a message that is not in this export]")
            if m.kind == "call" and "calls" in ann:
                body.append(_call_note(m))
            elif m.kind in ("ttl_photo", "ttl_video") and "ttl" in ann:
                state = m.media.status or "unavailable"
                body.append(f"[⏱ {KIND_LABEL[m.kind]} ({state}): not included in the export]")
            elif m.kind == "unknown":
                body.append(f"[Unsupported content: {m.media.title or 'unknown'}]")
            if m.text:
                body.extend(apply_formatting(m, opts).split("\n"))
            if m.group_key and "albums" in ann:
                body.append(f"[Sent together: {m.group_pos}/{m.group_size}]")
            if m.reactions and "reactions" in ann:
                body.append("[Reactions: " + "; ".join(f"{r['emoji']} {', '.join(r['actors'])}".strip()
                                                       for r in m.reactions) + "]")
        if head is None and not body:
            out.skipped[f"empty_{m.kind}"] = out.skipped.get(f"empty_{m.kind}", 0) + 1
            continue

        prefix = f"{stamp(m.ts, opts)} {m.sender}: "
        if head is not None:
            # Telegram drops the attachment when text follows it, so notes/caption become the next message.
            out.starts.append((len(out.lines), m.source_id))
            out.lines.append(prefix + head)
        limit = MAX_MESSAGE_CHARS if len(body) == 1 else MAX_MULTILINE_CHARS
        for part in split_message(body, limit):
            for i, line in enumerate(part):
                line = line.rstrip()
                if not line:
                    line = WORD_JOINER  # Telegram's parser loses messages containing empty lines
                elif i and LINE_START.match(line):
                    line = WORD_JOINER + line
                    out.escaped_lines += 1
                if i == 0:
                    out.starts.append((len(out.lines), m.source_id))
                out.lines.append(prefix + line if i == 0 else line)
    return out


def split_message(lines: list[str], limit: int) -> list[list[str]]:
    """Group lines into messages of at most `limit` characters, splitting overlong lines."""
    parts: list[list[str]] = []
    size = limit + 1
    for line in lines:
        while len(line) > limit:
            parts.append([line[:limit]])
            line, size = line[limit:], limit + 1
        n = len(line) + 1
        if size + n > limit:
            parts.append([])
            size = 0
        parts[-1].append(line)
        size += n
    return parts
