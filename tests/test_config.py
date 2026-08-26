import json
from pathlib import Path

import pytest

import config


def test_data_dir_override_is_isolated(monkeypatch, tmp_path):
    data_dir = tmp_path / "portable data"
    monkeypatch.setenv(config.ENV_DATA_DIR, str(data_dir))

    assert Path(config.get_data_dir()) == data_dir.resolve()
    assert Path(config.get_config_path()) == data_dir.resolve() / "config.json"
    assert Path(config.get_db_path()) == data_dir.resolve() / "deskguardian.db"
    assert data_dir.is_dir()


def test_load_missing_config_creates_complete_defaults(tmp_path):
    path = tmp_path / "nested" / "config.json"

    loaded = config.load_config(path)

    assert loaded["reminder_minutes"] == 5
    assert loaded["host"] == "127.0.0.1"
    assert len(loaded["section_times"]) == 8
    assert json.loads(path.read_text(encoding="utf-8")) == loaded


def test_load_without_create_does_not_write(tmp_path):
    path = tmp_path / "config.json"
    loaded = config.load_config(path, create=False)

    assert loaded == config.validate_config(config.DEFAULT_CONFIG)
    assert not path.exists()


def test_partial_config_deep_merges_and_is_persisted(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "reminder_minutes": "10",
                "section_times": {"1": {"start": "08:10", "end": "08:50"}},
            }
        ),
        encoding="utf-8",
    )

    loaded = config.load_config(path)

    assert loaded["reminder_minutes"] == 10
    assert loaded["section_times"]["1"] == {"start": "08:10", "end": "08:50"}
    assert loaded["section_times"]["2"] == config.DEFAULT_SECTION_TIMES["2"]
    assert json.loads(path.read_text(encoding="utf-8"))["port"] == 8080


def test_update_config_deep_merges_without_losing_other_values(tmp_path):
    path = tmp_path / "config.json"
    config.save_config(config.DEFAULT_CONFIG, path)

    updated = config.update_config(
        {"beep": True, "section_times": {"8": {"start": "18:00", "end": "18:45"}}},
        path,
    )

    assert updated["beep"] is True
    assert updated["section_times"]["8"]["start"] == "18:00"
    assert updated["section_times"]["1"] == config.DEFAULT_SECTION_TIMES["1"]
    assert config.load_config(path) == updated


@pytest.mark.parametrize(
    "updates",
    [
        {"port": 0},
        {"port": True},
        {"reminder_minutes": -1},
        {"daily_summary_time": "7:00"},
        {"autostart": "true"},
        {"section_times": {"1": {"start": "09:00", "end": "08:00"}}},
    ],
)
def test_invalid_config_is_rejected(updates):
    with pytest.raises(ValueError):
        config.validate_config(updates)


def test_invalid_json_reports_value_error(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="有效 JSON"):
        config.load_config(path)


def test_save_returns_copy_not_mutable_internal_default(tmp_path):
    path = tmp_path / "config.json"
    saved = config.save_config(config.DEFAULT_CONFIG, path)
    saved["section_times"]["1"]["start"] = "00:00"

    reloaded = config.load_config(path)
    assert reloaded["section_times"]["1"]["start"] == "08:00"
    assert config.DEFAULT_SECTION_TIMES["1"]["start"] == "08:00"
