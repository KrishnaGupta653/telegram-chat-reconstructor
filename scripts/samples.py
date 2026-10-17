"""Print one-or-two raw HTML samples of each interesting message block type.

Usage: python scripts/samples.py <export_dir>
"""
import collections
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
files = sorted(root.glob("messages*.html"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 1))

MARKERS = [
    "media_photo", "media_video", "video_file_wrap", "animated_wrap", "sticker_wrap",
    "photo_wrap", "media_call", "media_file", "media_voice_message", "media_audio_file",
    "forwarded body", "reactions", "Not included", "media_contact", "media_location", "media_poll",
    "<strong>", "<em>", "<code>", "<pre>", "<s>", "<u>", "spoiler", "blockquote", "edited",
]
want = 2
found = collections.defaultdict(list)
status_texts = collections.Counter()
titles = collections.Counter()
block_re = re.compile(r'(<div class="message [^"]+" id="message-?\d+">.*?)(?=<div class="message |\Z)', re.S)

for f in files:
    s = f.read_text(encoding="utf-8")
    for m in re.finditer(r'<div class="title bold">\s*(.*?)\s*</div>', s, re.S):
        titles[re.sub(r"\s+", " ", m.group(1))[:50]] += 1
    for m in re.finditer(r'<div class="status details">\s*(.*?)\s*</div>', s, re.S):
        status_texts[re.sub(r"[\d.:]+", "#", re.sub(r"\s+", " ", m.group(1)))[:70]] += 1
    if all(len(found[k]) >= want for k in MARKERS):
        continue
    for b in block_re.finditer(s):
        block = b.group(1)
        for k in MARKERS:
            if len(found[k]) < want and k in block:
                found[k].append((f.name, re.sub(r"\n\s*\n", "\n", block)[:2200]))

print("MEDIA TITLES", titles.most_common(40))
print("\nSTATUS TEXTS", status_texts.most_common(40))
for k in MARKERS:
    print(f"\n{'=' * 20} {k} ({len(found[k])})")
    for name, block in found[k]:
        print(f"--- {name}\n{block}")
