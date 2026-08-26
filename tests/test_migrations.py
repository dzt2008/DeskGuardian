import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

import config
import db


def _user_version(path):
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def _simple_payload(**metadata):
    return {
        **metadata,
        "courses": [],
        "homeworks": [],
        "settings": {},
    }


def test_config_version_zero_is_backed_up_and_migrated_once(tmp_path):
    path = tmp_path / "config.json"
    original = json.dumps({"reminder_minutes": "10"}, ensure_ascii=False).encode(
        "utf-8"
    )
    path.write_bytes(original)

    loaded = config.load_config(path)

    backup = tmp_path / "config.json.v0.bak"
    assert loaded["config_version"] == config.CURRENT_CONFIG_VERSION == 1
    assert backup.read_bytes() == original
    assert json.loads(path.read_text(encoding="utf-8"))["config_version"] == 1

    backup_before = backup.read_bytes()
    config.load_config(path)
    assert backup.read_bytes() == backup_before
    assert list(tmp_path.glob("config.json.v*.bak")) == [backup]


def test_config_create_false_migrates_in_memory_without_backup(tmp_path):
    path = tmp_path / "config.json"
    original = b'{"beep": true}'
    path.write_bytes(original)

    loaded = config.load_config(path, create=False)

    assert loaded["config_version"] == 1
    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.bak")) == []


def test_future_config_is_rejected_without_modification_or_backup(tmp_path):
    path = tmp_path / "config.json"
    original = b'{"config_version": 2, "beep": true}\n'
    path.write_bytes(original)

    with pytest.raises(ValueError, match="高于当前支持"):
        config.load_config(path)

    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.bak")) == []


def test_broken_config_is_not_overwritten_or_backed_up(tmp_path):
    path = tmp_path / "config.json"
    original = b"{broken"
    path.write_bytes(original)

    with pytest.raises(ValueError, match="有效 JSON"):
        config.load_config(path)

    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.bak")) == []


def test_existing_version_zero_database_is_backed_up_and_adopted(tmp_path):
    path = tmp_path / "deskguardian.db"
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(db._SCHEMA)
        connection.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)",
            ("legacy", '"kept"', "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()

    db.init_db(path)

    backup = tmp_path / "deskguardian.db.v0.bak"
    assert backup.is_file()
    assert _user_version(backup) == 0
    assert _user_version(path) == db.SCHEMA_VERSION == 1
    assert db.get_setting("legacy", db_path=path) == "kept"

    backup_before = backup.read_bytes()
    db.init_db(path)
    assert backup.read_bytes() == backup_before
    assert list(tmp_path.glob("deskguardian.db.v*.bak")) == [backup]


def test_existing_empty_version_zero_database_is_adopted(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(str(path)).close()

    db.init_db(path)

    assert _user_version(path) == 1
    assert (tmp_path / "empty.db.v0.bak").is_file()
    assert db.list_courses(db_path=path) == []


def test_failed_database_migration_rolls_back_version_and_schema(
    monkeypatch, tmp_path
):
    path = tmp_path / "failed.db"
    sqlite3.connect(str(path)).close()

    def failing_migration(connection):
        connection.execute("CREATE TABLE migration_marker(value TEXT)")
        raise RuntimeError("planned migration failure")

    monkeypatch.setattr(db, "MIGRATIONS", {0: (1, failing_migration)})

    with pytest.raises(RuntimeError, match="planned migration failure"):
        db.init_db(path)

    connection = sqlite3.connect(str(path))
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        marker = connection.execute(
            "SELECT name FROM sqlite_master WHERE name='migration_marker'"
        ).fetchone()
        assert marker is None
    finally:
        connection.close()
    assert (tmp_path / "failed.db.v0.bak").is_file()


def test_future_database_is_rejected_without_rewriting(tmp_path):
    path = tmp_path / "future.db"
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA user_version = 2")
    finally:
        connection.close()
    original = path.read_bytes()

    with pytest.raises(RuntimeError, match="高于当前支持"):
        db.init_db(path)

    assert path.read_bytes() == original
    assert _user_version(path) == 2
    assert list(tmp_path.glob("*.bak")) == []


def test_export_contains_explicit_format_and_application_versions(tmp_path):
    path = tmp_path / "export.db"

    payload = db.export_data(db_path=path)

    assert payload["format"] == "DeskGuardian"
    assert payload["version"] == 1
    assert payload["format_version"] == db.DATA_FORMAT_VERSION == 1
    assert payload["app_version"] == db.APP_VERSION


def test_import_accepts_legacy_official_and_unversioned_simple_payloads(tmp_path):
    path = tmp_path / "import.db"

    legacy = _simple_payload(format="DeskGuardian", version=1)
    assert db.import_data(legacy, db_path=path) == {
        "courses": 0,
        "homeworks": 0,
        "settings": 0,
    }
    assert db.import_data(_simple_payload(), db_path=path) == {
        "courses": 0,
        "homeworks": 0,
        "settings": 0,
    }


@pytest.mark.parametrize(
    "metadata, message",
    [
        ({"format": "Other", "format_version": 1}, "format"),
        ({"format": "DeskGuardian", "format_version": 2}, "高于当前支持"),
        ({"format_version": 2}, "高于当前支持"),
        ({"format": "DeskGuardian", "format_version": "1"}, "正整数"),
        (
            {"format": "DeskGuardian", "format_version": 1, "version": 2},
            "不一致",
        ),
    ],
)
def test_import_rejects_wrong_or_future_format(metadata, message, tmp_path):
    path = tmp_path / "rejected.db"

    with pytest.raises(ValueError, match=message):
        db.import_data(_simple_payload(**metadata), db_path=path)

    assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions do not apply")
def test_runtime_data_files_use_owner_only_permissions(tmp_path):
    data_dir = tmp_path / "private"
    config_path = data_dir / "config.json"
    db_path = data_dir / "deskguardian.db"

    config.save_config(config.DEFAULT_CONFIG, config_path)
    db.init_db(db_path)

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        auxiliary = Path(str(db_path) + suffix)
        if auxiliary.exists():
            assert stat.S_IMODE(auxiliary.stat().st_mode) == 0o600
