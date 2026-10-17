import datetime as dt

import pytest
from conftest import msg, photo, reaction

from telegram_restore import database
from telegram_restore.media import Inspector
from telegram_restore.parser import iter_messages
from telegram_restore.scanner import content_hash


def load(root):
    msgs = list(iter_messages(root))
    ins = Inspector(root)
    for m in msgs:
        if m.media:
            ins.inspect(m.media)
        m.content_hash = content_hash(m)
    return msgs


@pytest.fixture
def export(make_export):
    blocks = [msg(1, "30.03.2025 10:00:00", "A", "<strong>hi</strong>", reactions=reaction("👍", "B")),
              msg(2, "30.03.2025 10:00:05", "B", "cap", media=photo("photos/a.jpg"), reply="#go_to_message1")]
    return make_export([blocks], files=["photos/a.jpg"])


def test_roundtrip_preserves_everything(tmp_path, export):
    msgs = load(export)
    conn = database.connect(tmp_path / "db.sqlite3")
    database.replace_source(conn, msgs, [], {"k": "v"})
    back = list(database.iter_messages(conn))
    assert back == msgs
    assert back[1].ts == dt.datetime(2025, 3, 30, 10, 0, 5, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    assert database.get_meta(conn) == {"k": "v"}


def test_rescan_is_idempotent(tmp_path, export):
    conn = database.connect(tmp_path / "db.sqlite3")
    database.replace_source(conn, load(export), [], {})
    database.replace_source(conn, load(export), [("messages.html", 9, "x")], {})
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM parse_errors").fetchone()[0] == 1


def test_rescan_refused_once_import_started(tmp_path, export):
    conn = database.connect(tmp_path / "db.sqlite3")
    database.replace_source(conn, load(export), [], {})
    conn.execute("INSERT INTO import_runs (created_at, destination, chunk_index, first_seq, last_seq, chat_sha256, status)"
                 " VALUES ('now', '@x', 1, 1, 2, 'h', 'initialized')")
    conn.commit()
    with pytest.raises(RuntimeError):
        database.replace_source(conn, load(export), [], {})


def test_content_hash_changes_with_content(export):
    a, b = load(export)
    before = a.content_hash
    a.text = "changed"
    assert content_hash(a) != before and a.content_hash != b.content_hash
