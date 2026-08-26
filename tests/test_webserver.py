"""Flask API contract tests.

The app is created through Flask's test client: no listening server, real Ollama,
or desktop notifier is started.
"""

import importlib.util
import json

import pytest

import config as config_module
import db


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.setenv(config_module.ENV_DATA_DIR, str(tmp_path))
    db.init_db()
    if importlib.util.find_spec("webserver") is None:
        pytest.fail("缺少 webserver.py，无法验证 Flask 页面与 API 公共契约")
    import webserver

    settings = config_module.validate_config(config_module.DEFAULT_CONFIG)
    factory = getattr(webserver, "create_app", None) or getattr(
        webserver, "make_app", None
    )
    assert callable(
        factory
    ), "webserver must expose create_app(config=..., database/db=...)"
    try:
        app = factory(config=settings, database=db)
    except TypeError:
        try:
            app = factory(config=settings, db=db)
        except TypeError:
            app = factory()
    app.config.update(TESTING=True)
    return app, app.test_client(), webserver


def unwrap(response):
    payload = response.get_json()
    assert payload is not None
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def assert_error(response, expected=(400, 404, 422)):
    assert response.status_code in expected
    payload = response.get_json()
    assert isinstance(payload, dict)
    assert payload.get("error") or payload.get("message")


def test_core_pages_render(app_client):
    _, client, _ = app_client
    for route in ("/", "/import", "/manage"):
        response = client.get(route)
        assert response.status_code == 200
        assert b"DeskGuardian" in response.data


def test_parse_rule_mode_and_input_validation(app_client):
    _, client, _ = app_client
    response = client.post(
        "/api/parse",
        json={"text": "高等数学 周一 第1-2节 教室：A101", "mode": "rule"},
    )
    assert response.status_code == 200
    data = unwrap(response)
    assert data["source"] == "rule"
    assert data["courses"][0]["name"] == "高等数学"

    assert_error(client.post("/api/parse", json={"text": "", "mode": "rule"}))
    assert_error(
        client.post("/api/parse", data="not-json", content_type="application/json")
    )


def test_parse_auto_uses_mocked_ai_not_real_ollama(app_client, monkeypatch):
    _, client, webserver = app_client
    expected = {
        "source": "ai",
        "confidence": 0.9,
        "fallback_reason": None,
        "warnings": [],
        "courses": [
            {
                "name": "模拟课程",
                "weekday": 2,
                "start_section": 3,
                "end_section": 4,
                "room": "B201",
            }
        ],
        "candidates": [],
    }

    # Patch every likely imported/public parse entry point on the web module.
    patched = False
    for name in ("parse_timetable", "parse_courses", "parse_schedule"):
        if hasattr(webserver, name):
            monkeypatch.setattr(webserver, name, lambda *args, **kwargs: expected)
            patched = True
    if not patched:
        import ai_parser

        monkeypatch.setattr(
            ai_parser, "parse_timetable", lambda *args, **kwargs: expected
        )

    response = client.post("/api/parse", json={"text": "anything", "mode": "auto"})
    assert response.status_code == 200
    assert unwrap(response)["courses"][0]["name"] == "模拟课程"


def test_course_api_crud_and_import_confirm_is_atomic(app_client):
    _, client, _ = app_client
    payload = {
        "name": "高等数学",
        "weekday": 1,
        "start_section": 1,
        "end_section": 2,
        "room": "A101",
        "is_active": True,
    }
    created_response = client.post("/api/courses", json=payload)
    assert created_response.status_code in (200, 201)
    created = unwrap(created_response)
    course_id = created.get("id") if isinstance(created, dict) else None
    assert course_id

    listed = unwrap(client.get("/api/courses"))
    if isinstance(listed, dict):
        listed = listed.get("courses", listed.get("items"))
    assert [item["name"] for item in listed] == ["高等数学"]

    update = client.put("/api/courses/%s" % course_id, json={"room": "C301"})
    assert update.status_code == 200
    assert unwrap(update)["room"] == "C301"
    assert_error(client.put("/api/courses/999999", json={"room": "x"}), expected=(404,))

    before = len(db.list_courses())
    invalid_batch = client.post(
        "/api/import/confirm",
        json={
            "courses": [
                payload | {"name": "有效"},
                payload | {"name": "无效", "weekday": 9},
            ]
        },
    )
    assert_error(invalid_batch)
    assert len(db.list_courses()) == before

    valid_batch = client.post(
        "/api/import/confirm",
        json={"courses": [payload | {"name": "大学英语", "weekday": 2}]},
    )
    assert valid_batch.status_code in (200, 201)
    assert len(db.list_courses()) == before + 1

    deleted = client.delete("/api/courses/%s" % course_id)
    assert deleted.status_code in (200, 204)
    assert_error(client.delete("/api/courses/%s" % course_id), expected=(404,))


def test_homework_api_crud(app_client):
    _, client, _ = app_client
    payload = {"subject": "数学", "content": "习题", "deadline": "2026-09-01"}
    response = client.post("/api/homeworks", json=payload)
    assert response.status_code in (200, 201)
    created = unwrap(response)
    homework_id = created["id"]

    listed = unwrap(client.get("/api/homeworks"))
    if isinstance(listed, dict):
        listed = listed.get("homeworks", listed.get("items"))
    assert listed[0]["subject"] == "数学"

    updated = client.put(
        "/api/homeworks/%s" % homework_id, json={"content": "习题 1-20"}
    )
    assert updated.status_code == 200
    assert unwrap(updated)["content"] == "习题 1-20"
    assert client.delete("/api/homeworks/%s" % homework_id).status_code in (200, 204)


def test_settings_export_import_and_test_notification_are_mocked(
    app_client, monkeypatch
):
    _, client, webserver = app_client
    calls = []

    class FakeNotifier:
        def notify(self, title, message, **kwargs):
            calls.append((title, message))
            return True

    for name in ("DesktopNotifier", "Notifier"):
        if hasattr(webserver, name):
            monkeypatch.setattr(webserver, name, FakeNotifier)
    for name in ("notify", "send_notification"):
        if hasattr(webserver, name):
            monkeypatch.setattr(
                webserver,
                name,
                lambda title, message, **kwargs: calls.append((title, message)) or True,
            )

    settings_response = client.get("/api/settings")
    assert settings_response.status_code == 200
    update = client.put(
        "/api/settings", json={"reminder_minutes": 12, "autostart": True}
    )
    assert update.status_code == 200
    assert config_module.load_config()["reminder_minutes"] == 12

    notice = client.post("/api/notifications/test")
    assert notice.status_code == 200
    assert calls, "test notification endpoint must use the patched notifier"

    db.add_course(
        {"name": "导出课", "weekday": 3, "start_section": 1, "end_section": 2}
    )
    exported = client.get("/api/export")
    assert exported.status_code == 200
    payload = exported.get_json() or json.loads(exported.data)
    if "data" in payload:
        payload = payload["data"]
    assert payload["courses"]

    imported = client.post(
        "/api/import/json",
        json={
            "courses": [
                {"name": "导入课", "weekday": 4, "start_section": 3, "end_section": 4}
            ],
            "homeworks": [],
            "settings": {},
        },
    )
    assert imported.status_code in (200, 201)
    assert any(item["name"] == "导入课" for item in db.list_courses())
