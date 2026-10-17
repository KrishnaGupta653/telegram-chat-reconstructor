"""Telegram history restore: command-line entry point.

Offline: scan, validate, dry-run, status. Read-only: probe. Writes to Telegram (after confirmation): import.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from telegram_restore import config as config_mod
from telegram_restore.render import FORMATS, RenderOptions, parse_offset

console = Console()


def cmd_scan(cfg, args) -> int:
    from telegram_restore.scanner import scan

    s = scan(cfg)
    console.print(f"[bold]SOURCE[/] {cfg.export_dir}   [bold]DB[/] {cfg.db_path}")
    t = Table(show_header=False)
    for key in ("html_file_count", "message_blocks", "messages", "first", "last", "chronological", "replies",
                "replies_target_missing", "forwards", "messages_with_reactions", "messages_with_formatting",
                "sent_together_groups", "media_refs_with_file", "media_unique_files", "media_missing",
                "media_magic_mismatch", "parse_errors"):
        t.add_row(key, str(s[key]))
    t.add_row("media_unique_bytes", f"{s['media_unique_bytes'] / 2**30:.2f} GiB")
    console.print(t)
    for key in ("senders", "kinds", "service", "media_no_file_in_export"):
        console.print(f"[bold]{key}[/]: {s[key]}")
    if s["unsupported"]:
        console.print(f"[yellow]unsupported content[/]: {len(s['unsupported'])}")
    console.print(f"Full report: {cfg.work_dir / 'scan_report.json'}")
    return 1 if s["parse_errors"] else 0


def cmd_validate(cfg, args) -> int:
    from telegram_restore.validator import validate

    findings = validate(cfg)
    t = Table("level", "check", "count", "detail")
    colors = {"error": "red", "warning": "yellow", "info": "cyan", "ok": "green"}
    for f in findings:
        t.add_row(f"[{colors[f.level]}]{f.level}[/]", f.check, str(f.count), f.detail)
    console.print(t)
    errors = sum(1 for f in findings if f.level == "error")
    console.print("[red]Validation failed[/]" if errors else "[green]No blocking errors[/]")
    return 1 if errors else 0


def cmd_dry_run(cfg, args) -> int:
    from telegram_restore.dryrun import dry_run

    opts = RenderOptions(fmt=args.format, annotations=cfg.annotations, formatting=args.formatting,
                         utc_offset=parse_offset(args.utc_offset), reply_quote_chars=cfg.reply_quote_chars)
    plan = dry_run(cfg, opts, max_chunk_bytes=int(args.max_chunk_mib * 2**20))
    console.print("[bold]DRY RUN: Telegram was not contacted.[/]")
    console.print(json.dumps({k: v for k, v in plan.items() if k != "chunks"}, ensure_ascii=False, indent=1))
    for c in plan["chunks"]:
        console.print(f"  {c['file']}: {c['bytes'] / 2**20:.1f} MiB, {c['message_lines']} messages, {c['media_count']} media")
    return 0


def cmd_probe(cfg, args) -> int:
    from telegram_restore.telegram_api import probe

    files = [Path(p) for p in args.chat_files] or latest_dry_run_files(cfg)
    if not files:
        console.print("[red]No dry-run output; run `dry-run --format ios` and `dry-run --format android` first.[/]")
        return 2
    missing = [p for p in files if not p.is_file()]
    if missing:
        console.print(f"[red]Not found:[/] {missing}")
        return 2
    peer = args.peer or cfg.destination
    console.print("Read-only probe: messages.checkHistoryImport" + (f" + checkHistoryImportPeer({peer})" if peer else ""))
    results = asyncio.run(probe(cfg, files, peer))
    for r in results:
        console.print(r)
    report = cfg.work_dir / "probe_report.json"
    report.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    console.print(f"Saved: {report}")
    return 1 if any("error" in r for r in results) else 0


def cmd_import(cfg, args) -> int:
    from telegram_restore import importer

    if args.abandon:
        return importer.abandon(cfg)
    peer = args.peer or cfg.destination
    if not peer:
        console.print("[red]No destination: set DESTINATION in .env or pass --peer.[/]")
        return 2
    opts = RenderOptions(fmt=args.format, annotations=cfg.annotations, formatting=args.formatting,
                         reply_quote_chars=cfg.reply_quote_chars)
    return asyncio.run(importer.run_import(cfg, mode="full" if args.full else "test", opts=opts, peer_ref=peer,
                                           resume=args.resume, max_flood_wait=args.max_flood_wait,
                                           max_chunk_bytes=int(args.max_chunk_mib * 2**20)))


def cmd_create_group(cfg, args) -> int:
    from telegram_restore.telegram_api import create_import_group

    gid = asyncio.run(create_import_group(cfg, args.title, args.about))
    console.print(f"Created supergroup [bold]{args.title}[/] (only you are in it).")
    console.print(f"Set this in .env, then run `python main.py import`:\n  DESTINATION={gid}")
    return 0


def cmd_upgrade_group(cfg, args) -> int:
    from telegram_restore.telegram_api import upgrade_group

    target = args.peer or cfg.destination
    if not target or not target.lstrip("-").isdigit():
        console.print("[red]Pass the basic group's numeric id with --peer or set it as DESTINATION.[/]")
        return 2
    console.print("This converts the basic group into a supergroup (irreversible; members and messages stay).")
    if input("Type UPGRADE to continue: ").strip() != "UPGRADE":
        console.print("Cancelled.")
        return 1
    title, gid = asyncio.run(upgrade_group(cfg, int(target)))
    console.print(f"Upgraded [bold]{title}[/]. Set this in .env:\n  DESTINATION={gid}")
    return 0


def cmd_native(cfg, args) -> int:
    from telegram_restore.reactions import run_native

    return asyncio.run(run_native(cfg, args.command, batch=args.batch, session=args.session, dry_run=args.dry_run,
                                  logout=args.logout, pace=args.pace, max_flood_wait=args.max_flood_wait))


def cmd_verify(cfg, args) -> int:
    from telegram_restore.reactions import verify

    return asyncio.run(verify(cfg, args.batch))


def cmd_status(cfg, args) -> int:
    from telegram_restore.importer import status

    rows = status(cfg)
    if not rows:
        console.print("No imports yet.")
        return 0
    t = Table("run", "batch", "chunk", "status", "media", "destination", "error")
    for r in rows:
        t.add_row(str(r["run_id"]), r["batch"] or "", str(r["chunk_index"]), r["status"],
                  f"{r['media_uploaded']}/{r['media_count']}", r["destination"], r["error"] or "")
    console.print(t)
    return 0


def latest_dry_run_files(cfg) -> list[Path]:
    """chat_001.txt of the newest dry-run of each format."""
    files = []
    for fmt in FORMATS:
        runs = sorted((cfg.work_dir / "dry_run").glob(f"{fmt}-*/chat_001.txt"))
        if runs:
            files.append(runs[-1])
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=str(config_mod.PROJECT_DIR / ".env"), help="path to .env (default: project .env)")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="parse the export into SQLite and print a census")
    sub.add_parser("validate", help="consistency checks on the parsed data")
    d = sub.add_parser("dry-run", help="build the import chat file(s) + media manifest locally")
    d.add_argument("--format", choices=FORMATS, default="ios")
    d.add_argument("--formatting", choices=("plain", "whatsapp"), default="plain")
    d.add_argument("--utc-offset", help="render times in this offset, e.g. +00:00 (default: export's own)")
    d.add_argument("--max-chunk-mib", type=float, default=30.0)
    p = sub.add_parser("probe", help="read-only: ask Telegram whether a chat file's format is recognised")
    p.add_argument("chat_files", nargs="*", help="chat_XXX.txt files (default: newest ios and android dry-run)")
    p.add_argument("--peer", help="destination for checkHistoryImportPeer: @username or +phone (default: DESTINATION)")
    i = sub.add_parser("import", help="native history import (asks for confirmation; default: small test)")
    mode = i.add_mutually_exclusive_group()
    mode.add_argument("--test", action="store_true", help="import a ~30-message sample covering every type (default)")
    mode.add_argument("--full", action="store_true", help="import the whole export")
    mode.add_argument("--resume", action="store_true", help="continue the unfinished import")
    mode.add_argument("--abandon", action="store_true", help="discard an unfinished, not-yet-started import")
    i.add_argument("--peer", help="@username or +phone (default: DESTINATION)")
    i.add_argument("--format", choices=FORMATS, default="ios")
    i.add_argument("--formatting", choices=("plain", "whatsapp"), default="plain")
    i.add_argument("--max-flood-wait", type=int, default=900, help="longest FLOOD_WAIT to sit out, seconds")
    i.add_argument("--max-chunk-mib", type=float, default=30.0)
    sub.add_parser("status", help="show import runs recorded in SQLite (offline)")
    v = sub.add_parser("verify", help="list source messages Telegram did not create in DESTINATION (read-only)")
    v.add_argument("--batch", help="import batch (default: latest completed import into DESTINATION)")
    for name, what in (("react", "reactions"), ("pin", "pins")):
        r = sub.add_parser(name, help=f"re-create the logged-in account's own original {what} on imported messages")
        r.add_argument("--dry-run", action="store_true", help="match and count only; send nothing")
        r.add_argument("--batch", help="import batch (default: latest completed import into DESTINATION)")
        r.add_argument("--session", help="session file for this account, e.g. work/other (default: TG_SESSION)")
        r.add_argument("--logout", action="store_true", help="log this account out of this computer when finished")
        r.add_argument("--pace", type=float, default=1.0, help="seconds between requests")
        r.add_argument("--max-flood-wait", type=int, default=900)
    u = sub.add_parser("upgrade-group", help="convert a basic group into a supergroup so it can receive an import")
    u.add_argument("--peer", help="basic group id (default: DESTINATION)")
    g = sub.add_parser("create-group", help="create an empty supergroup for importing (keeps original dates)")
    g.add_argument("--title", required=True)
    g.add_argument("--about", default="Imported chat history")
    args = ap.parse_args(argv)
    cfg = config_mod.load(args.env)
    handler = {"scan": cmd_scan, "validate": cmd_validate, "dry-run": cmd_dry_run, "probe": cmd_probe,
               "import": cmd_import, "status": cmd_status, "verify": cmd_verify, "create-group": cmd_create_group,
               "upgrade-group": cmd_upgrade_group, "react": cmd_native, "pin": cmd_native}[args.command]
    return handler(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
