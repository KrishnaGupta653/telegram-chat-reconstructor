"""One-off read-only census of a Telegram Desktop HTML export.

Usage: python scripts/census.py <export_dir>
"""
import collections
import pathlib
import re
import sys
import urllib.parse

root = pathlib.Path(sys.argv[1])
files = sorted(root.glob("messages*.html"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 1))

classes = collections.Counter()
ids = collections.Counter()
hrefs = collections.Counter()
missing = []
tz = collections.Counter()
from_names = collections.Counter()
service = collections.Counter()
first_ts = last_ts = None
order_breaks = 0
cross_file_replies = 0
prev_id = None

for f in files:
    s = f.read_text(encoding="utf-8")
    for m in re.finditer(r'class="([^"]+)"', s):
        classes[m.group(1)] += 1
    for m in re.finditer(r'<div class="message ([^"]+)" id="message(-?\d+)"', s):
        mid = int(m.group(2))
        ids[mid] += 1
        if mid > 0:
            if prev_id is not None and mid <= prev_id:
                order_breaks += 1
            prev_id = mid
    for m in re.finditer(r'href="([^"#][^"]*)"', s):
        h = m.group(1)
        if h.startswith(("http", "mailto", "tg:", "css/", "js/")):
            hrefs[h.split(":")[0]] += 1
            continue
        if h.startswith("messages"):
            hrefs["messages*.html"] += 1
            continue
        hrefs[h.split("/")[0]] += 1
        if not (root / urllib.parse.unquote(h)).exists():
            missing.append((f.name, h))
    cross_file_replies += len(re.findall(r'href="messages\d*\.html#go_to_message', s))
    for m in re.finditer(r'class="pull_right date details" title="([^"]+)"', s):
        first_ts = first_ts or m.group(1)
        last_ts = m.group(1)
        tz[m.group(1).split(" ", 2)[-1]] += 1
    for m in re.finditer(r'<div class="from_name">\s*(.*?)\s*</div>', s, re.S):
        from_names[re.sub(r"<.*?>", "", m.group(1)).strip()[:60]] += 1
    for m in re.finditer(
        r'<div class="message service" id="message-?\d+">\s*<div class="body details">\s*(.*?)\s*</div>', s, re.S
    ):
        text = re.sub(r"<.*?>", "", m.group(1)).strip()[:80]
        if not re.match(r"^\d+ \w+ \d{4}$", text):
            service[text] += 1

pos = [i for i in ids if i > 0]
print("html files", len(files))
print("message divs", sum(ids.values()), "unique ids", len(ids), "positive", len(pos),
      "duplicate ids", sum(1 for c in ids.values() if c > 1))
print("id range", min(pos), max(pos))
print("order breaks", order_breaks, "cross-file reply links", cross_file_replies)
print("first", first_ts, "| last", last_ts, "| tz", tz.most_common(5))
print("\nCLASSES")
for k, c in classes.most_common(150):
    print(f"  {c:8} {k}")
print("\nHREF TOPS", hrefs.most_common(30))
print("\nMISSING", len(missing), missing[:20])
print("\nFROM", from_names.most_common(15))
print("\nSERVICE (non-date)", service.most_common(40))
