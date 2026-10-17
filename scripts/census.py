"""Read-only forensic census of a Telegram Desktop HTML export.

Extends the original regex census (census_v1.py) with per-message parsing via lxml.
Never writes to the export. Output contains counts, structure and redacted samples only.

Usage: python scripts/census.py <export_dir> [out_dir]
"""
import collections
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import urllib.parse

from lxml import html

ROOT = pathlib.Path(sys.argv[1]).resolve()
OUT = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else pathlib.Path(__file__).parent.parent / "census")
OUT.mkdir(parents=True, exist_ok=True)

DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2}):(\d{2}) UTC([+-])(\d{2}):(\d{2})$")
GOTO_RE = re.compile(r"^(messages\d*\.html)?#go_to_message(\d+)$")
DATE_SEP_RE = re.compile(r"^\d{1,2} \w+ \d{4}$")
MAGIC = [
    (b"\xff\xd8\xff", "jpeg"), (b"\x89PNG", "png"), (b"\x1aE\xdf\xa3", "webm/mkv"), (b"OggS", "ogg"),
    (b"\x1f\x8b", "gzip(tgs)"), (b"%PDF", "pdf"), (b"PK\x03\x04", "zip(docx)"), (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"), (b"\xff\xf3", "mp3"), (b"GIF8", "gif"),
]


def page_no(p):
    return int(re.sub(r"\D", "", p.stem) or 1)


def cls(el):
    return set((el.get("class") or "").split())


def text_of(el):
    return re.sub(r"\s+", " ", el.text_content()).strip() if el is not None else ""


def child(el, klass):
    """First direct child div whose class list equals/contains all of `klass`."""
    want = set(klass.split())
    for c in el:
        if want <= cls(c):
            return c
    return None


def parse_date(title):
    m = DATE_RE.match(title.split("\n")[0].strip())
    if not m:
        return None
    d, mo, y, h, mi, s, sign, oh, om = m.groups()
    off = dt.timedelta(hours=int(oh), minutes=int(om)) * (1 if sign == "+" else -1)
    return dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=dt.timezone(off))


def sniff(path):
    with open(path, "rb") as f:
        head = f.read(16)
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    for sig, name in MAGIC:
        if head.startswith(sig):
            return name
    return "unknown"


def classify_media(node):
    c = cls(node)
    title = text_of(node.find_class("title")[0]) if node.find_class("title") else ""
    status = text_of(node.find_class("status")[0]) if node.find_class("status") else ""
    if "photo_wrap" in c:
        return "photo", title, status
    if "video_file_wrap" in c:
        return "video", title, status
    if "animated_wrap" in c:
        return "gif", title, status
    if "sticker_wrap" in c:
        return "sticker_static", title, status
    if "media_photo" in c:
        return {"Sticker": "sticker_nonimage", "Self-destructing photo": "ttl_photo"}.get(title, "photo_other"), title, status
    if "media_video" in c:
        return {"Animation": "gif_as_file", "Self-destructing video": "ttl_video"}.get(title, "video_other"), title, status
    for k, name in [("media_voice_message", "voice"), ("media_audio_file", "audio"), ("media_file", "document"),
                    ("media_call", "call"), ("media_contact", "contact"), ("media_location", "location"),
                    ("media_live_location", "live_location"), ("media_venue", "venue"), ("media_poll", "poll"),
                    ("media_game", "game"), ("media_invoice", "invoice")]:
        if k in c:
            return name, title, status
    return "unknown:" + " ".join(sorted(c)), title, status


def resolve(ref):
    p = (ROOT / urllib.parse.unquote(ref)).resolve()
    return p, (ROOT in p.parents and p.is_file())


files = sorted(ROOT.glob("messages*.html"), key=page_no)
c = collections.Counter
stats = dict(types=c(), media_kind=c(), media_title=c(), status=c(), senders=c(), sender_by_type=c(),
             service=c(), body_children=c(), text_tags=c(), link_schemes=c(), date_shapes=c(),
             reactions_emoji=c(), reaction_actor=c(), call_status=c(), forward_from=c(), problems=c())
per_file, samples, msgs = [], {}, []
ids_seen = c()
media_refs = collections.defaultdict(list)  # resolved path -> [(msg_id, role)]
unresolvable = []
prev_sender = None

