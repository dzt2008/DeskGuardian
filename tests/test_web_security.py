"""Security regression tests for the local-only Flask application."""

import re
from pathlib import Path

import pytest

import config as config_module
import db
import webserver


@pytest.fixture
def secure_app(monkeypatch, tmp_path):
    monkeypatch.setenv(config_module.ENV_DATA_DIR, str(tmp_path))
    db.init_db()
    settings = config_module.validate_config(config_module.DEFAULT_CONFIG)
    app = webserver.create_app(config=settings, database=db)
    # Production behavior is intentional here: CSRF must not be bypassed.
    app.config.update(TESTING=False)
    return app, app.test_client()


def request_headers(app, **extra):
    raw_headers = {
        "Host": "localhost:8080",
        "X-DeskGuardian-CSRF": app.extensions["deskguardian_csrf_token"],
    }
    raw_headers.update(extra)
    return raw_headers


def test_csrf_token_is_injected_and_required_in_production(secure_app):
    app, client = secure_app
    token = app.extensions["deskguardian_csrf_token"]
    assert token == app.extensions["csrf_token"]

    page = client.get("/", headers={"Host": "localhost:8080"})
    assert page.status_code == 200
    assert (
        'name="deskguardian-csrf-token" content="%s"' % token
    ).encode() in page.data

    missing = client.post(
        "/api/parse",
        json={"text": "高等数学 周一 第1-2节", "mode": "rule"},
        headers={"Host": "localhost:8080"},
    )
    assert missing.status_code == 403
    assert missing.get_json()["error"]["code"] == "CSRF_FAILED"

    wrong = client.post(
        "/api/parse",
        json={"text": "高等数学 周一 第1-2节", "mode": "rule"},
        headers={
            "Host": "localhost:8080",
            "X-DeskGuardian-CSRF": "not-the-token",
        },
    )
    assert wrong.status_code == 403

    accepted = client.post(
        "/api/parse",
        json={"text": "高等数学 周一 第1-2节", "mode": "rule"},
        headers=request_headers(app),
    )
    assert accepted.status_code == 200


def test_testing_mode_only_bypasses_csrf(monkeypatch, tmp_path):
    monkeypatch.setenv(config_module.ENV_DATA_DIR, str(tmp_path))
    db.init_db()
    app = webserver.create_app(
        config=config_module.validate_config(config_module.DEFAULT_CONFIG), database=db
    )
    app.config.update(TESTING=True)
    client = app.test_client()

    without_token = client.post(
        "/api/parse",
        json={"text": "高等数学 周一 第1-2节", "mode": "rule"},
        headers={"Host": "localhost:8080"},
    )
    assert without_token.status_code == 200
    # Host validation remains active while TESTING is true.
    assert client.get("/", headers={"Host": "example.com:8080"}).status_code == 400


@pytest.mark.parametrize(
    "host",
    [
        "example.com:8080",
        "localhost:9090",
        "127.0.0.2:8080",
        "localhost.example:8080",
    ],
)
def test_host_header_rejects_non_local_or_wrong_port(secure_app, host):
    _, client = secure_app
    response = client.get("/api/today", headers={"Host": host})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "INVALID_HOST"


@pytest.mark.parametrize(
    "host", ["localhost:8080", "127.0.0.1:8080", "[::1]:8080"]
)
def test_host_header_accepts_only_supported_loopback_names(secure_app, host):
    _, client = secure_app
    assert client.get("/api/today", headers={"Host": host}).status_code == 200


def test_origin_and_referer_must_be_same_origin_when_present(secure_app):
    app, client = secure_app
    payload = {"text": "高等数学 周一 第1-2节", "mode": "rule"}

    same_origin = client.post(
        "/api/parse",
        json=payload,
        headers=request_headers(
            app,
            Origin="http://localhost:8080",
            Referer="http://localhost:8080/import",
        ),
    )
    assert same_origin.status_code == 200

    bad_origin = client.post(
        "/api/parse",
        json=payload,
        headers=request_headers(app, Origin="http://evil.example"),
    )
    assert bad_origin.status_code == 403
    assert bad_origin.get_json()["error"]["code"] == "ORIGIN_FORBIDDEN"

    bad_referer = client.post(
        "/api/parse",
        json=payload,
        headers=request_headers(app, Referer="http://evil.example/import"),
    )
    assert bad_referer.status_code == 403


def test_security_headers_and_no_permissive_cors(secure_app):
    _, client = secure_app
    response = client.get("/", headers={"Host": "localhost:8080"})
    assert response.status_code == 200
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "'unsafe-inline'" not in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in response.headers


def test_request_parse_text_and_batch_limits(secure_app):
    app, client = secure_app
    assert app.config["MAX_CONTENT_LENGTH"] == 2 * 1024 * 1024

    oversized_body = client.post(
        "/api/import/json",
        data=b"x" * (2 * 1024 * 1024 + 1),
        content_type="application/json",
        headers=request_headers(app),
    )
    assert oversized_body.status_code == 413
    assert oversized_body.get_json()["error"]["code"] == "REQUEST_TOO_LARGE"

    long_text = client.post(
        "/api/parse",
        json={"text": "x" * (webserver.MAX_PARSE_TEXT_LENGTH + 1), "mode": "rule"},
        headers=request_headers(app),
    )
    assert long_text.status_code == 400

    too_many_courses = client.post(
        "/api/import/confirm",
        json={"courses": [{}] * (webserver.MAX_BATCH_ITEMS + 1)},
        headers=request_headers(app),
    )
    assert too_many_courses.status_code == 400

    too_many_combined = client.post(
        "/api/import/json",
        json={
            "courses": [{}] * 501,
            "homeworks": [{}] * 500,
            "settings": {},
        },
        headers=request_headers(app),
    )
    assert too_many_combined.status_code == 400


def test_date_only_deadline_avoids_javascript_date_parsing():
    source = (Path(__file__).parents[1] / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert r"/^(\d{4})-(\d{2})-(\d{2})$/" in source
    assert "new Date(raw)" not in source
    assert "new Date(text)" in source


def test_internal_error_returns_incident_id_without_exception_details(
    secure_app, monkeypatch
):
    app, client = secure_app
    secret = "sensitive-internal-exception-text"

    def fail(_text):
        raise RuntimeError(secret)

    monkeypatch.setattr(webserver, "parse_with_rules", fail)
    response = client.post(
        "/api/parse",
        json={"text": "trigger", "mode": "rule"},
        headers=request_headers(app),
    )
    assert response.status_code == 500
    body = response.get_data(as_text=True)
    assert secret not in body
    payload = response.get_json()
    assert payload["error"]["message"] == "服务器内部错误"
    incident_id = payload["error"]["incident_id"]
    assert payload["incident_id"] == incident_id
    assert re.fullmatch(r"[0-9a-f]{32}", incident_id)
