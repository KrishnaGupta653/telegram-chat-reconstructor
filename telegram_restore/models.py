"""Normalized, source-faithful representation of exported messages. Fields are only filled from the export."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

MEDIA_KINDS_WITH_FILE = {
    "photo", "video", "gif", "sticker", "sticker_animated", "sticker_video",
    "document", "voice", "audio",
}
NOTE_ONLY_KINDS = {"call", "ttl_photo", "ttl_video"}


@dataclass
class Media:
    kind: str
    rel_path: str | None = None
    thumb: str | None = None
    title: str = ""
    status: str = ""
    duration: int | None = None
    emoji: str | None = None
    exists: bool = False
    size: int | None = None
    sha256: str | None = None
    sniffed: str | None = None


@dataclass
class Entity:
    type: str  # bold, italic, code, pre, strike, underline, spoiler, blockquote, url, text_link
    offset: int  # in Python str characters
    length: int
    url: str | None = None


@dataclass
class Message:
    source_id: int
    html_file: str
    seq: int
    sender: str | None
    ts: dt.datetime | None
    ts_inferred: bool = False
    kind: str = "text"
    text: str = ""
    entities: list[Entity] = field(default_factory=list)
    media: Media | None = None
    reply_to_id: int | None = None
    forward: dict | None = None
    reactions: list[dict] = field(default_factory=list)
    service_type: str | None = None
    pin_target: int | None = None
    group_key: str | None = None
    group_pos: int | None = None
    group_size: int | None = None
    joined: bool = False
    content_hash: str | None = None

    @property
    def is_service(self) -> bool:
        return self.service_type is not None
