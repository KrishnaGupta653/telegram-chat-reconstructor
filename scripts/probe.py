"""Read-only probe for remaining export features (edits, custom emoji, reactions, forwards, pins).

Usage: python scripts/probe.py <export_dir>
"""
import collections
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
files = sorted(root.glob("messages*.html"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 1))

date_title_shapes = collections.Counter()
reaction_actors = collections.Counter()
reaction_kinds = collections.Counter()
forward_from = collections.Counter()
needles = collections.Counter()
pin_links = 0
joined_without_prev_sender = 0
NEEDLES = ["Edited", "edited", "via @", "custom_emoji", "spoiler", "blockquote", "Deleted Account",
           "media_poll", "media_contact", "media_location", "media_game", "media_invoice", "<pre",
           "<u>", "<blockquote", "class=\"reply_to details\">\nIn reply to this message", "Not included"]

for f in files:
    s = f.read_text(encoding="utf-8")
    for m in re.finditer(r'class="pull_right date details" title="([^"]+)"', s):
        date_title_shapes[re.sub(r"\d", "#", m.group(1))] += 1
    for m in re.finditer(r'<span class="reaction( active)?">\s*<span class="emoji">\s*(.*?)\s*</span>\s*<span class="userpics">(.*?)</span>', s, re.S):
        reaction_kinds["img" if "<img" in m.group(2) else "emoji"] += 1
        for who in re.findall(r'title="([^"]+)"', m.group(3)):
            reaction_actors[(who, bool(m.group(1)))] += 1
    for m in re.finditer(r'<div class="forwarded body">\s*<div class="from_name">\s*(.*?)<span', s, re.S):
        forward_from[m.group(1).strip()] += 1
    pin_links += len(re.findall(r'pinned <a href="[^"]*#go_to_message\d+"', s))
    for n in NEEDLES:
        needles[n] += s.count(n)

print("DATE TITLE SHAPES", date_title_shapes.most_common(5))
print("REACTION KINDS", reaction_kinds)
print("REACTION ACTORS (name, active)", reaction_actors.most_common(10))
print("FORWARDED FROM", forward_from)
print("PIN EVENTS LINKING TO A MESSAGE", pin_links)
print("NEEDLES", {k: v for k, v in needles.items()})
