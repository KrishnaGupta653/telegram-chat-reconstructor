"""Read-only probe of media properties relevant to Telegram upload (codec, size, duplicates).

Usage: python scripts/media_probe.py <export_dir>
"""
import collections
import hashlib
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1])

for p in sorted((ROOT / "voice_messages").glob("*.ogg"))[:3]:
    head = p.read_bytes()[:64]
    print("voice", p.name, "opus" if b"OpusHead" in head else "vorbis" if b"vorbis" in head else head[28:40])

files = [p for d in ("photos", "video_files", "files", "voice_messages", "stickers") for p in (ROOT / d).iterdir()
         if p.is_file() and "_thumb" not in p.name]
big = sorted(files, key=lambda p: p.stat().st_size, reverse=True)[:5]
print("largest:", [(p.parent.name, p.suffix, round(p.stat().st_size / 2**20, 1)) for p in big])
by_ext = collections.Counter((p.parent.name, p.suffix.lower()) for p in files)
print("primary files by dir/ext:", sorted(by_ext.items()))

hashes = collections.defaultdict(list)
for p in files:
    hashes[hashlib.sha256(p.read_bytes()).hexdigest()].append(p)
dupes = [v for v in hashes.values() if len(v) > 1]
print("content-duplicate groups:", len(dupes), "files involved:", sum(len(v) for v in dupes))
print("dupe groups by dir:", collections.Counter(v[0].parent.name for v in dupes))
