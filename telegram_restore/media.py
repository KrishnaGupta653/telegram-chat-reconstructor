"""Resolve, verify and describe export media. Files are opened read-only."""
from __future__ import annotations

import hashlib
import mimetypes
import urllib.parse
from pathlib import Path

from .models import Media

MAGIC = [
    (b"\xff\xd8\xff", "jpeg"), (b"\x89PNG", "png"), (b"\x1aE\xdf\xa3", "webm"), (b"OggS", "ogg"),
    (b"\x1f\x8b", "gzip"), (b"%PDF", "pdf"), (b"PK\x03\x04", "zip"), (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"), (b"\xff\xf3", "mp3"), (b"\xff\xf2", "mp3"), (b"GIF8", "gif"),
]
EXPECTED = {
    "photo": {"jpeg", "png"}, "video": {"mp4", "webm"}, "gif": {"mp4"}, "sticker": {"webp"},
    "sticker_animated": {"gzip"}, "sticker_video": {"webm"}, "voice": {"ogg"}, "audio": {"mp3", "mp4", "ogg"},
}
SNIFF_MIME = {"mp3": "audio/mpeg", "mp4": "audio/mp4", "ogg": "audio/ogg"}


def resolve(root: Path, ref: str) -> Path | None:
    """Absolute path of a relative export reference, or None if it is missing or escapes the export."""
    p = (root / urllib.parse.unquote(ref)).resolve()
    return p if root in p.parents and p.is_file() else None


def sniff(path: Path) -> str:
    with open(path, "rb") as f:
        head = f.read(16)
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return next((name for sig, name in MAGIC if head.startswith(sig)), "unknown")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Inspector:
    """Fills file facts into Media objects; each physical file is hashed once."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[Path, tuple[int, str, str]] = {}

    def inspect(self, media: Media) -> None:
        if not media.rel_path or ":" in media.rel_path:
            return
        path = resolve(self.root, media.rel_path)
        if path is None:
            media.exists = False
            return
        if path not in self._cache:
            self._cache[path] = (path.stat().st_size, sha256(path), sniff(path))
        media.exists = True
        media.size, media.sha256, media.sniffed = self._cache[path]


def magic_mismatch(media: Media) -> bool:
    expected = EXPECTED.get(media.kind)
    return bool(expected and media.exists and media.sniffed not in expected)


def upload_spec(media: Media) -> dict:
    """How the importer should present this file to Telegram (InputMedia type, mime, attributes).

    Sticker and GIF handling inside history import is not documented; specs flagged
    `needs_live_test` must be confirmed by the small test import before the full run.
    """
    name = Path(urllib.parse.unquote(media.rel_path or "")).name
    k = media.kind
    if k == "photo" and media.sniffed in ("jpeg", "png"):
        return {"input": "photo"}
    if k == "video":
        return {"input": "document", "mime": "video/mp4" if media.sniffed == "mp4" else "video/webm",
                "attributes": [{"type": "video", "duration": media.duration or 0, "supports_streaming": True},
                               {"type": "filename", "name": name}]}
    if k == "gif":
        return {"input": "document", "mime": "video/mp4", "needs_live_test": True,
                "attributes": [{"type": "animated"}, {"type": "video", "duration": 0, "nosound": True},
                               {"type": "filename", "name": name}]}
    if k == "sticker_animated":
        return {"input": "document", "mime": "video/webm", "convert": "tgs_to_webm",
                "attributes": [{"type": "sticker", "alt": media.emoji or ""},
                               {"type": "filename", "name": Path(name).with_suffix(".webm").name},
                               {"type": "video", "duration": 0, "w": 512, "h": 512}]}
    if k in ("sticker", "sticker_video"):
        mime = {"sticker": "image/webp", "sticker_video": "video/webm"}[k]
        attrs = [{"type": "sticker", "alt": media.emoji or ""}, {"type": "filename", "name": name}]
        if k == "sticker_video":
            attrs.append({"type": "video", "duration": 0, "w": 512, "h": 512})
        return {"input": "document", "mime": mime, "attributes": attrs, "needs_live_test": True}
    if k == "voice":
        return {"input": "document", "mime": "audio/ogg",
                "attributes": [{"type": "audio", "voice": True, "duration": media.duration or 0}]}
    if k == "audio":
        return {"input": "document", "mime": SNIFF_MIME.get(media.sniffed or "", "audio/mpeg"),
                "attributes": [{"type": "audio", "duration": media.duration or 0, "title": media.title or None},
                               {"type": "filename", "name": name}]}
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return {"input": "document", "mime": mime, "attributes": [{"type": "filename", "name": name}]}
