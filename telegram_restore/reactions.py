"""Re-create reactions and pins on imported messages, for the logged-in account only.

Each person adds their own reactions and pins from their own account; nothing is ever sent on
someone else's behalf. Imported messages are matched back to source messages through the
messages_index.json written with every import batch (order + original timestamp).
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import datetime as dt
import json
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn
from telethon import errors, types, utils
from telethon.tl import functions

from . import database
from .config import Config
from .models import Message
from .telegram_api import FloodStop, call_with_flood, make_client

console = Console()
MAX_CONSECUTIVE_FAILURES = 20


def load_index(conn, destination: str, batch: str | None) -> tuple[str, list[tuple[int, dt.datetime]], int]:
    """(batch, [(source_id, original ts)] in import order, id of the last chat message before the import)."""
    rows = [dict(r) for r in conn.execute(
        "SELECT batch, chat_file, chunk_index, start_after_id FROM import_runs WHERE destination = ? AND status = 'done'"
        " ORDER BY run_id", (destination,))]
    if batch:
        rows = [r for r in rows if r["batch"] == batch]
    if not rows:
        raise SystemExit(f"No completed import into {destination}" + (f" for batch {batch}" if batch else "") + ".")
    batch = rows[-1]["batch"]
    rows = sorted((r for r in rows if r["batch"] == batch), key=lambda r: r["chunk_index"])
    index_file = Path(rows[0]["chat_file"]).parent / "messages_index.json"
    if not index_file.exists():
        raise SystemExit(f"{batch} was imported before reaction/pin support (no messages_index.json); re-import needed.")
    index = json.loads(index_file.read_text(encoding="utf-8"))
    entries = [(sid, dt.datetime.fromisoformat(ts)) for r in rows for sid, ts in index[Path(r["chat_file"]).name]]
    return batch, entries, rows[0]["start_after_id"] or 0


def match(expected: list[tuple[int, dt.datetime]], imported: list[tuple[int, dt.datetime, int | None]]):
    """[(index into expected, tg_msg_id, from_user_id)], pairing by order and original timestamp."""
    out, j = [], 0
    for msg_id, date, from_id in imported:
        k = j
        while k < len(expected) and expected[k][1] != date:
            k += 1
        if k == len(expected):
            continue  # an imported message from another batch
        out.append((k, msg_id, from_id))
        j = k + 1
    return out


def align(expected: list[tuple[int, dt.datetime]], imported: list[tuple[int, dt.datetime, int | None]]):
    """Pair imported Telegram messages with source ids by order and original timestamp.

    Returns ([(tg_msg_id, source_id, from_user_id)], expected entries with no imported counterpart).
    """
    matched = match(expected, imported)
    return [(msg_id, expected[k][0], from_id) for k, msg_id, from_id in matched], len(expected) - len(matched)


def my_export_name(pairs, by_id: dict[int, Message], my_user_id: int) -> str:
    """The export sender name Telegram attributed to this account during the import."""
    ids_by_name = collections.defaultdict(collections.Counter)
    for _, sid, from_id in pairs:
        if from_id is not None:
            ids_by_name[by_id[sid].sender][from_id] += 1
    names = [name for name, c in ids_by_name.items() if c.most_common(1)[0][0] == my_user_id]
    if len(names) != 1:
        raise SystemExit("Could not tell which export name is this account; it must appear as a sender of the import.")
    return names[0]


def first_tg_ids(pairs) -> dict[int, int]:
    """source_id -> Telegram id of its first imported message (attachment, or first part of a split text)."""
    out: dict[int, int] = {}
    for tg_id, sid, _ in pairs:
        out.setdefault(sid, tg_id)
    return out


def plan_reactions(pairs, by_id: dict[int, Message], my_user_id: int) -> tuple[str, list[tuple[int, int, list[str]]]]:
    """(my export name, [(tg_msg_id, source_id, emojis)]) for reactions the logged-in user made."""
    me = my_export_name(pairs, by_id, my_user_id)
    todo = []
    for sid, tg_id in first_tg_ids(pairs).items():
        emojis = [r["emoji"] for r in by_id[sid].reactions if me in r["actors"]]
        if emojis:
            todo.append((tg_id, sid, emojis))
    return me, todo


def plan_pins(pairs, by_id: dict[int, Message], my_user_id: int) -> tuple[str, list[tuple[int, int]]]:
    """(my export name, [(tg_msg_id, target source_id)]) in original pin order, latest pin of a message wins."""
    me = my_export_name(pairs, by_id, my_user_id)
    targets = first_tg_ids(pairs)
    order: dict[int, int] = {}
    for m in sorted(by_id.values(), key=lambda m: m.seq):
        if m.service_type == "pin" and m.sender == me and m.pin_target in targets:
            order.pop(m.pin_target, None)
            order[m.pin_target] = targets[m.pin_target]
    return me, [(tg_id, sid) for sid, tg_id in order.items()]


def attempts(emojis: list[str]) -> list[list[str]]:
    """Reaction sets to try in order: all of them (Premium allows several), then the first one with and
    without the emoji variation selector, since exports and Telegram's reaction list may differ there."""
    plain = emojis[0].replace("\ufe0f", "")
    singles = [[v] for v in dict.fromkeys([emojis[0], plain, plain + "\ufe0f"])]
    return ([emojis] if len(emojis) > 1 else []) + singles


