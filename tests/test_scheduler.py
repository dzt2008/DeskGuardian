from datetime import datetime
from pathlib import Path

import config as config_module
import db
import scheduler


class RecordingNotifier:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def notify(self, title, message):
        self.calls.append((title, message))
        if self.fail:
            raise RuntimeError("mock notification failure")
        return True


def scheduler_config():
    value = config_module.validate_config(config_module.DEFAULT_CONFIG)
    value["daily_summary_time"] = "07:00"
    value["reminder_minutes"] = 5
    return value


def test_repeated_scan_does_not_send_same_summary_or_course_twice(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(config_module.ENV_DATA_DIR, str(tmp_path))
    db_path = Path(config_module.get_db_path())
    db.init_db()
    db.add_course(
        {
            "name": "高等数学",
            "weekday": 1,
            "start_section": 1,
            "end_section": 2,
            "room": "A101",
        }
    )
    notifier = RecordingNotifier()
    guardian = scheduler.Scheduler(db, config=scheduler_config(), notifier=notifier)
    now = datetime(2026, 3, 16, 7, 56)  # Monday, within the 5-minute course window.

    first = guardian.scan_once(now)
    second = guardian.scan_once(now)

    assert first == {"claimed": 2, "sent": 2, "failed": 0, "skipped": 0, "errors": []}
    assert second["claimed"] == 0
    assert second["sent"] == 0
    assert second["skipped"] == 2
    assert len(notifier.calls) == 2
    assert {title for title, _ in notifier.calls} == {"今日课程简报", "课程即将开始"}
    assert len(db.list_notification_events(status="completed", db_path=db_path)) == 2


def test_homework_day1_and_day0_each_send_once(monkeypatch, tmp_path):
    monkeypatch.setenv(config_module.ENV_DATA_DIR, str(tmp_path))
    db.init_db()
    db.add_homework(
        {"subject": "数学", "content": "完成习题", "deadline": "2026-03-17"}
    )
    notifier = RecordingNotifier()
    guardian = scheduler.Scheduler(db, config=scheduler_config(), notifier=notifier)

    day1 = guardian.scan_once(datetime(2026, 3, 16, 8, 0))
    day1_repeat = guardian.scan_once(datetime(2026, 3, 16, 9, 0))
    day0 = guardian.scan_once(datetime(2026, 3, 17, 8, 0))
    day0_repeat = guardian.scan_once(datetime(2026, 3, 17, 9, 0))

    # Each date also has one daily summary, so isolate homework calls/counters by title.
    homework_messages = [
        message for title, message in notifier.calls if title == "作业截止提醒"
    ]
    assert len(homework_messages) == 2
    assert any("明天" in message for message in homework_messages)
    assert any("今天" in message for message in homework_messages)
    assert day1["sent"] == 2
    assert day1_repeat["skipped"] == 2
    assert day0["sent"] == 2
    assert day0_repeat["skipped"] == 2


def test_notification_failure_is_recorded_without_real_desktop_call(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(config_module.ENV_DATA_DIR, str(tmp_path))
    db_path = Path(config_module.get_db_path())
    db.init_db()
    notifier = RecordingNotifier(fail=True)
    guardian = scheduler.Scheduler(db, config=scheduler_config(), notifier=notifier)

    result = guardian.scan_once(datetime(2026, 3, 16, 7, 0))

    assert result["claimed"] == 1
    assert result["sent"] == 0
    assert result["failed"] == 1
    assert "mock notification failure" in result["errors"][0]["error"]
    events = db.list_notification_events(status="failed", db_path=db_path)
    assert len(events) == 1
    assert "mock notification failure" in events[0]["error"]


def test_inactive_and_out_of_window_courses_are_not_notified(monkeypatch, tmp_path):
    monkeypatch.setenv(config_module.ENV_DATA_DIR, str(tmp_path))
    db.init_db()
    db.add_course(
        {
            "name": "停用课程",
            "weekday": 1,
            "start_section": 1,
            "end_section": 1,
            "is_active": False,
        }
    )
    notifier = RecordingNotifier()
    settings = scheduler_config()
    settings["daily_summary_time"] = "23:00"
    guardian = scheduler.Scheduler(db, config=settings, notifier=notifier)

    result = guardian.scan_once(datetime(2026, 3, 16, 7, 56))

    assert result["sent"] == 0
    assert notifier.calls == []
