import json

import pytest

import db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "state" / "deskguardian.db"
    db.init_db(path)
    return path


def course(name="高等数学", weekday=1, start=1, end=2, room="A101", active=True):
    return {
        "name": name,
        "weekday": weekday,
        "start_section": start,
        "end_section": end,
        "room": room,
        "is_active": active,
    }


def homework(subject="数学", content="习题 1-10", deadline="2026-09-01"):
    return {"subject": subject, "content": content, "deadline": deadline}


def test_init_db_is_idempotent_and_creates_schema(db_path):
    db.init_db(db_path)
    with db.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert {"courses", "homeworks", "settings", "notification_events"} <= tables
    assert version == db.SCHEMA_VERSION


def test_course_crud_filters_and_missing_records(db_path):
    math = db.add_course(course(), db_path=db_path)
    english = db.add_course(course("大学英语", 2, 3, 4, "B202", False), db_path=db_path)

    assert math["id"] > 0
    assert math["is_active"] is True
    assert db.get_course(math["id"], db_path)["name"] == "高等数学"
    assert [
        item["id"] for item in db.list_courses(weekday="周一", db_path=db_path)
    ] == [math["id"]]
    assert [item["id"] for item in db.list_courses(search="英语", db_path=db_path)] == [
        english["id"]
    ]
    assert [
        item["id"] for item in db.list_courses(active_only=True, db_path=db_path)
    ] == [math["id"]]

    updated = db.update_course(
        math["id"], {"room": "新楼 C301", "is_active": False}, db_path
    )
    assert updated["room"] == "新楼 C301"
    assert updated["name"] == "高等数学"
    assert updated["is_active"] is False
    assert db.update_course(999999, {"room": "none"}, db_path) is None

    assert db.delete_course(english["id"], db_path) is True
    assert db.delete_course(english["id"], db_path) is False
    assert db.get_course(english["id"], db_path) is None


def test_course_validation_rejects_unknown_or_invalid_fields(db_path):
    with pytest.raises(ValueError):
        db.add_course(course(start=4, end=2), db_path=db_path)
    with pytest.raises(ValueError, match="未知课程字段"):
        db.add_course({**course(), "unexpected": 1}, db_path=db_path)
    assert db.list_courses(db_path=db_path) == []


def test_bulk_course_insert_is_ordered_and_all_or_nothing(db_path):
    inserted = db.bulk_add_courses(
        [course("晚课", 5, 7, 8), course("早课", 1, 1, 2)], db_path
    )
    assert len(inserted) == 2
    assert [item["name"] for item in db.list_courses(db_path=db_path)] == [
        "早课",
        "晚课",
    ]

    before = len(db.list_courses(db_path=db_path))
    with pytest.raises(ValueError):
        db.bulk_add_courses([course("有效", 2), course("无效", 9)], db_path)
    assert len(db.list_courses(db_path=db_path)) == before


def test_homework_crud_filters_and_notification_flags(db_path):
    first = db.add_homework(homework(deadline="2026-09-01"), db_path=db_path)
    second = db.add_homework(homework("英语", "Essay", "2026-09-03"), db_path=db_path)

    assert first["deadline"] == "2026-09-01"
    assert first["notified_day1"] is False
    assert [
        item["id"]
        for item in db.list_homeworks(due_before="2026-09-01", db_path=db_path)
    ] == [first["id"]]
    assert [
        item["id"]
        for item in db.list_homeworks(due_after="2026-09-02", db_path=db_path)
    ] == [second["id"]]

    updated = db.update_homework(first["id"], {"content": "习题 1-20"}, db_path)
    assert updated["content"] == "习题 1-20"
    assert db.mark_homework_notified(first["id"], 1, db_path) is True
    assert db.get_homework(first["id"], db_path)["notified_day1"] is True
    assert db.delete_homework(second["id"], db_path) is True
    assert db.get_homework(second["id"], db_path) is None


def test_settings_round_trip_json_types(db_path):
    db.set_setting("number", 3, db_path)
    db.set_setting("object", {"enabled": True}, db_path)

    assert db.get_setting("number", db_path=db_path) == 3
    assert db.get_setting("object", db_path=db_path) == {"enabled": True}
    assert db.get_setting("missing", "fallback", db_path) == "fallback"
    assert db.list_settings(db_path) == {"number": 3, "object": {"enabled": True}}
    assert db.delete_setting("number", db_path) is True
    assert db.delete_setting("number", db_path) is False


def test_notification_claim_is_unique_and_completion_prevents_reclaim(db_path):
    claimed = db.claim_notification(
        "course:1:2026-03-16",
        "course",
        entity_type="course",
        entity_id=1,
        scheduled_for="2026-03-16T07:55:00",
        payload={"name": "高等数学"},
        db_path=db_path,
    )
    assert claimed and claimed["status"] == "claimed"
    assert claimed["payload"] == {"name": "高等数学"}
    assert (
        db.claim_notification("course:1:2026-03-16", "course", db_path=db_path) is None
    )
    assert (
        db.complete_notification(
            claimed["event_key"], claim_token="wrong", db_path=db_path
        )
        is False
    )
    assert (
        db.complete_notification(
            claimed["event_key"], claim_token=claimed["claim_token"], db_path=db_path
        )
        is True
    )
    assert (
        db.claim_notification("course:1:2026-03-16", "course", db_path=db_path) is None
    )


def test_failed_notification_can_be_reclaimed(db_path):
    first = db.claim_notification("test:retry", "test", db_path=db_path)
    assert (
        db.fail_notification(
            "test:retry", "backend down", first["claim_token"], db_path
        )
        is True
    )
    second = db.claim_notification("test:retry", "test", db_path=db_path)
    assert second is not None
    assert second["claim_token"] != first["claim_token"]
    assert second["status"] == "claimed"


def test_export_and_replace_import_round_trip_without_notification_history(
    db_path, tmp_path
):
    db.add_course(course(), db_path=db_path)
    db.add_homework(homework(), db_path=db_path)
    db.set_setting("theme", "dark", db_path)
    db.claim_notification("old:event", "test", db_path=db_path)

    export_path = tmp_path / "backup" / "data.json"
    text = db.export_json(export_path, db_path)
    payload = json.loads(text)
    assert export_path.read_text(encoding="utf-8") == text
    assert "notification_events" not in payload

    db.add_course(course("待替换", 2), db_path=db_path)
    counts = db.import_json(export_path, mode="replace", db_path=db_path)
    assert counts == {"courses": 1, "homeworks": 1, "settings": 1}
    assert [item["name"] for item in db.list_courses(db_path=db_path)] == ["高等数学"]
    assert db.get_setting("theme", db_path=db_path) == "dark"
    # Import deliberately does not overwrite reminder history.
    assert db.get_notification_event("old:event", db_path) is not None


def test_import_validation_happens_before_replace_transaction(db_path):
    db.add_course(course(), db_path=db_path)
    bad = {"courses": [course("坏课程", 99)], "homeworks": [], "settings": {}}

    with pytest.raises(ValueError):
        db.import_data(bad, mode="replace", db_path=db_path)
    assert [item["name"] for item in db.list_courses(db_path=db_path)] == ["高等数学"]