def _service_ids(updates) -> list[int]:
    """Ids of the 'pinned a message' service messages created by an updatePinnedMessage call."""
    ids = []
    for u in getattr(updates, "updates", []):
        msg = getattr(u, "message", None)
        if isinstance(msg, types.MessageService) and isinstance(msg.action, types.MessageActionPinMessage):
            ids.append(msg.id)
    return ids


async def _imported_messages(client, peer, start_after: int) -> list[tuple[int, dt.datetime, int | None]]:
    out = []
    with console.status("Reading imported messages from the chat...") as status:
        async for m in client.iter_messages(peer, reverse=True, min_id=start_after, wait_time=0):
            f = m.fwd_from
            if f and f.imported and f.date:
                out.append((m.id, f.date, utils.get_peer_id(m.from_id) if m.from_id else None))
                if len(out) % 5000 == 0:
                    status.update(f"Reading imported messages from the chat... {len(out):,}")
    return out


async def run_native(cfg: Config, action: str, *, batch: str | None, session: str | None, dry_run: bool,
                     logout: bool, pace: float, max_flood_wait: int) -> int:
    """action: 'react' or 'pin'."""
    if not cfg.destination:
        raise SystemExit("Set DESTINATION in .env.")
    if session:
        cfg = dataclasses.replace(cfg, session=Path(session).resolve())
    conn = database.connect(cfg.db_path)
    batch, expected, start_after = load_index(conn, cfg.destination, batch)
    by_id = {m.source_id: m for m in database.iter_messages(conn)}
    table = "reactions_sent" if action == "react" else "pins_sent"

    client = make_client(cfg)
    await client.start()
    try:
        me = await client.get_me()
        entity = await client.get_entity(int(cfg.destination) if cfg.destination.lstrip("-").isdigit() else cfg.destination)
        peer = await client.get_input_entity(entity)

        cached = conn.execute("SELECT tg_msg_id, source_id, from_id FROM tg_map WHERE destination = ? AND batch = ?"
                              " ORDER BY tg_msg_id", (cfg.destination, batch)).fetchall()
        if cached:
            pairs, missing = [tuple(r) for r in cached], None
        else:
            pairs, missing = align(expected, await _imported_messages(client, peer, start_after))
            if missing <= len(expected) // 100:  # imports land gradually; never cache a partial view
                with conn:
                    conn.executemany("INSERT OR REPLACE INTO tg_map VALUES (?,?,?,?,?)",
                                     [(cfg.destination, batch, *p) for p in pairs])
        if action == "react":
            my_name, todo = plan_reactions(pairs, by_id, me.id)
        else:
            my_name, plan = plan_pins(pairs, by_id, me.id)
            todo = [(tg_id, sid, []) for tg_id, sid in plan]
        done = {r[0] for r in conn.execute(f"SELECT tg_msg_id FROM {table} WHERE destination = ? AND user_id = ?"
                                           " AND status = 'ok'", (cfg.destination, me.id))}
        todo = [t for t in todo if t[0] not in done]

        console.rule(f"[bold]{'REACTIONS' if action == 'react' else 'PINS'}")
        console.print(f"[bold]ACCOUNT[/]      {utils.get_display_name(me)} (id {me.id}), export name {my_name!r}")
        console.print(f"[bold]DESTINATION[/]  {utils.get_display_name(entity)} [{batch}]")
        console.print(f"[bold]MATCHED[/]      {len(pairs):,} imported messages"
                      + (f"; {missing:,} expected messages not found in the chat" if missing else ""))
        summary = (", ".join(f"{e} {n}" for e, n in collections.Counter(e for t in todo for e in t[2]).most_common(8))
                   if action == "react" else "pinned in original order; the 'pinned a message' lines are deleted")
        console.print(f"[bold]TO DO[/]        {len(todo):,} ({len(done):,} already done): {summary}")
        if dry_run or not todo:
            return 0
        if action == "react":
            console.print("Each reaction may notify the message's author; muting the group first is a good idea.")
        word = "REACT" if action == "react" else "PIN"
        if input(f"Type {word} to continue: ").strip() != word:
            console.print("Cancelled.")
            return 1

        waits: list[int] = []
        failures = consecutive = 0
        columns = (TextColumn(action), BarColumn(), MofNCompleteColumn(), TimeRemainingColumn())
        with Progress(*columns, console=console) as progress:
            task = progress.add_task("", total=len(todo))
            for tg_id, sid, emojis in todo:
                status, error = "failed", None
                if action == "react":
                    for attempt in attempts(emojis):
                        try:
                            await call_with_flood(lambda a=attempt: client(functions.messages.SendReactionRequest(
                                peer=peer, msg_id=tg_id, reaction=[types.ReactionEmoji(emoticon=e) for e in a],
                                add_to_recent=False)), max_flood_wait, waits)
                            status, error = "ok", None
                            break
                        except errors.BadRequestError as e:
                            error = f"{type(e).__name__}: {e}"
                else:
                    try:
                        r = await call_with_flood(lambda: client(functions.messages.UpdatePinnedMessageRequest(
                            peer=peer, id=tg_id, silent=True)), max_flood_wait, waits)
                        status = "ok"
                        service = _service_ids(r)
                        if service:
                            await call_with_flood(lambda: client.delete_messages(peer, service), max_flood_wait, waits)
                    except (errors.ChatAdminRequiredError, errors.ChatWriteForbiddenError) as e:
                        console.print(f"[red]This account may not pin in this group ({type(e).__name__}). The group "
                                      "owner must allow it (admin with 'Pin messages', or members may pin).[/]")
                        return 1
                    except errors.BadRequestError as e:
                        error = f"{type(e).__name__}: {e}"
                with conn:
                    conn.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?,?)",
                                 (cfg.destination, tg_id, me.id, sid, json.dumps(emojis, ensure_ascii=False), status,
                                  error, dt.datetime.now(dt.timezone.utc).isoformat()))
                failures += status != "ok"
                consecutive = consecutive + 1 if status != "ok" else 0
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    console.print(f"[red]{consecutive} failures in a row (last: {error}); stopping.[/]")
                    return 1
                progress.advance(task)
                await asyncio.sleep(pace)
        console.print(f"[green]Done.[/] {len(todo) - failures:,} succeeded, {failures:,} failed"
                      + (f" (see {table} in the database)" if failures else "") + ".")
        return 1 if failures else 0
    except FloodStop as e:
        console.print(f"[yellow]Telegram asked to wait {e.seconds}s; progress is saved. Run the command again later.[/]")
        return 1
    finally:
        if logout:
            await client.log_out()
            console.print("Logged out; this account's session was removed from this computer.")
        else:
            await client.disconnect()
        conn.close()


