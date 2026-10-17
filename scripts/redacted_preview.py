"""Show the structure of a generated chat file with all message text redacted.

Usage: python scripts/redacted_preview.py <chat_XXX.txt> [n_examples]
"""
import collections
import re
import sys

path = sys.argv[1]
n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
HEAD = re.compile(r"^(\[?\d{2}/\d{2}/\d{4}, [\d:]+\]?(?: -)?) ([^:]+): (.*)$")
NOTE = re.compile(r"^\u200e?(\[[^\]:]+?(?::|\]))")
kinds = collections.Counter()
examples = collections.defaultdict(list)
for line in open(path, encoding="utf-8"):
    line = line.rstrip("\n")
    m = HEAD.match(line)
    rest = m.group(3) if m else line
    if "<attached:" in rest or "(file attached)" in rest:
        k, shown = "attachment", rest
    elif rest.startswith("["):
        label = NOTE.match(rest)
        k = "note:" + (label.group(1)[:30] if label else "?")
        shown = label.group(1) + " …" if label else "[…"
    else:
        k, shown = "text", f"<{len(rest)} chars>"
    k = ("head " if m else "cont ") + k
    kinds[k] += 1
    if len(examples[k]) < n:
        examples[k].append((m.group(1) + " " + m.group(2) + ": " if m else "") + shown)
for k, c in kinds.most_common():
    print(f"{c:8}  {k}")
    for e in examples[k]:
        print("            ", e)
