from conftest import block_media, msg, photo, reaction, service

from telegram_restore.config import ALL_ANNOTATIONS
from telegram_restore.dryrun import chunk_bounds
from telegram_restore.media import Inspector
from telegram_restore.parser import iter_messages
from telegram_restore.render import LINE_START, RenderOptions, parse_offset, render
from telegram_restore.scanner import mark_sent_together

ALL = frozenset(ALL_ANNOTATIONS)


def rendered(make_export, blocks, files=(), **kw):
    root = make_export([blocks], files=list(files))
    msgs = list(iter_messages(root))
    ins = Inspector(root)
    for m in msgs:
        if m.media:
            ins.inspect(m.media)
    mark_sent_together(msgs)
    return render(msgs, RenderOptions(annotations=kw.pop("annotations", ALL), **kw))


def test_group_header_precedes_first_message(make_export):
    blocks = [msg(1, "30.03.2025 17:50:50", "A", "Hey!")]
    ios = rendered(make_export, blocks, group_header=("Archive", "Me")).lines
    assert ios[0] == "[30/03/2025, 17:50:49] Archive: \u200eMe created group \u201cArchive\u201d"
    android = rendered(make_export, blocks, fmt="android", group_header=("Archive", "Me")).lines
    assert android[0] == '30/03/2025, 17:50 - Me created group "Archive"'


def test_ios_and_android_line_shapes(make_export):
    blocks = [msg(1, "30.03.2025 17:50:50", "Alice (A)", "Hey!")]
    assert rendered(make_export, blocks).lines == ["[30/03/2025, 17:50:50] Alice (A): Hey!"]
    assert rendered(make_export, blocks, fmt="android").lines == ["30/03/2025, 17:50 - Alice (A): Hey!"]


def test_multiline_and_escape_of_lookalike_lines(make_export):
    blocks = [msg(1, "30.03.2025 17:50:50", "A", "one<br>[01/01/2025, 10:00:00] B: fake<br>three")]
    r = rendered(make_export, blocks)
    assert r.lines[0] == "[30/03/2025, 17:50:50] A: one"
    assert r.lines[1].startswith("\u2060[01/01/2025") and not LINE_START.match(r.lines[1])
    assert r.lines[2] == "three" and r.escaped_lines == 1


def test_attachment_first_then_notes_caption_reactions(make_export):
    blocks = [msg(1, "30.03.2025 10:00:00", "A", "orig text"),
              msg(2, "30.03.2025 10:00:05", "B", "cap", media=photo("photos/a.jpg"),
                  reply="#go_to_message1", reactions=reaction("❤", "A"))]
    r = rendered(make_export, blocks, files=["photos/a.jpg"])
    assert r.lines[1] == "[30/03/2025, 10:00:05] B: \u200e<attached: 00000002-PHOTO-2025-03-30-10-00-05.jpg>"
    assert r.lines[2] == "[30/03/2025, 10:00:05] B: [↩ Reply to A, 30.03.2025 10:00:00: orig text]"
    assert r.lines[3] == "cap"
    assert r.lines[4] == "[Reactions: ❤ A]"
    assert r.media[0]["import_name"] == "00000002-PHOTO-2025-03-30-10-00-05.jpg" and r.media[0]["line"] == 1


def test_missing_media_calls_ttl_pins_and_missing_reply(make_export):
    blocks = [
        msg(1, "30.03.2025 10:00:00", "A", "x", media=photo("photos/gone.jpg")),
        msg(2, "30.03.2025 10:00:01", "A", media=block_media("media_call success", None, "B", "Incoming (3426 seconds)")),
        msg(3, "30.03.2025 10:00:02", "A", "secret", media=block_media("media_photo", None, "Self-destructing photo", "Expired")),
        service(-2, "B pinned", target=1),
        msg(4, "30.03.2025 10:00:03", "B", "?", reply="#go_to_message999"),
    ]
    lines = rendered(make_export, blocks).lines
    assert lines[0] == "[30/03/2025, 10:00:00] A: [Photo: file missing from the export]"
    assert lines[2] == "[30/03/2025, 10:00:01] A: [📞 Call: Incoming (57m 06s)]"
    assert lines[3] == "[30/03/2025, 10:00:02] A: [⏱ Self-destructing photo (Expired): not included in the export]"
    assert lines[5] == "[30/03/2025, 10:00:02] B: [📌 Pinned a message from 30.03.2025 10:00:00: [Photo] x]"
    assert lines[6] == "[30/03/2025, 10:00:03] B: [↩ Reply to a message that is not in this export]"