def missing_report(expected, matched_idx: set[int], by_id: dict[int, Message]) -> list[dict]:
    """Metadata (no message text) for every expected chat line Telegram did not create."""
    out, seen = [], collections.Counter()
    for k, (sid, ts) in enumerate(expected):
        part = seen[sid]
        seen[sid] += 1
        if k in matched_idx:
            continue
        m = by_id.get(sid)
        has_file = bool(m and m.media and m.media.rel_path)
        out.append({"source_id": sid, "date": ts.isoformat(), "sender": m.sender if m else None,
                    "what": m.media.kind if has_file and part == 0 else "text",
                    "chars": len(m.text) if m else None, "part": part})
    return out


async def verify(cfg: Config, batch: str | None) -> int:
    """Compare the destination chat with the latest import batch and report what Telegram did not create."""
    if not cfg.destination:
        raise SystemExit("Set DESTINATION in .env.")
    conn = database.connect(cfg.db_path)
    batch, expected, start_after = load_index(conn, cfg.destination, batch)
    by_id = {m.source_id: m for m in database.iter_messages(conn)}
    client = make_client(cfg)
    await client.start()
    try:
        entity = await client.get_entity(int(cfg.destination) if cfg.destination.lstrip("-").isdigit() else cfg.destination)
        imported = await _imported_messages(client, await client.get_input_entity(entity), start_after)
    finally:
        await client.disconnect()
    matched = match(expected, imported)
    missing = missing_report(expected, {k for k, _, _ in matched}, by_id)
    report = {"batch": batch, "destination": cfg.destination, "expected": len(expected), "found": len(matched),
              "missing": missing}
    path = cfg.work_dir / "verify_report.json"
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")

    console.rule("[bold]VERIFY")
    console.print(f"[bold]DESTINATION[/]  {utils.get_display_name(entity)} [{batch}]")
    console.print(f"[bold]FOUND[/]        {len(matched):,} of {len(expected):,} chat lines")
    if not missing:
        console.print("[green]Nothing missing.[/]")
        return 0
    kinds = collections.Counter(("long text" if r["what"] == "text" and (r["chars"] or 0) > 500 else r["what"])
                                for r in missing)
    console.print(f"[bold]MISSING[/]      {len(missing):,}: " + ", ".join(f"{k} {n}" for k, n in kinds.most_common()))
    for r in missing[:15]:
        console.print(f"  {r['date'][:19]}  {(r['sender'] or '?')[:12]:12}  {r['what']:8} {r['chars'] or 0:>6} chars"
                      + (f" (part {r['part'] + 1})" if r["part"] else ""))
    if len(missing) > 15:
        console.print(f"  ... and {len(missing) - 15:,} more")
    console.print(f"Full list (dates and sizes only): {path}")
    console.print("If the import finished only minutes ago, run verify again later: Telegram adds messages gradually.")
    return 1
