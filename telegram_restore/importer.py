"""Native Telegram history import: init -> upload media -> start, resumable from SQLite.

Nothing is visible in Telegram until messages.startHistoryImport succeeds; every step before it
can be retried or abandoned without leaving anything in the chat. A run is never started twice.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn
from telethon import errors, types, utils
from telethon.tl import functions

from . import database
from .config import Config
from .dryrun import build, load_messages
from .media import resolve
from .models import MEDIA_KINDS_WITH_FILE, NOTE_ONLY_KINDS, Message
from .render import RenderOptions
from .telegram_api import FloodStop, call_with_flood, make_client

console = Console()
UNFINISHED = ("prepared", "uploading", "starting")


class StopImport(Exception):
    """Stop now; state is saved and `import --resume` continues later."""


@dataclass
class Stats:
    uploaded: int = 0
    reused: int = 0
    uploaded_bytes: int = 0
    flood_waits: list[int] = field(default_factory=list)
    reuse_disabled: str | None = None


def select_test_messages(messages: list[Message], head: int = 10) -> list[Message]:
    """A small chronological sample covering every content type and annotation in the export."""
    real = [m for m in messages if not m.is_service and m.ts and m.sender]
    ids = {m.source_id for m in messages}
    picks: dict[int, Message] = {m.source_id: m for m in real[:head]}

    def first(pred) -> None:
        m = next((m for m in messages if pred(m)), None)
        if m:
            picks[m.source_id] = m

    for kind in sorted(MEDIA_KINDS_WITH_FILE | NOTE_ONLY_KINDS):
        first(lambda m, k=kind: m.kind == k and m.ts and (k not in MEDIA_KINDS_WITH_FILE or m.media.exists))
    first(lambda m: m.reply_to_id in ids and m.kind == "text")
    first(lambda m: m.reply_to_id and m.reply_to_id not in ids)
    first(lambda m: m.reactions)
    for actor in sorted({a for m in messages for r in m.reactions for a in r["actors"]}):
        first(lambda m, a=actor: m.kind == "text" and any(a in r["actors"] for r in m.reactions))
    first(lambda m: m.forward)
    by_id = {m.source_id: m for m in messages}
    for actor in sorted({m.sender for m in messages if m.service_type == "pin"}):
        pin = next((m for m in messages if m.service_type == "pin" and m.sender == actor and m.pin_target in by_id
                    and by_id[m.pin_target].ts), None)
        if pin:
            picks[pin.source_id] = pin
            picks[pin.pin_target] = by_id[pin.pin_target]
    first(lambda m: any(e.type != "url" for e in m.entities))
    first(lambda m: "\n" in m.text)
    first(lambda m: "\n\n" in m.text and len(m.text) < 300)
    first(lambda m: len(m.text) > 3000 and "\n\n" not in m.text)
    first(lambda m: len(m.text.encode("utf-16-le")) // 2 > 4096)
    first(lambda m: m.kind == "photo" and not m.text and not m.reactions and not m.group_key)
    first(lambda m: m.kind == "photo" and m.text and m.media.exists)
    group = next((m.group_key for m in messages if m.group_key), None)
    for m in messages:
        if group and m.group_key == group:
            picks[m.source_id] = m
    return sorted(picks.values(), key=lambda m: m.seq)


def tgs_to_webm(src: Path, sha: str, cache_dir: Path) -> Path:
    """Render an animated .tgs sticker to a VP9+alpha video sticker (<=3 s, 30 fps), once per file.

    History import drops .tgs and GIF uploads but accepts .webm video stickers.
    """
    out = cache_dir / f"{sha}.webm"
    if out.exists():
        return out
    import subprocess
    import tempfile

    import imageio_ffmpeg
    from rlottie_python import LottieAnimation

    cache_dir.mkdir(parents=True, exist_ok=True)
    anim = LottieAnimation.from_tgs(str(src))
    total, fps = anim.lottie_animation_get_totalframe(), anim.lottie_animation_get_framerate()
    frames = max(1, int(min(total / fps, 3.0) * 30))
    tmp = out.with_suffix(".tmp.webm")
    with tempfile.TemporaryDirectory() as d:
        for i in range(frames):
            anim.render_pillow_frame(frame_num=min(int(i * fps / 30), total - 1)).save(f"{d}/{i:04d}.png")
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-framerate", "30",
                        "-i", f"{d}/%04d.png", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0",
                        "-crf", "40", "-an", "-f", "webm", str(tmp)], check=True, capture_output=True)
    tmp.replace(out)
    return out


def _attributes(spec: dict, path: Path) -> list:
    attrs = []
    for a in spec.get("attributes", []):
        t = a["type"]
        if t == "filename":
            attrs.append(types.DocumentAttributeFilename(a["name"]))
        elif t == "video":
            w, h, duration = a.get("w", 0), a.get("h", 0), a.get("duration", 0)
            if not w:  # real dimensions/duration via hachoir when available
                probed, _ = utils.get_attributes(str(path), mime_type=spec["mime"])
                vid = next((x for x in probed if isinstance(x, types.DocumentAttributeVideo)), None)
                if vid:
                    w, h, duration = vid.w, vid.h, duration or vid.duration
            attrs.append(types.DocumentAttributeVideo(duration=duration, w=w or 1, h=h or 1,
                                                      supports_streaming=a.get("supports_streaming"),
                                                      nosound=a.get("nosound")))
        elif t == "audio":
            attrs.append(types.DocumentAttributeAudio(duration=a.get("duration", 0), voice=a.get("voice"),
                                                      title=a.get("title")))
        elif t == "animated":
            attrs.append(types.DocumentAttributeAnimated())
        elif t == "sticker":
            attrs.append(types.DocumentAttributeSticker(alt=a.get("alt", ""), stickerset=types.InputStickerSetEmpty()))
    return attrs


def _reusable(media) -> object | None:
    """InputMedia pointing at an already-uploaded photo/document, or None."""
    if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
        p = media.photo
        return types.InputMediaPhoto(types.InputPhoto(p.id, p.access_hash, p.file_reference))
    if isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
        d = media.document
        return types.InputMediaDocument(types.InputDocument(d.id, d.access_hash, d.file_reference))
    return None


class Importer:
    def __init__(self, cfg: Config, client, peer, conn, max_flood_wait: int) -> None:
        self.cfg, self.client, self.peer, self.conn = cfg, client, peer, conn
        self.max_flood_wait = max_flood_wait
        self.stats = Stats()
        self.by_sha: dict[str, object] = {}

    async def call(self, make_coro):
        """Run one Telegram request; wait out short FLOOD_WAITs a bounded number of times."""
        try:
            return await call_with_flood(make_coro, self.max_flood_wait, self.stats.flood_waits)
        except FloodStop as e:
            raise StopImport(f"Telegram asked to wait {e.seconds}s (limit {self.max_flood_wait}s). Progress is "
                             "saved; run `python main.py import --resume` after that." if e.seconds else
                             "Still rate-limited after repeated waits; progress is saved, resume later.") from e

    def set_status(self, run_id: int, status: str, **cols) -> None:
        cols = {"status": status, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), **cols}
        with self.conn:
            self.conn.execute(f"UPDATE import_runs SET {', '.join(f'{k} = ?' for k in cols)} WHERE run_id = ?",
                              [*cols.values(), run_id])

    async def upload_one(self, run_id: int, import_id: int, item: dict) -> None:
        path = resolve(self.cfg.export_dir, item["rel_path"])
        if path is None:
            raise StopImport(f"Export file vanished: {item['rel_path']}")
        spec = item["upload"]
        if spec.get("convert") == "tgs_to_webm":
            path = tgs_to_webm(path, item["sha256"], self.cfg.work_dir / "converted")
        sent = None
        reuse = None if self.stats.reuse_disabled else self.by_sha.get(item["sha256"])
        if reuse is not None:
            try:
                sent = await self.call(lambda: self.client(functions.messages.UploadImportedMediaRequest(
                    peer=self.peer, import_id=import_id, file_name=item["import_name"], media=reuse)))
                self.stats.reused += 1
            except errors.ImportIdInvalidError:
                raise
            except errors.BadRequestError as e:
                self.stats.reuse_disabled = f"{type(e).__name__}: {e}"
                console.print(f"[yellow]Re-using uploaded files is not accepted ({type(e).__name__}); "
                              f"uploading every file individually from now on.[/]")
        if sent is None:
            file = await self.call(lambda: self.client.upload_file(path))
            if spec["input"] == "photo":
                media = types.InputMediaUploadedPhoto(file=file)
            else:
                media = types.InputMediaUploadedDocument(file=file, mime_type=spec["mime"],
                                                         attributes=_attributes(spec, path),
                                                         nosound_video=item["kind"] == "gif" or None)
            sent = await self.call(lambda: self.client(functions.messages.UploadImportedMediaRequest(
                peer=self.peer, import_id=import_id, file_name=item["import_name"], media=media)))
            self.stats.uploaded += 1
            self.stats.uploaded_bytes += path.stat().st_size
        reusable = _reusable(sent)
        if reusable is not None:
            self.by_sha.setdefault(item["sha256"], reusable)
        with self.conn:
            self.conn.execute("INSERT INTO import_media (run_id, import_name, sha256, uploaded_at) VALUES (?,?,?,?)",
                              (run_id, item["import_name"], item["sha256"], dt.datetime.now(dt.timezone.utc).isoformat()))

    async def run_chunk(self, run: dict, items: list[dict]) -> None:
        run_id, chat_path = run["run_id"], Path(run["chat_file"])
        data = chat_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != run["chat_sha256"]:
            raise StopImport(f"{chat_path} changed since it was prepared; refusing to import it.")
        import_id = run["import_id"]
        if run["status"] == "prepared":
            file = await self.call(lambda: self.client.upload_file(data, file_name=chat_path.name))
            try:
                r = await self.call(lambda: self.client(functions.messages.InitHistoryImportRequest(
                    peer=self.peer, file=file, media_count=len(items))))
            except errors.PreviousChatImportActiveWaitMinError as e:
                raise StopImport(f"Telegram: a previous import into this chat is still active; "
                                 f"wait {getattr(e, 'minutes', '?')} min and resume.") from e
            import_id = r.id
            self.set_status(run_id, "uploading", import_id=import_id)
            run["status"] = "uploading"

        if run["status"] == "uploading":
            done = {r[0] for r in self.conn.execute("SELECT import_name FROM import_media WHERE run_id = ?", (run_id,))}
            todo = [i for i in items if i["import_name"] not in done]
            columns = (TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn())
            with Progress(*columns, console=console) as progress:
                task = progress.add_task(f"{chat_path.name} media", total=len(items), completed=len(done))
                for item in todo:
                    try:
                        await self.upload_one(run_id, import_id, item)
                    except errors.ImportIdInvalidError as e:
                        self.set_status(run_id, "expired", error="IMPORT_ID_INVALID during upload")
                        raise StopImport("Telegram expired this import session before it was started; nothing "
                                         "was imported. Run `python main.py import --abandon` and start again.") from e
                    progress.advance(task)
            self.set_status(run_id, "starting")
            run["status"] = "starting"

        if run["status"] == "starting":
            try:
                await self.call(lambda: self.client(functions.messages.StartHistoryImportRequest(
                    peer=self.peer, import_id=import_id)))
            except errors.ImportIdInvalidError as e:
                self.set_status(run_id, "unknown", error="IMPORT_ID_INVALID on start")
                raise StopImport("Telegram rejected the start (IMPORT_ID_INVALID). If an earlier attempt already "
                                 "started this import the messages are in the chat; check before doing anything "
                                 "else. Nothing will be re-imported automatically.") from e
            self.set_status(run_id, "done")
            run["status"] = "done"


def _runs(conn, batch: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM import_runs WHERE batch = ? ORDER BY chunk_index", (batch,))]


def unfinished_batch(conn) -> str | None:
    r = conn.execute(f"SELECT batch FROM import_runs WHERE status IN ({','.join('?' * len(UNFINISHED))}) "
                     "ORDER BY run_id DESC LIMIT 1", UNFINISHED).fetchone()
    return r[0] if r else None


def prepare(cfg: Config, conn, mode: str, opts: RenderOptions, destination: str, max_chunk_bytes: int) -> str:
    if mode == "full":
        prior = conn.execute("SELECT run_id FROM import_runs WHERE mode = 'full' AND destination = ? "
                             "AND status IN ('starting', 'done', 'unknown')", (destination,)).fetchone()
        if prior:
            raise SystemExit(f"A full import into {destination} already ran (run {prior[0]}). Refusing a second one.")
    messages = load_messages(cfg)
    chosen = select_test_messages(messages) if mode == "test" else messages
    batch = f"{mode}-{opts.fmt}-{dt.datetime.now():%Y%m%d-%H%M%S}"
    n = 1
    while (cfg.work_dir / "imports" / batch).exists():
        n += 1
        batch = f"{batch.rsplit('~', 1)[0]}~{n}"
    out_dir = cfg.work_dir / "imports" / batch
    plan, manifest = build(chosen, opts, out_dir, max_chunk_bytes, lookup={m.source_id: m for m in messages})
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with conn:
        for i, c in enumerate(plan["chunks"], 1):
            dup = conn.execute("SELECT run_id FROM import_runs WHERE chat_sha256 = ? AND destination = ? "
                               "AND status IN ('starting', 'done', 'unknown')", (c["sha256"], destination)).fetchone()
            if dup:
                raise SystemExit(f"{c['file']} is identical to run {dup[0]}, which was already imported "
                                 f"into {destination}. Refusing to import it twice.")
            conn.execute("INSERT INTO import_runs (created_at, destination, chunk_index, first_seq, last_seq, chat_sha256,"
                         " status, batch, mode, fmt, chat_file, media_count, updated_at)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (now, destination, i, chosen[0].seq, chosen[-1].seq, c["sha256"], "prepared", batch, mode,
                          opts.fmt, str(out_dir / c["file"]), c["media_count"], now))
    return batch


def _confirm(cfg: Config, batch: str, runs: list[dict], plan: dict, entity, confirm_text: str) -> bool:
    name = utils.get_display_name(entity)
    username = f" @{entity.username}" if getattr(entity, "username", None) else ""
    console.rule("[bold]CONFIRM IMPORT")
    console.print(f"[bold]SOURCE[/]       {cfg.export_dir}")
    kind = "private chat with" if isinstance(entity, types.User) else "group"
    console.print(f"[bold]DESTINATION[/]  {kind} {name}{username} (id {entity.id})")
    console.print(f"[bold]MESSAGES[/]     {plan['message_lines']:,} in {len(runs)} import file(s) "
                  f"[{batch}, format {plan['format']}]")
    console.print(f"[bold]MEDIA[/]        {plan['media_uploads']:,} attachments, {plan['unique_media_files']:,} unique "
                  f"files ({plan['unique_media_bytes'] / 2**20:,.1f} MiB)")
    done = [r for r in runs if r["status"] != "prepared"]
    if done:
        console.print(f"[bold]RESUMING[/]     " + ", ".join(f"chunk {r['chunk_index']}: {r['status']}" for r in done))
    console.print(f"\n[italic]{confirm_text}[/]\n")
    console.print("Imported messages are visible to everyone in the destination chat and can only be removed "
                  "by deleting them.")
    return input("Type IMPORT to continue: ").strip() == "IMPORT"


async def run_import(cfg: Config, *, mode: str, opts: RenderOptions, peer_ref: str, resume: bool,
                     max_flood_wait: int, max_chunk_bytes: int) -> int:
    conn = database.connect(cfg.db_path)
    pending = unfinished_batch(conn)
    if pending and not resume:
        console.print(f"[red]Unfinished import {pending} exists.[/] Use `--resume` to continue it "
                      "or `--abandon` to discard it (nothing from it is in the chat unless its status is 'starting').")
        return 2
    if resume and not pending:
        console.print("Nothing to resume.")
        return 0

    client = make_client(cfg)
    client.flood_sleep_threshold = 0  # every FLOOD_WAIT goes through Importer.call
    await client.start()
    try:
        entity = await client.get_entity(int(peer_ref) if peer_ref.lstrip("-").isdigit() else peer_ref)
        if isinstance(entity, types.Chat):
            console.print(f"[red]{entity.title} is a basic group; Telegram only imports history into supergroups.[/] "
                          "Run `python main.py upgrade-group` (same members, new id) and update DESTINATION.")
            return 2
        is_group = getattr(entity, "megagroup", False)
        if not (is_group or isinstance(entity, types.User)):
            console.print(f"[red]{peer_ref} is neither a user nor a group.[/]")
            return 2
        peer = await client.get_input_entity(entity)
        check = await client(functions.messages.CheckHistoryImportPeerRequest(peer=peer))

        if is_group:
            me = await client.get_me()
            opts = dataclasses.replace(opts, group_header=(entity.title, utils.get_display_name(me)))
        batch = pending or prepare(cfg, conn, mode, opts, peer_ref, max_chunk_bytes)
        runs = _runs(conn, batch)
        if any(r["destination"] != peer_ref for r in runs):
            console.print(f"[red]Batch {batch} was prepared for {runs[0]['destination']}, not {peer_ref}.[/]")
            return 2
        out_dir = Path(runs[0]["chat_file"]).parent
        plan = json.loads((out_dir / "plan.json").read_text(encoding="utf-8"))
        manifest = json.loads((out_dir / "media_manifest.json").read_text(encoding="utf-8"))
        if not _confirm(cfg, batch, runs, plan, entity, check.confirm_text):
            if not pending:
                with conn:
                    conn.execute("UPDATE import_runs SET status = 'abandoned' WHERE batch = ?", (batch,))
            console.print("Cancelled; nothing was imported." + (" Progress kept for --resume." if pending else ""))
            return 1

        before = (await client.get_messages(peer, limit=0)).total
        top = await client.get_messages(peer, limit=1)
        with conn:
            conn.execute("UPDATE import_runs SET start_after_id = ? WHERE batch = ? AND start_after_id IS NULL",
                         (top[0].id if top else 0, batch))
        imp = Importer(cfg, client, peer, conn, max_flood_wait)
        started = time.monotonic()
        error = None
        try:
            for run in runs:
                if run["status"] in UNFINISHED:
                    await imp.run_chunk(run, [m for m in manifest if m["chunk"] == run["chunk_index"]])
        except StopImport as e:
            error = str(e)
        except errors.RPCError as e:
            error = f"Telegram error {type(e).__name__}: {e}. Progress is saved; see `python main.py status`."
        await asyncio.sleep(5)
        after = (await client.get_messages(peer, limit=0)).total
        runs = _runs(conn, batch)
        report = {
            "batch": batch, "mode": runs[0]["mode"], "destination": peer_ref, "destination_id": entity.id,
            "format": plan["format"], "runs": [{k: r[k] for k in ("run_id", "chunk_index", "status", "import_id",
                                                                   "media_count", "chat_sha256", "error")} for r in runs],
            "expected_messages": plan["message_lines"], "chat_count_before": before, "chat_count_after": after,
            "media_fresh_uploads": imp.stats.uploaded, "media_reused": imp.stats.reused,
            "uploaded_bytes": imp.stats.uploaded_bytes, "reuse_disabled": imp.stats.reuse_disabled,
            "flood_waits_s": imp.stats.flood_waits, "seconds": round(time.monotonic() - started),
            "stopped": error,
        }
        (out_dir / "import_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
        if error:
            console.print(f"[yellow]{error}[/]")
        else:
            console.print(f"[green]Import started for all chunks.[/] Telegram processes it in the background; "
                          f"chat count {before:,} -> {after:,} so far (expected +{plan['message_lines']:,}).")
        console.print(f"Report: {out_dir / 'import_report.json'}")
        return 1 if error else 0
    finally:
        await client.disconnect()
        conn.close()


def abandon(cfg: Config) -> int:
    conn = database.connect(cfg.db_path)
    batch = unfinished_batch(conn)
    if not batch:
        console.print("No unfinished import.")
        return 0
    runs = _runs(conn, batch)
    if any(r["status"] == "starting" for r in runs):
        console.print("[red]A chunk was being started; it may already be in the chat.[/] Check the chat, then "
                      "run `--resume` (safe: it re-sends start for the same import only).")
        return 2
    with conn:
        conn.execute("UPDATE import_runs SET status = 'abandoned' WHERE batch = ? AND status IN ('prepared', 'uploading')",
                     (batch,))
    console.print(f"Abandoned {batch}; nothing from it was imported.")
    return 0


def status(cfg: Config) -> list[dict]:
    conn = database.connect(cfg.db_path)
    rows = [dict(r) for r in conn.execute(
        "SELECT r.run_id, r.batch, r.chunk_index, r.status, r.media_count, r.destination, r.error,"
        " (SELECT COUNT(*) FROM import_media m WHERE m.run_id = r.run_id) AS media_uploaded"
        " FROM import_runs r ORDER BY r.run_id")]
    conn.close()
    return rows