def test_annotations_can_be_disabled(make_export):
    blocks = [msg(1, "30.03.2025 10:00:00", "A", "o"),
              msg(2, "30.03.2025 10:00:01", "B", "r", reply="#go_to_message1", reactions=reaction("❤", "A")),
              service(-2, "B pinned", target=1)]
    r = rendered(make_export, blocks, annotations=frozenset())
    assert r.lines == ["[30/03/2025, 10:00:00] A: o", "[30/03/2025, 10:00:01] B: r"]
    assert r.skipped == {"pin": 1}


def test_formatting_modes_and_hidden_links(make_export):
    blocks = [msg(1, "30.03.2025 10:00:00", "A", '<strong>b</strong> <a href="https://y.org">see</a>')]
    assert rendered(make_export, blocks).lines[0].endswith("A: b see (https://y.org)")
    assert rendered(make_export, blocks, formatting="whatsapp").lines[0].endswith("A: *b* see (https://y.org)")


def test_sent_together_note_and_utc_conversion(make_export):
    blocks = [msg(1, "30.03.2025 10:00:00", "A", media=photo("photos/1.jpg")),
              msg(2, "30.03.2025 10:00:00", None, media=photo("photos/2.jpg"))]
    r = rendered(make_export, blocks, files=["photos/1.jpg", "photos/2.jpg"], utc_offset=parse_offset("+00:00"))
    assert r.lines[0].startswith("[30/03/2025, 04:30:00] A: \u200e<attached:")
    assert r.lines[1] == "[30/03/2025, 04:30:00] A: [Sent together: 1/2]"


def test_blank_lines_are_kept_visible_but_not_empty(make_export):
    r = rendered(make_export, [msg(1, "30.03.2025 10:00:00", "A", "one<br><br>two")])
    assert r.lines == ["[30/03/2025, 10:00:00] A: one", "\u2060", "two"]


def test_trailing_whitespace_is_removed_from_every_line(make_export):
    r = rendered(make_export, [msg(1, "30.03.2025 10:00:00", "A", "title <br>body  <br>end")])
    assert r.lines == ["[30/03/2025, 10:00:00] A: title", "body", "end"]


def test_long_multiline_messages_use_the_lower_limit(make_export):
    from telegram_restore.render import MAX_MESSAGE_CHARS, MAX_MULTILINE_CHARS
    single = "x" * (MAX_MESSAGE_CHARS - 50)
    multi = "<br>".join(["y" * 400] * 4)
    r = rendered(make_export, [msg(1, "30.03.2025 10:00:00", "A", single), msg(2, "30.03.2025 10:00:01", "A", multi)])
    assert len(r.starts) == 3
    messages = "\n".join(r.lines).split("\n[")
    assert all(len(m) <= MAX_MULTILINE_CHARS + 40 for m in messages[1:])


def test_overlong_messages_are_split():
    from telegram_restore.render import split_message
    parts = split_message(["a" * 30, "b" * 30, "c" * 75], 64)
    assert parts == [["a" * 30, "b" * 30], ["c" * 64], ["c" * 11]]


def test_chunks_split_only_at_message_starts():
    lines = ["[30/03/2025, 10:00:00] A: aaaa", "cont", "[30/03/2025, 10:00:01] A: bbbb", "cont2"]
    assert chunk_bounds(lines, 40) == [0, 2]
    assert chunk_bounds(lines, 10_000) == [0]
