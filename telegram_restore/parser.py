"""Telegram Desktop HTML export -> normalized Message objects. Never writes to the export."""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Iterator
from pathlib import Path

from lxml import html

from .models import Entity, Media, Message

DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2}):(\d{2}) UTC([+-])(\d{2}):(\d{2})$")
GOTO_RE = re.compile(r"(?:^|/)(messages\d*\.html)?#go_to_message(\d+)$")
DATE_SEP_RE = re.compile(r"^\d{1,2} \w+ \d{4}$")
CLOCK_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")
TAG_ENTITY = {
    "strong": "bold", "b": "bold", "em": "italic", "i": "italic", "code": "code", "pre": "pre",
    "s": "strike", "del": "strike", "u": "underline", "blockquote": "blockquote",
}
SIMPLE_MEDIA = [
    ("media_voice_message", "voice"), ("media_audio_file", "audio"), ("media_file", "document"),
    ("media_call", "call"), ("media_contact", "contact"), ("media_location", "location"),
    ("media_live_location", "live_location"), ("media_venue", "venue"), ("media_poll", "poll"),
    ("media_game", "game"), ("media_invoice", "invoice"),
]


class ParseError(Exception):
    pass


def html_files(root: Path) -> list[Path]:
    return sorted(root.glob("messages*.html"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 1))


def cls(el) -> set[str]:
    return set((el.get("class") or "").split())


def child(el, klass: str):
    want = set(klass.split())
    for c in el:
        if isinstance(c.tag, str) and want <= cls(c):
            return c
    return None


def squash(el) -> str:
    return re.sub(r"\s+", " ", el.text_content()).strip() if el is not None else ""


def parse_date(title: str) -> dt.datetime | None:
    m = DATE_RE.match(title.strip())
    if not m:
        return None
    d, mo, y, h, mi, s, sign, oh, om = m.groups()
    off = dt.timedelta(hours=int(oh), minutes=int(om)) * (1 if sign == "+" else -1)
    return dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=dt.timezone(off))


def parse_clock(value: str) -> int | None:
    m = CLOCK_RE.match(value.strip())
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(s)


def extract_text(div) -> tuple[str, list[Entity]]:
    """Plain text with <br> as newlines, plus formatting entities (offsets in str characters)."""
    parts: list[str] = []
    entities: list[Entity] = []
    length = 0

    def add(s: str) -> None:
        nonlocal length
        parts.append(s)
        length += len(s)

    def walk(node) -> None:
        if node.text:
            add(node.text)
        for ch in node:
            if not isinstance(ch.tag, str):
                pass
            elif ch.tag == "br":
                add("\n")
            else:
                start = length
                walk(ch)
                etype, url = TAG_ENTITY.get(ch.tag), None
                if ch.tag == "a" and ch.get("href"):
                    url = ch.get("href")
                    covered = "".join(parts)[start:length]
                    etype = "url" if covered == url else "text_link"
                elif ch.tag == "span" and "spoiler" in cls(ch):
                    etype = "spoiler"
                if etype and length > start:
                    entities.append(Entity(etype, start, length - start, url))
            if ch.tail:
                add(ch.tail)

    walk(div)
    raw = "".join(parts)
    lead = len(raw) - len(raw.lstrip())
    text = raw.strip()
    kept = []
    for e in entities:
        start = max(e.offset - lead, 0)
        end = min(e.offset + e.length - lead, len(text))
        if end > start:
            kept.append(Entity(e.type, start, end - start, e.url))
    return text, kept


def classify_media(node) -> Media:
    c = cls(node)
    title = squash(node.find_class("title")[0]) if node.find_class("title") else ""
    status = squash(node.find_class("status")[0]) if node.find_class("status") else ""
    href = node.get("href")
    img = node.find(".//img")
    thumb = img.get("src") if img is not None else None
    if "photo_wrap" in c:
        kind = "photo"
    elif "video_file_wrap" in c:
        kind = "video"
        dur = node.find_class("video_duration")
        status = squash(dur[0]) if dur else status
    elif "animated_wrap" in c:
        kind = "gif"
    elif "sticker_wrap" in c:
        kind = "sticker"
    elif "media_photo" in c:
        if title == "Sticker":
            ext = (href or "").rsplit(".", 1)[-1].lower()
            kind = {"tgs": "sticker_animated", "webm": "sticker_video"}.get(ext, "sticker")
        elif title == "Self-destructing photo":
            kind = "ttl_photo"
        else:
            kind = "photo"
    elif "media_video" in c:
        kind = {"Animation": "gif", "Self-destructing video": "ttl_video"}.get(title, "video")
    else:
        kind = next((k for marker, k in SIMPLE_MEDIA if marker in c), "unknown")
    media = Media(kind=kind, rel_path=href, thumb=thumb, title=title, status=status)
    if kind in ("video", "voice", "audio"):
        media.duration = parse_clock(status)
    if kind.startswith("sticker") and status and kind != "sticker":
        media.emoji = status
    if kind == "unknown":
        media.title = (" ".join(sorted(c)) + " | " + title).strip(" |")
    return media


