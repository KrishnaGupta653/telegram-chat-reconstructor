"""Synthetic Telegram Desktop export fixtures (structure copied from a real export, no private data)."""
from __future__ import annotations

from pathlib import Path

import pytest

PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"/><title>Exported Data</title></head>
<body><div class="page_wrap"><div class="page_header"><div class="content"><div class="text bold">
Bob
</div></div></div><div class="page_body chat_page"><div class="history">
{blocks}
</div></div></div></body></html>"""

MAGIC = {
    ".jpg": b"\xff\xd8\xff\xe0" + b"0" * 60,
    ".mp4": b"\x00\x00\x00\x18ftypmp42" + b"0" * 52,
    ".webp": b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"0" * 48,
    ".tgs": b"\x1f\x8b\x08\x00" + b"0" * 60,
    ".webm": b"\x1aE\xdf\xa3" + b"0" * 60,
    ".ogg": b"OggS" + b"0" * 60,
    ".pdf": b"%PDF-1.4" + b"0" * 56,
}


def date_sep(mid: int, text: str = "30 March 2025") -> str:
    return f'<div class="message service" id="message{mid}"><div class="body details">\n{text}\n</div></div>'


def service(mid: int, text: str, target: int | None = None) -> str:
    link = f'<a href="#go_to_message{target}" onclick="return GoToMessage({target})">this message</a>' if target else ""
    return f'<div class="message service" id="message{mid}"><div class="body details">\n{text} {link}\n</div></div>'


def msg(mid: int, ts: str, sender: str | None = None, text: str | None = None, media: str = "",
        reply: str | None = None, reactions: str = "", forward: str | None = None) -> str:
    joined = sender is None
    parts = [f'<div class="pull_right date details" title="{ts} UTC+05:30">\n{ts[11:16]}\n</div>']
    if sender is not None:
        parts.append(f'<div class="from_name">\n{sender} \n</div>')
    if reply:
        parts.append(f'<div class="reply_to details">\nIn reply to <a href="{reply}" onclick="return GoToMessage(1)">this message</a>\n</div>')
    inner = ""
    if media:
        inner += f'<div class="media_wrap clearfix">\n{media}\n</div>'
    if text is not None:
        inner += f'<div class="text">\n{text}\n       </div>'
    if forward is not None:
        parts.append(f'<div class="forwarded body">{forward}{inner}</div>')
    else:
        parts.append(inner)
    parts.append(reactions)
    cls = "message default clearfix joined" if joined else "message default clearfix"
    pic = "" if joined else '<div class="pull_left userpic_wrap"><div class="userpic userpic1"><div class="initials">A</div></div></div>'
    return f'<div class="{cls}" id="message{mid}">{pic}<div class="body">{"".join(parts)}</div></div>'


def photo(href: str) -> str:
    return f'<a class="photo_wrap clearfix pull_left" href="{href}"><img class="photo" src="{href[:-4]}_thumb.jpg"/></a>'


def block_media(cls: str, href: str | None, title: str, status: str) -> str:
    tag = "a" if href else "div"
    h = f' href="{href}"' if href else ""
    return (f'<{tag} class="media clearfix pull_left {cls}"{h}><div class="fill pull_left"></div><div class="body">'
            f'<div class="title bold">\n{title}\n</div><div class="status details">\n{status}\n</div></div></{tag}>')


def reaction(emoji: str, who: str) -> str:
    return (f'<span class="reactions"><span class="reaction"><span class="emoji">\n{emoji}\n</span><span class="userpics">'
            f'<div class="userpic userpic1"><div class="initials" title="{who}">X</div></div></span></span></span>')


@pytest.fixture
def make_export(tmp_path: Path):
    def build(pages: list[list[str]], files: list[str] = ()) -> Path:
        root = tmp_path / f"export{len(list(tmp_path.glob('export*')))}"
        root.mkdir()
        for i, blocks in enumerate(pages, 1):
            name = "messages.html" if i == 1 else f"messages{i}.html"
            (root / name).write_text(PAGE.format(blocks="\n".join(blocks)), encoding="utf-8")
        for rel in files:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(MAGIC[p.suffix])
        return root
    return build