for f in files:
    tree = html.parse(str(f))
    hist = tree.getroot().find_class("history")
    if len(hist) != 1:
        stats["problems"]["history_div_count!=1:" + f.name] += 1
        continue
    first_id = last_id = None
    n = 0
    joined_at_file_start = None
    for el in hist[0]:
        if el.tag != "div" or "message" not in cls(el):
            if el.tag == "a" or "pagination" in cls(el):
                continue
            stats["problems"]["non_message_in_history:" + el.tag + ":" + " ".join(sorted(cls(el)))] += 1
            continue
        mid = int(el.get("id", "message0")[7:])
        ids_seen[mid] += 1
        n += 1
        if mid > 0:
            first_id = first_id or mid
            last_id = mid
        if "service" in cls(el):
            t = text_of(child(el, "body"))
            if DATE_SEP_RE.match(t):
                kind = "date_separator"
            elif "pinned" in t:
                kind = "pin"
            elif "background" in t:
                kind = "set_background"
            else:
                kind = "other:" + re.sub(r"\d+", "#", t)[:60]
            stats["service"][kind] += 1
            target = None
            for a in el.iter("a"):
                m = GOTO_RE.match(a.get("href", ""))
                if m:
                    target = int(m.group(2))
            msgs.append(dict(id=mid, file=f.name, service=kind, pin_target=target))
            continue

        body = child(el, "body")
        joined = "joined" in cls(el)
        if joined_at_file_start is None:
            joined_at_file_start = joined
        for ch in body:
            stats["body_children"][" ".join(sorted(cls(ch))) or ch.tag] += 1
        date_el = child(body, "date")
        title = date_el.get("title", "") if date_el is not None else ""
        stats["date_shapes"][re.sub(r"\d", "#", title)] += 1
        ts = parse_date(title)
        fn = child(body, "from_name")
        if fn is not None:
            sender = text_of(fn)
            prev_sender = sender
        elif joined:
            sender = prev_sender
        else:
            sender = None
            stats["problems"]["no_sender_not_joined"] += 1

        rec = dict(id=mid, file=f.name, sender=sender, ts=ts.isoformat() if ts else None, joined=joined)
        rt = child(body, "reply_to")
        if rt is not None:
            a = rt.find(".//a")
            m = GOTO_RE.match(a.get("href", "")) if a is not None else None
            rec["reply_to"] = int(m.group(2)) if m else "unparsed"
            rec["reply_cross_file"] = bool(m and m.group(1))
        fwd = child(body, "forwarded body")
        if fwd is not None:
            ffn = child(fwd, "from_name")
            span = ffn.find(".//span") if ffn is not None else None
            name = (ffn.text or "").strip() if ffn is not None else None
            rec["forward"] = dict(from_name=name, date=span.get("title") if span is not None else None)
            stats["forward_from"][name] += 1
        content = fwd if fwd is not None else body

        texts = [t for t in content.iter("div") if cls(t) == {"text"}]
        if texts:
            t = texts[0]
            raw = text_of(t)
            rec["text_len"] = len(t.text_content().strip())
            rec["non_bmp"] = any(ord(ch) > 0xFFFF for ch in t.text_content())
            for sub in t.iter():
                if sub is t:
                    continue
                stats["text_tags"][(sub.tag + "." + ".".join(sorted(cls(sub)))).rstrip(".")] += 1
                if sub.tag == "a":
                    href = sub.get("href", "")
                    stats["link_schemes"][href.split(":")[0] if ":" in href else ("mention/other:" + href[:1])] += 1
        mw = [m for m in content.iter("div") if "media_wrap" in cls(m)]
        media = []
        for w in mw:
            for node in w:
                kind, mtitle, status = classify_media(node)
                href, src = node.get("href"), None
                img = node.find(".//img")
                if img is not None:
                    src = img.get("src")
                stats["media_kind"][kind] += 1
                stats["media_title"][(kind, mtitle if kind in ("sticker_nonimage", "ttl_photo", "ttl_video",
                                                                "photo_other", "video_other", "gif_as_file") else "")] += 1
                if kind == "call":
                    stats["call_status"][re.sub(r"\d+", "#", status)] += 1
                elif status and "Not included" in status:
                    stats["problems"]["media_not_included:" + kind] += 1
                elif status in ("Expired",):
                    stats["status"][(kind, status)] += 1
                m = dict(kind=kind, href=href, thumb=src)
                for role, ref in (("file", href), ("thumb", src)):
                    if not ref:
                        continue
                    if re.match(r"^[a-z]+:", ref) or ref.startswith("#"):
                        continue
                    p, ok = resolve(ref)
                    if ok:
                        media_refs[p].append((mid, role, kind))
                    else:
                        unresolvable.append((f.name, mid, role, kind, ref))
                        m[role + "_missing"] = True
                media.append(m)
        rec["media"] = media
        rx = el.find_class("reactions")
        if rx:
            rec["reactions"] = 0
            for r in rx[0].find_class("reaction"):
                e = text_of(r.find_class("emoji")[0]) if r.find_class("emoji") else "?"
                stats["reactions_emoji"][e] += 1
                rec["reactions"] += 1
                for who in r.iter():
                    if who.get("title"):
                        stats["reaction_actor"][who.get("title")] += 1
        if not texts and not media:
            stats["problems"]["empty_message(no text, no media)"] += 1

        mtype = media[0]["kind"] if media else "text"
        if len(media) > 1:
            stats["problems"]["multi_media_in_one_message"] += 1
        rec["type"] = mtype
        stats["types"][mtype] += 1
        stats["senders"][sender] += 1
        stats["sender_by_type"][(sender, mtype)] += 1
        samples.setdefault(mtype, dict(id=mid, file=f.name, sender=sender, ts=rec["ts"],
                                       text_len=rec.get("text_len"), media=[dict(kind=x["kind"], href=x["href"]) for x in media]))
        msgs.append(rec)
    per_file.append(dict(file=f.name, bytes=f.stat().st_size, blocks=n, first_id=first_id, last_id=last_id,
                         joined_at_start=joined_at_file_start))

