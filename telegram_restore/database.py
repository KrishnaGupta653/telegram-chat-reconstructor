"""SQLite persistence: parsed source messages/media, errors, and (later) import state."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import asdict
from pathlib import Path

from .models import Entity, Media, Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    source_id INTEGER PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    html_file TEXT NOT NULL,
    sender TEXT,
    ts TEXT,
    ts_unix INTEGER,
    ts_inferred INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    entities TEXT NOT NULL,
    reply_to_id INTEGER,
    forward TEXT,
    reactions TEXT NOT NULL,
    service_type TEXT,
    pin_target INTEGER,
    group_key TEXT,
    group_pos INTEGER,
    group_size INTEGER,
    joined INTEGER NOT NULL,
    content_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS media (
    source_id INTEGER PRIMARY KEY REFERENCES messages(source_id),
    kind TEXT NOT NULL,
    rel_path TEXT,
    thumb TEXT,
    title TEXT,
    status TEXT,
    duration INTEGER,
    emoji TEXT,
    file_exists INTEGER NOT NULL,
    size INTEGER,
    sha256 TEXT,
    sniffed TEXT
);
CREATE INDEX IF NOT EXISTS media_sha ON media(sha256);
CREATE TABLE IF NOT EXISTS parse_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    html_file TEXT, source_id INTEGER, error TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    destination TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL,
    chat_sha256 TEXT NOT NULL,
    import_id INTEGER,
    status TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS import_media (
    run_id INTEGER NOT NULL REFERENCES import_runs(run_id),
    import_name TEXT NOT NULL,
    sha256 TEXT,
    uploaded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, import_name)
);
CREATE TABLE IF NOT EXISTS tg_map (
    destination TEXT NOT NULL, batch TEXT NOT NULL, tg_msg_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL, from_id INTEGER,
    PRIMARY KEY (destination, tg_msg_id)
);
CREATE TABLE IF NOT EXISTS reactions_sent (
    destination TEXT NOT NULL, tg_msg_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL, emojis TEXT NOT NULL, status TEXT NOT NULL, error TEXT, at TEXT NOT NULL,
    PRIMARY KEY (destination, tg_msg_id, user_id)
);
CREATE TABLE IF NOT EXISTS pins_sent (
    destination TEXT NOT NULL, tg_msg_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL, emojis TEXT NOT NULL, status TEXT NOT NULL, error TEXT, at TEXT NOT NULL,
    PRIMARY KEY (destination, tg_msg_id, user_id)
);
"""
IMPORT_RUN_COLUMNS = {"batch": "TEXT", "mode": "TEXT", "fmt": "TEXT", "chat_file": "TEXT",
                      "media_count": "INTEGER", "updated_at": "TEXT", "start_after_id": "INTEGER"}


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(import_runs)")}
    for col, typ in IMPORT_RUN_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE import_runs ADD COLUMN {col} {typ}")
    return conn


def has_import_activity(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM import_runs WHERE status != 'abandoned'").fetchone()[0] > 0


def replace_source(conn: sqlite3.Connection, messages: Iterable[Message],
                   errors: Iterable[tuple[str, int | None, str]], meta: dict[str, str]) -> None:
    """Atomically replace all parsed source data. Refuses once any import has been started."""
    if has_import_activity(conn):
        raise RuntimeError("Import runs exist; refusing to rebuild source tables (would break resume/audit).")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with conn:
        conn.execute("DELETE FROM media")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM parse_errors")
        for m in messages:
            conn.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (m.source_id, m.seq, m.html_file, m.sender, m.ts.isoformat() if m.ts else None,
                 int(m.ts.timestamp()) if m.ts else None, int(m.ts_inferred), m.kind, m.text,
                 json.dumps([asdict(e) for e in m.entities], ensure_ascii=False), m.reply_to_id,
                 json.dumps(m.forward, ensure_ascii=False) if m.forward else None,
                 json.dumps(m.reactions, ensure_ascii=False), m.service_type, m.pin_target,
                 m.group_key, m.group_pos, m.group_size, int(m.joined), m.content_hash),
            )
            if m.media:
                d = m.media
                conn.execute(
                    "INSERT INTO media VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (m.source_id, d.kind, d.rel_path, d.thumb, d.title, d.status, d.duration, d.emoji,
                     int(d.exists), d.size, d.sha256, d.sniffed),
                )
        conn.executemany("INSERT INTO parse_errors (html_file, source_id, error, at) VALUES (?,?,?,?)",
                         [(f, i, e, now) for f, i, e in errors])
        conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", list(meta.items()))


def _row_to_message(r: sqlite3.Row) -> Message:
    msg = Message(
        source_id=r["source_id"], html_file=r["html_file"], seq=r["seq"], sender=r["sender"],
        ts=dt.datetime.fromisoformat(r["ts"]) if r["ts"] else None, ts_inferred=bool(r["ts_inferred"]),
        kind=r["kind"], text=r["text"], entities=[Entity(**e) for e in json.loads(r["entities"])],
        reply_to_id=r["reply_to_id"], forward=json.loads(r["forward"]) if r["forward"] else None,
        reactions=json.loads(r["reactions"]), service_type=r["service_type"], pin_target=r["pin_target"],
        group_key=r["group_key"], group_pos=r["group_pos"], group_size=r["group_size"],
        joined=bool(r["joined"]), content_hash=r["content_hash"],
    )
    if r["m_kind"] is not None:
        msg.media = Media(kind=r["m_kind"], rel_path=r["rel_path"], thumb=r["thumb"], title=r["title"],
                          status=r["status"], duration=r["duration"], emoji=r["emoji"],
                          exists=bool(r["file_exists"]), size=r["size"], sha256=r["sha256"], sniffed=r["sniffed"])
    return msg


def iter_messages(conn: sqlite3.Connection) -> Iterator[Message]:
    rows = conn.execute(
        "SELECT m.*, d.kind AS m_kind, d.rel_path, d.thumb, d.title, d.status, d.duration, d.emoji,"
        " d.file_exists, d.size, d.sha256, d.sniffed"
        " FROM messages m LEFT JOIN media d USING (source_id) ORDER BY m.seq"
    )
    for r in rows:
        yield _row_to_message(r)


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
