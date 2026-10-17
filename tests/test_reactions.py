import datetime as dt
import json

import pytest
from conftest import msg, photo, reaction

from telegram_restore import database
from telegram_restore.config import ALL_ANNOTATIONS
from telegram_restore.dryrun import build
from telegram_restore.media import Inspector
from telegram_restore.models import Message
from telegram_restore.parser import iter_messages
from telegram_restore.reactions import align, attempts, plan_pins, plan_reactions
from telegram_restore.render import RenderOptions

T = dt.datetime(2025, 3, 30, 10, 0, tzinfo=dt.timezone.utc)


def at(s: int) -> dt.datetime:
    return T + dt.timedelta(seconds=s)


def test_align_skips_foreign_and_missing_messages():
    expected = [(1, at(0)), (2, at(1)), (3, at(1)), (4, at(5))]
    imported = [(90, at(-50), 7),  # older import of another batch
                (100, at(0), 7), (101, at(1), 8), (102, at(5), 7)]  # source 3 was dropped by Telegram
    pairs, missing = align(expected, imported)
    assert pairs == [(100, 1, 7), (101, 2, 8), (102, 4, 7)]
    assert missing == 1


def test_plan_only_takes_own_reactions_on_first_part():
    by_id = {
        1: Message(1, "m", 1, "Me", at(0), reactions=[{"emoji": "\u2764", "actors": ["Her"]}]),
        2: Message(2, "m", 2, "Her", at(1), reactions=[{"emoji": "\U0001F44D", "actors": ["Me", "Her"]}]),
    }
    pairs = [(10, 1, 111), (11, 2, 222), (12, 2, 222)]  # source 2 rendered as two messages
    me, todo = plan_reactions(pairs, by_id, my_user_id=111)
    assert me == "Me" and todo == [(11, 2, ["\U0001F44D"])]
    her, todo = plan_reactions(pairs, by_id, my_user_id=222)
    assert her == "Her" and todo == [(10, 1, ["\u2764"]), (11, 2, ["\U0001F44D"])]
    with pytest.raises(SystemExit):
        plan_reactions(pairs, by_id, my_user_id=333)


def test_attempts_fall_back_to_single_and_variation_selector():
    assert attempts(["\u2764"]) == [["\u2764"], ["\u2764\ufe0f"]]
    assert attempts(["\u2764", "\U0001F44D"])[0] == ["\u2764", "\U0001F44D"]


def test_build_writes_message_index_matching_chat_lines(make_export, tmp_path):
    root = make_export([[msg(1, "30.03.2025 10:00:00", "A", "hi"),
                         msg(2, "30.03.2025 10:00:05", "B", "cap", media=photo("photos/a.jpg"),
                             reactions=reaction("\u2764", "A"))]], files=["photos/a.jpg"])
    msgs = list(iter_messages(root))
    for m in msgs:
        if m.media:
            Inspector(root).inspect(m.media)
    build(msgs, RenderOptions(annotations=frozenset(ALL_ANNOTATIONS) - {"reactions"}), tmp_path / "out", 2**20)
    index = json.loads((tmp_path / "out" / "messages_index.json").read_text(encoding="utf-8"))
    assert [sid for sid, _ in index["chat_001.txt"]] == [1, 2, 2]  # photo, then its caption message
    chat = (tmp_path / "out" / "chat_001.txt").read_text(encoding="utf-8")
    assert "Reactions" not in chat


def test_schema_has_reaction_tables(tmp_path):
    conn = database.connect(tmp_path / "db.sqlite3")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"tg_map", "reactions_sent", "pins_sent", "import_media"} <= tables


def test_plan_pins_own_only_in_order_latest_wins():
    by_id = {
        1: Message(1, "m", 1, "Me", at(0)),
        2: Message(2, "m", 2, "Her", at(1)),
        3: Message(3, "m", 3, "Me", at(2), kind="service", service_type="pin", pin_target=2),
        4: Message(4, "m", 4, "Her", at(3), kind="service", service_type="pin", pin_target=1),
        5: Message(5, "m", 5, "Me", at(4), kind="service", service_type="pin", pin_target=1),
        6: Message(6, "m", 6, "Me", at(5), kind="service", service_type="pin", pin_target=2),
        7: Message(7, "m", 7, "Me", at(6), kind="service", service_type="pin", pin_target=999),
    }
    pairs = [(10, 1, 111), (11, 2, 222)]
    me, plan = plan_pins(pairs, by_id, my_user_id=111)
    assert me == "Me" and plan == [(10, 1), (11, 2)]  # 2 re-pinned last, missing target 999 skipped
    her, plan = plan_pins(pairs, by_id, my_user_id=222)
    assert plan == [(10, 1)]


def test_missing_report_names_each_undelivered_line():
    import datetime as dt
    from telegram_restore.models import Media, Message
    from telegram_restore.reactions import match, missing_report
    t = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    s = dt.timedelta(seconds=1)
    by_id = {1: Message(1, "f", 1, "A", t, kind="photo", text="cap", media=Media("photo", rel_path="p.jpg")),
             2: Message(2, "f", 2, "B", t + s, text="x" * 900)}
    expected = [(1, t), (1, t), (2, t + s), (2, t + s)]
    matched = match(expected, [(10, t, 5), (11, t + s, 6)])
    assert [k for k, _, _ in matched] == [0, 2]
    rows = missing_report(expected, {k for k, _, _ in matched}, by_id)
    assert [(r["source_id"], r["what"], r["part"]) for r in rows] == [(1, "text", 1), (2, "text", 1)]
    assert "cap" not in str(rows) and "xxx" not in str(rows)