# ---- Derived checks -------------------------------------------------------
real = [m for m in msgs if "service" not in m]
ids = [m["id"] for m in msgs if m["id"] > 0]
id_set = set(ids)
order_breaks = sum(1 for a, b in zip(ids, ids[1:]) if b <= a)
ts_list = [(m["id"], m["ts"]) for m in real if m["ts"]]
ts_inversions = sum(1 for a, b in zip(ts_list, ts_list[1:]) if dt.datetime.fromisoformat(b[1]) < dt.datetime.fromisoformat(a[1]))
same_second = sum(1 for a, b in zip(ts_list, ts_list[1:]) if a[1] == b[1])
gaps = [b - a - 1 for a, b in zip(ids, ids[1:]) if b - a > 1]
file_bounds = [(p["first_id"], p["last_id"]) for p in per_file]
file_overlap = sum(1 for a, b in zip(file_bounds, file_bounds[1:]) if b[0] <= a[1])

replies = [m for m in real if "reply_to" in m]
reply_targets_found = sum(1 for m in replies if m["reply_to"] in id_set)
reply_target_before = sum(1 for m in replies if isinstance(m["reply_to"], int) and m["reply_to"] < m["id"])
pins = [m for m in msgs if m.get("service") == "pin"]
pin_targets_found = sum(1 for m in pins if m["pin_target"] in id_set)

# album candidates: consecutive photo/video messages, same sender, same second
visual = {"photo", "video"}
groups, cur = [], []
for m in real:
    if m["type"] in visual and cur and m["sender"] == cur[-1]["sender"] and m["ts"] == cur[-1]["ts"]:
        cur.append(m)
    else:
        if len(cur) > 1:
            groups.append(cur)
        cur = [m] if m["type"] in visual else []
if len(cur) > 1:
    groups.append(cur)

# media resolution / sniff / hash
hash_of, sniffed, sizes = {}, c(), {}
for p in media_refs:
    sizes[p] = p.stat().st_size
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    hash_of[p] = h.hexdigest()
    sniffed[(p.suffix.lower(), sniff(p))] += 1
primary = {p: refs for p, refs in media_refs.items() if any(r[1] == "file" for r in refs)}
reuse = c(len([r for r in refs if r[1] == "file"]) for refs in primary.values())
by_hash = collections.defaultdict(list)
for p in primary:
    by_hash[hash_of[p]].append(p)