class _State:
    def __init__(self) -> None:
        self.seq = 0
        self.prev_sender: str | None = None
        self.prev_ts: dt.datetime | None = None
        self.prev_forward: dict | None = None


def _parse_service(el, fname: str, mid: int, st: _State) -> Message:
    body = child(el, "body")
    text = squash(body)
    pin_target = None
    for a in el.iter("a"):
        m = GOTO_RE.search(a.get("href", ""))
        if m:
            pin_target = int(m.group(2))
    if DATE_SEP_RE.match(text):
        stype = "date_separator"
    elif " pinned " in f" {text} ":
        stype = "pin"
    elif "set a new background" in text:
        stype = "set_background"
    else:
        stype = "other"
    actor = None
    m = re.match(r"^(.*?) (pinned|set a new background)\b", text)
    if m:
        actor = m.group(1)
    return Message(source_id=mid, html_file=fname, seq=st.seq, sender=actor, ts=st.prev_ts,
                   ts_inferred=True, kind="service", text=text, service_type=stype, pin_target=pin_target)


def _parse_default(el, fname: str, mid: int, st: _State) -> Message:
    body = child(el, "body")
    if body is None:
        raise ParseError("message without body")
    joined = "joined" in cls(el)
    date_el = child(body, "date")
    ts = parse_date(date_el.get("title", "")) if date_el is not None else None
    if ts is None:
        raise ParseError("missing or unparseable timestamp")
    fn = child(body, "from_name")
    if fn is not None:
        sender = squash(fn)
    elif joined:
        sender = st.prev_sender
    else:
        raise ParseError("no sender and not a joined message")

    msg = Message(source_id=mid, html_file=fname, seq=st.seq, sender=sender, ts=ts, joined=joined)
    rt = child(body, "reply_to")
    if rt is not None:
        a = rt.find(".//a")
        m = GOTO_RE.search(a.get("href", "")) if a is not None else None
        if not m:
            raise ParseError("unparseable reply link")
        msg.reply_to_id = int(m.group(2))

    fwd = child(body, "forwarded body")
    if fwd is not None:
        ffn = child(fwd, "from_name")
        if ffn is not None:
            span = ffn.find(".//span")
            fdate = parse_date(span.get("title", "")) if span is not None else None
            msg.forward = {"from_name": (ffn.text or "").strip() or None,
                           "date": fdate.isoformat() if fdate else None}
        else:
            prev = st.prev_forward or {}
            msg.forward = {"from_name": prev.get("from_name") if joined else None, "date": None,
                           "inherited": bool(joined and prev)}
    content = fwd if fwd is not None else body

    text_div = child(content, "text")
    if text_div is not None:
        msg.text, msg.entities = extract_text(text_div)
    wrap = child(content, "media_wrap")
    if wrap is not None:
        nodes = [n for n in wrap if isinstance(n.tag, str)]
        if len(nodes) != 1:
            raise ParseError(f"media_wrap with {len(nodes)} children")
        msg.media = classify_media(nodes[0])
        msg.kind = msg.media.kind
    if text_div is None and wrap is None:
        raise ParseError("message with neither text nor media")

    rx = child(body, "reactions")
    if rx is not None:
        for r in rx.find_class("reaction"):
            emoji = squash(r.find_class("emoji")[0]) if r.find_class("emoji") else ""
            actors = [w.get("title") for w in r.iter() if isinstance(w.tag, str) and w.get("title")]
            msg.reactions.append({"emoji": emoji, "actors": actors})

    st.prev_sender, st.prev_ts = sender, ts
    st.prev_forward = msg.forward if fwd is not None else None
    return msg


def iter_messages(root: Path, on_error: Callable[[str, int | None, str], None] | None = None,
                  on_file: Callable[[Path], None] | None = None) -> Iterator[Message]:
    """Yield messages of all pages in order. Bad blocks are reported via on_error and skipped."""
    st = _State()
    for f in html_files(root):
        if on_file:
            on_file(f)
        hist = html.parse(str(f)).getroot().find_class("history")
        if len(hist) != 1:
            if on_error:
                on_error(f.name, None, f"expected one history div, found {len(hist)}")
            continue
        for el in hist[0]:
            if not isinstance(el.tag, str) or el.tag != "div" or "message" not in cls(el):
                continue
            st.seq += 1
            try:
                mid = int(el.get("id", "")[len("message"):])
            except ValueError:
                if on_error:
                    on_error(f.name, None, f"bad message id {el.get('id')!r}")
                continue
            try:
                yield (_parse_service if "service" in cls(el) else _parse_default)(el, f.name, mid, st)
            except ParseError as e:
                if on_error:
                    on_error(f.name, mid, str(e))
