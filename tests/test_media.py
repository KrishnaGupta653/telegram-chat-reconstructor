from telegram_restore.media import Inspector, magic_mismatch, resolve, upload_spec
from telegram_restore.models import Media


def test_resolve_missing_and_escape(make_export):
    root = make_export([[]], files=["photos/a b.jpg"])
    assert resolve(root, "photos/a%20b.jpg") == (root / "photos/a b.jpg").resolve()
    assert resolve(root, "photos/missing.jpg") is None
    assert resolve(root, "../outside.jpg") is None


def test_inspector_hashes_once_and_detects_mismatch(make_export):
    root = make_export([[]], files=["photos/a.jpg", "stickers/s.webp", "files/fake.ogg"])
    ins = Inspector(root)
    a, b = Media("photo", "photos/a.jpg"), Media("photo", "photos/a.jpg")
    ins.inspect(a)
    ins.inspect(b)
    assert a.exists and a.sniffed == "jpeg" and a.sha256 == b.sha256 and len(ins._cache) == 1
    missing = Media("photo", "photos/none.jpg")
    ins.inspect(missing)
    assert not missing.exists
    wrong = Media("sticker", "photos/a.jpg")
    ins.inspect(wrong)
    assert magic_mismatch(wrong)


def test_upload_specs():
    assert upload_spec(Media("photo", "photos/a.jpg", sniffed="jpeg")) == {"input": "photo"}
    voice = upload_spec(Media("voice", "voice_messages/a.ogg", duration=4, sniffed="ogg"))
    assert voice["attributes"][0] == {"type": "audio", "voice": True, "duration": 4}
    sticker = upload_spec(Media("sticker_animated", "stickers/AnimatedSticker.tgs", emoji="💃"))
    assert sticker["mime"] == "video/webm" and sticker["convert"] == "tgs_to_webm"
    assert sticker["attributes"][1] == {"type": "filename", "name": "AnimatedSticker.webm"}
    m4a = upload_spec(Media("audio", "files/x.mp3", sniffed="mp4"))
    assert m4a["mime"] == "audio/mp4"
