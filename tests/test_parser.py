from conftest import block_media, date_sep, msg, photo, reaction, service

from telegram_restore.parser import iter_messages


def parse(root):
    errors = []
    out = list(iter_messages(root, on_error=lambda f, i, e: errors.append((f, i, e))))
    return out, errors


def test_multiple_pages_numeric_order_and_joined_sender_across_pages(make_export):
    pages = [[date_sep(-1), msg(1, "30.03.2025 17:50:50", "Alice", "hi")]]
    pages += [[] for _ in range(9)]
    pages.append([msg(2, "30.03.2025 17:51:00", None, "still alice")])  # messages11.html
    root = make_export(pages)
    out, errors = parse(root)
    assert not errors
    real = [m for m in out if not m.is_service]
    assert [m.source_id for m in real] == [1, 2]
    assert real[1].sender == "Alice" and real[1].joined
    assert real[1].html_file == "messages11.html"


def test_text_br_entities_and_links(make_export):
    text = ('<strong>Bold</strong> and <em>it</em><br><br>line &amp; <a href="https://x.org">https://x.org</a>'
            ' <a href="https://y.org/p">label</a> <s>gone</s> <code>c</code>')
    root = make_export([[msg(1, "30.03.2025 17:50:50", "Alice", text)]])
    (m,), _ = parse(root)
    assert m.text == "Bold and it\n\nline & https://x.org label gone c"
    kinds = {(e.type, m.text[e.offset:e.offset + e.length], e.url) for e in m.entities}
    assert ("bold", "Bold", None) in kinds
    assert ("italic", "it", None) in kinds
    assert ("url", "https://x.org", "https://x.org") in kinds
    assert ("text_link", "label", "https://y.org/p") in kinds
    assert ("strike", "gone", None) in kinds and ("code", "c", None) in kinds


def test_media_classification(make_export):
    blocks = [
        msg(1, "30.03.2025 10:00:00", "A", "cap", media=photo("photos/p1.jpg")),
        msg(2, "30.03.2025 10:00:01", "A", media='<a class="video_file_wrap clearfix pull_left" href="video_files/v.mp4">'
                                                  '<div class="video_duration">\n01:05\n</div><img class="video_file" src="t.jpg"/></a>'),
        msg(3, "30.03.2025 10:00:02", "A", media='<a class="animated_wrap clearfix pull_left" href="video_files/g.mp4"><img class="animated" src="t.jpg"/></a>'),
        msg(4, "30.03.2025 10:00:03", "A", media='<a class="sticker_wrap clearfix pull_left" href="stickers/s.webp"><img class="sticker" src="s_thumb.webp"/></a>'),
        msg(5, "30.03.2025 10:00:04", "A", media=block_media("block_link media_photo", "stickers/AnimatedSticker.tgs", "Sticker", "💃")),
        msg(6, "30.03.2025 10:00:05", "A", media=block_media("block_link media_photo", "video_files/sticker.webm", "Sticker", "🙂")),
        msg(7, "30.03.2025 10:00:06", "A", media=block_media("block_link media_video", "video_files/cubs.mp4", "Animation", "104.7 KB")),
        msg(8, "30.03.2025 10:00:07", "A", media=block_media("block_link media_voice_message", "voice_messages/a.ogg", "Voice message", "00:04")),
        msg(9, "30.03.2025 10:00:08", "A", media=block_media("block_link media_audio_file", "files/s.mp3", "s.mp3", "03:27")),
        msg(10, "30.03.2025 10:00:09", "A", media=block_media("block_link media_file", "files/d.pdf", "d.pdf", "713.5 KB")),
        msg(11, "30.03.2025 10:00:10", "A", "x", media=block_media("media_photo", None, "Self-destructing photo", "Expired")),
        msg(12, "30.03.2025 10:00:11", "A", media=block_media("media_video", None, "Self-destructing video", "Expired")),
        msg(13, "30.03.2025 10:00:12", "A", media=block_media("media_call success", None, "Bob", "Incoming (3426 seconds)")),
        msg(14, "30.03.2025 10:00:13", "A", media=block_media("block_link media_photo", "photos/p2.jpg", "Photo", "935×99")),
        msg(15, "30.03.2025 10:00:14", "A", media=block_media("media_dice", None, "Dice", "6")),
    ]
    out, errors = parse(make_export([blocks]))
    assert not errors
    assert [m.kind for m in out] == ["photo", "video", "gif", "sticker", "sticker_animated", "sticker_video", "gif",
                                     "voice", "audio", "document", "ttl_photo", "ttl_video", "call", "photo", "unknown"]
    assert out[0].text == "cap"
    assert out[1].media.duration == 65 and out[7].media.duration == 4
    assert out[4].media.emoji == "💃"
    assert out[10].media.status == "Expired" and out[10].media.rel_path is None


def test_reply_forward_reactions_and_joined_forward(make_export):
    fwd_head = '<div class="from_name">\nCarol <span class="date details" title="28.02.2025 14:52:06 UTC+05:30"> 28.02.2025</span>\n</div>'
    blocks = [
        msg(1, "30.03.2025 10:00:00", "A", "orig"),
        msg(2, "30.03.2025 10:00:01", "B", "re", reply="messages.html#go_to_message1", reactions=reaction("❤", "A")),
        msg(3, "30.03.2025 10:00:02", "B", forward=fwd_head, media=photo("photos/a.jpg")),
        msg(4, "30.03.2025 10:00:02", None, forward="", media=photo("photos/b.jpg")),
    ]
    out, errors = parse(make_export([blocks]))
    assert not errors
    assert out[1].reply_to_id == 1
    assert out[1].reactions == [{"emoji": "❤", "actors": ["A"]}]
    assert out[2].forward == {"from_name": "Carol", "date": "2025-02-28T14:52:06+05:30"}
    assert out[3].forward["from_name"] == "Carol" and out[3].forward["inherited"]


def test_service_messages_inherit_time(make_export):
    blocks = [date_sep(-1), msg(1, "30.03.2025 10:00:00", "A", "x"),
              service(-2, "B pinned", target=1), service(-3, "B set a new background for this chat")]
    out, _ = parse(make_export([blocks]))
    pin, bg = out[2], out[3]
    assert pin.service_type == "pin" and pin.pin_target == 1 and pin.sender == "B"
    assert pin.ts == out[1].ts and pin.ts_inferred
    assert bg.service_type == "set_background" and bg.sender == "B"
    assert out[0].service_type == "date_separator"


def test_malformed_block_is_reported_not_fatal(make_export):
    bad = '<div class="message default clearfix" id="message2"><div class="body"><div class="text">no date</div></div></div>'
    out, errors = parse(make_export([[msg(1, "30.03.2025 10:00:00", "A", "ok"), bad, msg(3, "30.03.2025 10:00:01", "A", "ok")]]))
    assert [m.source_id for m in out] == [1, 3]
    assert errors == [("messages.html", 2, "missing or unparseable timestamp")]
