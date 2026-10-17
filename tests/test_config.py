import pytest

from telegram_restore import config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("EXPORT_DIR", "WORK_DIR", "TG_API_ID", "TG_API_HASH", "TG_SESSION", "DESTINATION", "ANNOTATE",
                "REPLY_QUOTE_CHARS"):
        monkeypatch.delenv(key, raising=False)


def write_env(tmp_path, body):
    (tmp_path / "export").mkdir(exist_ok=True)
    env = tmp_path / "proj" / ".env"
    env.parent.mkdir(exist_ok=True)
    env.write_text(body.format(export=tmp_path / "export"), encoding="utf-8")
    return env


def test_relative_paths_resolve_against_env_dir(tmp_path, monkeypatch):
    env = write_env(tmp_path, "EXPORT_DIR={export}\nWORK_DIR=work\n")
    monkeypatch.chdir(tmp_path)  # a different cwd must not matter
    cfg = config.load(env)
    assert cfg.work_dir == (tmp_path / "proj" / "work").resolve()
    assert cfg.session == cfg.work_dir / "telegram"
    assert cfg.api_id is None and cfg.annotations == frozenset(config.ALL_ANNOTATIONS)


def test_work_dir_inside_export_is_refused(tmp_path):
    env = write_env(tmp_path, "EXPORT_DIR={export}\nWORK_DIR={export}/out\n")
    with pytest.raises(SystemExit, match="outside EXPORT_DIR"):
        config.load(env)
    assert not (tmp_path / "export" / "out").exists()


def test_bad_values_are_rejected(tmp_path):
    with pytest.raises(SystemExit, match="must be a number"):
        config.load(write_env(tmp_path, "EXPORT_DIR={export}\nTG_API_ID=abc\n"))
    with pytest.raises(SystemExit, match="REPLY_QUOTE_CHARS"):
        config.load(write_env(tmp_path, "EXPORT_DIR={export}\nREPLY_QUOTE_CHARS=lots\n"))


def test_missing_env_and_export(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        config.load(tmp_path / "nope.env")
    with pytest.raises(SystemExit, match="does not exist"):
        config.load(write_env(tmp_path, "EXPORT_DIR={export}/missing\n"))


def test_probe_defaults_to_newest_dry_run_of_each_format(tmp_path):
    import main

    cfg = config.load(write_env(tmp_path, "EXPORT_DIR={export}\n"))
    for run in ("ios-20260101-000000", "ios-20260102-000000", "android-20260101-000000"):
        (cfg.work_dir / "dry_run" / run).mkdir(parents=True)
        (cfg.work_dir / "dry_run" / run / "chat_001.txt").write_text("x", encoding="utf-8")
    assert [f.parent.name for f in main.latest_dry_run_files(cfg)] == ["ios-20260102-000000", "android-20260101-000000"]
