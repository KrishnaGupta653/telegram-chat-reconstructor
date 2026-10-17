"""Read-only probe of specific edge cases found by census.py. Text content is redacted.

Usage: python scripts/edge_probe.py <export_dir>
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1])
files = sorted(ROOT.glob("messages*.html"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 1))


def red(s):
    s = re.sub(r'(<div class="text">)(.*?)(</div>)', lambda m: f"{m.group(1)}[REDACTED {len(m.group(2))} chars]{m.group(3)}", s, flags=re.S)
    return re.sub(r"\s+", " ", s)


limits = {"fwd": 8, "photo_other": 3, "link": 12}
seen = dict.fromkeys(limits, 0)
for f in files:
    s = f.read_text(encoding="utf-8")
    for m in re.finditer(r'<div class="forwarded body">(.{0,500})', s, re.S):
        if seen["fwd"] < limits["fwd"]:
            seen["fwd"] += 1
            print("FWD", f.name, red(m.group(1))[:260])
    for m in re.finditer(r'<a class="([^"]+)" href="([^"]+)">\s*<div class="fill pull_left">\s*</div>\s*<div class="body">\s*<div class="title bold">\s*Photo\s*</div>\s*<div class="status details">\s*(.*?)\s*</div>', s, re.S):
        if seen["photo_other"] < limits["photo_other"]:
            seen["photo_other"] += 1
            print("PHOTO_OTHER", f.name, m.group(1), m.group(2), "status=", m.group(3))
    for m in re.finditer(r'Self-destructing video\s*</div>\s*<div class="status details">\s*(.*?)\s*</div>', s, re.S):
        if m.group(1) != "Expired":
            print("TTL_VIDEO_STATUS", f.name, repr(m.group(1)))
    for m in re.finditer(r'<a href="([^"]*)"[^>]*>([^<]{0,60})</a>', s):
        href = m.group(1)
        if ":" in href or href.startswith(("#", "messages", "photos", "files", "video", "stickers", "voice", "css")):
            continue
        if seen["link"] < limits["link"]:
            seen["link"] += 1
            print("NOSCHEME_LINK", f.name, f"href_len={len(href)} href_first={href[:1]!r} text_is_href={m.group(2).strip() == href}")