content_dupes = {h: ps for h, ps in by_hash.items() if len(ps) > 1}
zero_byte = [str(p.relative_to(ROOT)) for p in media_refs if sizes[p] == 0]
on_disk = {p.resolve() for d in ROOT.iterdir() if d.is_dir() and d.name not in ("css", "js", "images")
           for p in d.rglob("*") if p.is_file()}
orphans = sorted(on_disk - set(media_refs))
orphan_ext = c((p.parent.name, p.suffix.lower()) for p in orphans)
missing_by_kind = c((u[3], u[2]) for u in unresolvable)
kind_bytes = c()
for p, refs in primary.items():
    kind_bytes[refs[0][2]] += sizes[p]

first, last = ts_list[0], ts_list[-1]
report = dict(
    export=str(ROOT), html_files=len(files), html_bytes=sum(p["bytes"] for p in per_file),
    message_blocks=len(msgs), real_messages=len(real), service_entries=len(msgs) - len(real),
    duplicate_ids=sum(1 for v in ids_seen.values() if v > 1), positive_id_range=[min(ids), max(ids)],
    id_order_breaks=order_breaks, id_gaps=dict(count=len(gaps), missing_ids=sum(gaps), largest=max(gaps or [0])),
    file_id_overlaps=file_overlap, files_starting_with_joined=sum(1 for p in per_file if p["joined_at_start"]),
    first_message=first, last_message=last, timestamp_inversions=ts_inversions, consecutive_same_second=same_second,
    date_title_shapes=stats["date_shapes"].most_common(), types=stats["types"].most_common(),
    media_kinds=stats["media_kind"].most_common(), media_titles=[[list(k), v] for k, v in stats["media_title"].most_common()],
    expired_status=[[list(k), v] for k, v in stats["status"].items()], call_status=stats["call_status"].most_common(),
    senders=stats["senders"].most_common(), sender_by_type=[[list(k), v] for k, v in stats["sender_by_type"].most_common()],
    service=stats["service"].most_common(), pins=dict(total=len(pins), target_in_export=pin_targets_found),
    replies=dict(total=len(replies), target_in_export=reply_targets_found, target_earlier=reply_target_before,
                 cross_file=sum(1 for m in replies if m["reply_cross_file"]),
                 unparsed=sum(1 for m in replies if m["reply_to"] == "unparsed")),
    forwards=dict(total=sum(1 for m in real if "forward" in m), from_names=stats["forward_from"].most_common()),
    reactions=dict(messages=sum(1 for m in real if "reactions" in m), emoji=stats["reactions_emoji"].most_common(15),
                   actors=stats["reaction_actor"].most_common()),
    text=dict(messages_with_text=sum(1 for m in real if "text_len" in m),
              with_non_bmp_emoji=sum(1 for m in real if m.get("non_bmp")),
              max_len=max((m.get("text_len") or 0) for m in real),
              over_4096=sum(1 for m in real if (m.get("text_len") or 0) > 4096),
              tags=stats["text_tags"].most_common(), link_schemes=stats["link_schemes"].most_common()),
    album_candidates=dict(groups=len(groups), messages=sum(len(g) for g in groups),
                          sizes=c(len(g) for g in groups).most_common()),
    media_files=dict(referenced_unique=len(media_refs), primary_unique=len(primary),
                     primary_bytes=sum(sizes[p] for p in primary), bytes_by_kind=kind_bytes.most_common(),
                     reuse_histogram=sorted(reuse.items())[:15], max_reuse=max(reuse) if reuse else 0,
                     ext_vs_magic=[[list(k), v] for k, v in sniffed.most_common()],
                     content_duplicate_groups=len(content_dupes), zero_byte=zero_byte,
                     unresolvable=len(unresolvable), unresolvable_by_kind=[[list(k), v] for k, v in missing_by_kind.items()],
                     unresolvable_examples=unresolvable[:10], orphans=len(orphans),
                     orphans_by_dir_ext=[[list(k), v] for k, v in orphan_ext.most_common()],
                     orphan_examples=[str(p.relative_to(ROOT)) for p in orphans[:10]]),
    body_children=stats["body_children"].most_common(), problems=stats["problems"].most_common(),
    per_file=per_file, redacted_samples=samples,
)
(OUT / "census_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
skip = {"per_file", "redacted_samples", "sender_by_type"}
print(json.dumps({k: v for k, v in report.items() if k not in skip}, ensure_ascii=False, indent=1, default=str))
