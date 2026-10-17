"""Estimate the size of a WhatsApp-style import text file built from the export (read-only).

Usage: python scripts/size_estimate.py <export_dir>
"""
import html
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1])
files = sorted(ROOT.glob("messages*.html"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 1))
PREFIX = len("[30/03/2025, 17:50:50] Firstname (Nickname): ")
text_bytes = msgs = 0
for f in files:
    s = f.read_text(encoding="utf-8")
    msgs += len(re.findall(r'class="pull_right date details"', s))
    for m in re.finditer(r'<div class="text">\s*(.*?)\s*</div>', s, re.S):
        t = html.unescape(re.sub(r"<br\s*/?>", "\n", m.group(1)))
        text_bytes += len(re.sub(r"<[^>]+>", "", t).encode("utf-8"))
total = text_bytes + msgs * (PREFIX + 1)
print(f"messages={msgs} text_utf8={text_bytes / 2**20:.1f} MiB est_file={total / 2**20:.1f} MiB (before reply/context annotations)")
