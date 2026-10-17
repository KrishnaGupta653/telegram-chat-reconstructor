import asyncio
import json
from pathlib import Path

import pytest
from conftest import msg, photo
from telethon import errors, types
from telethon.tl import functions

from telegram_restore import database, importer
from telegram_restore.config import ALL_ANNOTATIONS, Config
from telegram_restore.render import RenderOptions
from telegram_restore.scanner import scan

OPTS = RenderOptions(annotations=frozenset(ALL_ANNOTATIONS))
BLOCKS = [
    msg(1, "30.03.2025 10:00:00", "A", "hello"),
    msg(2, "30.03.2025 10:00:05", "B", media=photo("photos/a.jpg")),
    msg(3, "30.03.2025 10:00:09", "A", "again", media=photo("photos/a.jpg")),
    msg(4, "30.03.2025 10:00:12", "B", "re", reply="#go_to_message1"),
]


class FakeClient:
    def __init__(self, reuse_ok=True, fail_on_upload=None):
        self.calls, self.reuse_ok, self.fail_on_upload, self.uploads = [], reuse_ok, fail_on_upload, 0

    async def upload_file(self, f, file_name=None):
        self.calls.append("upload_file")
        return types.InputFile(id=1, parts=1, name=file_name or "f", md5_checksum="")

    async def __call__(self, req):
        self.calls.append(type(req).__name__)
        if isinstance(req, functions.messages.InitHistoryImportRequest):
            return types.messages.HistoryImport(id=42)
        if isinstance(req, functions.messages.UploadImportedMediaRequest):
            self.uploads += 1
            if self.uploads == self.fail_on_upload:
                raise errors.FloodWaitError(request=req, capture=10**6)
            if isinstance(req.media, types.InputMediaPhoto) and not self.reuse_ok:
                raise errors.MediaInvalidError(request=req)
            return types.MessageMediaPhoto(photo=types.Photo(id=7, access_hash=8, file_reference=b"", date=None,
                                                             sizes=[], dc_id=1))
        if isinstance(req, functions.messages.StartHistoryImportRequest):
            return True
        raise AssertionError(req)


@pytest.fixture
def setup(make_export, tmp_path):
    root = make_export([BLOCKS], files=["photos/a.jpg"])
    cfg = Config(export_dir=root, work_dir=tmp_path / "work", api_id=1, api_hash="x", session=tmp_path / "s",
                 destination="@d", annotations=frozenset(ALL_ANNOTATIONS), reply_quote_chars=80)
    cfg.work_dir.mkdir()
    scan(cfg)
    conn = database.connect(cfg.db_path)
    return cfg, conn


def run_batch(cfg, conn, client, batch):
    imp = importer.Importer(cfg, client, "peer", conn, max_flood_wait=900)
    runs = importer._runs(conn, batch)
    manifest = json.loads((Path(runs[0]["chat_file"]).parent / "media_manifest.json").read_text(encoding="utf-8"))
    for run in runs:
        asyncio.run(imp.run_chunk(run, [m for m in manifest if m["chunk"] == run["chunk_index"]]))
    return imp


def test_full_flow_reuses_identical_files(setup):
    cfg, conn = setup
    batch = importer.prepare(cfg, conn, "full", OPTS, "@d", 30 * 2**20)
    client = FakeClient()
    imp = run_batch(cfg, conn, client, batch)
    assert client.calls[:2] == ["upload_file", "InitHistoryImportRequest"]
    assert client.calls[-1] == "StartHistoryImportRequest"
    assert (imp.stats.uploaded, imp.stats.reused) == (1, 1)
    assert importer._runs(conn, batch)[0]["status"] == "done"


def test_reuse_rejected_falls_back_to_fresh_upload(setup):
    cfg, conn = setup
    batch = importer.prepare(cfg, conn, "full", OPTS, "@d", 30 * 2**20)
    imp = run_batch(cfg, conn, FakeClient(reuse_ok=False), batch)
    assert (imp.stats.uploaded, imp.stats.reused) == (2, 0) and imp.stats.reuse_disabled


def test_long_flood_wait_stops_and_resume_does_not_reinit(setup):
    cfg, conn = setup
    batch = importer.prepare(cfg, conn, "full", OPTS, "@d", 30 * 2**20)
    with pytest.raises(importer.StopImport, match="wait"):
        run_batch(cfg, conn, FakeClient(fail_on_upload=2), batch)
    assert importer._runs(conn, batch)[0]["status"] == "uploading"
    assert importer.unfinished_batch(conn) == batch

    client = FakeClient()
    run_batch(cfg, conn, client, batch)
    assert "InitHistoryImportRequest" not in client.calls
    assert client.calls.count("UploadImportedMediaRequest") == 1  # only the file not yet uploaded
    assert importer._runs(conn, batch)[0]["status"] == "done"


def test_duplicates_are_refused(setup):
    cfg, conn = setup
    batch = importer.prepare(cfg, conn, "full", OPTS, "@d", 30 * 2**20)
    run_batch(cfg, conn, FakeClient(), batch)
    with pytest.raises(SystemExit, match="already ran"):
        importer.prepare(cfg, conn, "full", OPTS, "@d", 30 * 2**20)


def test_abandon_only_unstarted(setup):
    cfg, conn = setup
    batch = importer.prepare(cfg, conn, "test", OPTS, "@d", 30 * 2**20)
    assert importer.abandon(cfg) == 0
    assert importer._runs(conn, batch)[0]["status"] == "abandoned"
    assert importer.unfinished_batch(conn) is None


def test_test_sample_keeps_reply_context_from_full_export(setup):
    cfg, conn = setup
    msgs = list(database.iter_messages(conn))
    sample = importer.select_test_messages(msgs, head=0)
    assert [m.source_id for m in sample] == sorted(m.source_id for m in sample)
    batch = importer.prepare(cfg, conn, "test", OPTS, "@d", 30 * 2**20)
    text = Path(importer._runs(conn, batch)[0]["chat_file"]).read_text(encoding="utf-8")
    assert "not in this export" not in text
