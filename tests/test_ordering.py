from conftest import msg, photo

from telegram_restore.parser import iter_messages
from telegram_restore.scanner import mark_sent_together


def test_same_second_messages_keep_file_order(make_export):
    root = make_export([[msg(i, "30.03.2025 10:00:00", "A", f"m{i}") for i in range(5, 10)]])
    out = list(iter_messages(root))
    assert [m.source_id for m in out] == [5, 6, 7, 8, 9]
    assert [m.seq for m in out] == [1, 2, 3, 4, 5]


def test_sent_together_requires_same_sender_second_and_continuation(make_export):
    blocks = [
        msg(1, "30.03.2025 10:00:00", "A", media=photo("photos/1.jpg")),
        msg(2, "30.03.2025 10:00:00", None, media=photo("photos/2.jpg")),
        msg(3, "30.03.2025 10:00:00", None, media=photo("photos/3.jpg")),
        msg(4, "30.03.2025 10:00:01", None, media=photo("photos/4.jpg")),  # different second
        msg(5, "30.03.2025 10:00:01", "B", media=photo("photos/5.jpg")),   # different sender
    ]
    out = list(iter_messages(make_export([blocks])))
    assert mark_sent_together(out) == 1
    assert [(m.group_pos, m.group_size) for m in out[:3]] == [(1, 3), (2, 3), (3, 3)]
    assert out[3].group_key is None and out[4].group_key is None
