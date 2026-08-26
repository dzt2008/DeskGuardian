"""DeskGuardian 本地 Flask 仪表板与 REST API。"""

import os
import secrets
import threading
import uuid
import webbrowser
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request
from flask.testing import FlaskClient

import db as default_database
from ai_parser import ParseError, parse_timetable, parse_with_ai
from config import load_config, resource_path, save_config, validate_config
from notifier import DesktopNotifier
from rule_parser import parse_with_rules
from utils import parse_bool


MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_PARSE_TEXT_LENGTH = 100_000
MAX_BATCH_ITEMS = 1_000
_SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))
_ALLOWED_HOSTS = frozenset(("localhost", "127.0.0.1", "::1"))
_PROCESS_CSRF_TOKEN = secrets.token_urlsafe(32)
_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'"
)


class _ConfiguredPortTestClient(FlaskClient):
    """Give Flask's test client the same local origin as the real server."""

    def open(self, *args: Any, **kwargs: Any):
        raw_headers = kwargs.get("headers") or {}
        header_items = (
            raw_headers.items() if hasattr(raw_headers, "items") else raw_headers
        )
        has_host_header = any(
            str(key).lower() == "host" for key, _value in header_items
        )
        if (
            "base_url" not in kwargs
            and not has_host_header
            and (not args or isinstance(args[0], (str, bytes)))
        ):
            port = self.application.extensions["deskguardian_allowed_port"]
            kwargs["base_url"] = "http://localhost:%d" % port
        return super().open(*args, **kwargs)


def _success(data: Any = None, status: int = 200):
    return jsonify({"ok": True, "data": {} if data is None else data}), status


def _error(
    message: str,
    status: int = 400,
    code: str = "VALIDATION_ERROR",
    incident_id: Optional[str] = None,
):
    details = {"code": code, "message": str(message)}
    payload = {"ok": False, "error": details}
    if incident_id is not None:
        details["incident_id"] = incident_id
        payload["incident_id"] = incident_id
    return jsonify(payload), status


def _host_parts(host_header: str):
    """Return a normalized Host header or ``None`` for malformed input."""
    if not host_header or any(char.isspace() for char in host_header):
        return None
    try:
        parsed = urlsplit("http://" + host_header)
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    return hostname, port


def _url_origin(value: str, allow_path: bool):
    """Normalize an HTTP(S) URL to its web-origin tuple."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in ("http", "https")
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or (not allow_path and parsed.path not in ("", "/"))
            or (not allow_path and (parsed.query or parsed.fragment))
        ):
            return None
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _validate_batch(payload: Mapping[str, Any]) -> None:
    """Reject oversized course/homework imports before database validation."""
    counts = []
    for key in ("courses", "homeworks"):
        value = payload.get(key, [])
        if isinstance(value, list):
            counts.append(len(value))
            if len(value) > MAX_BATCH_ITEMS:
                raise ValueError("%s 每次最多导入 %d 条" % (key, MAX_BATCH_ITEMS))
    if sum(counts) > MAX_BATCH_ITEMS:
        raise ValueError("每次最多批量导入 %d 条记录" % MAX_BATCH_ITEMS)


def _json_body() -> Dict[str, Any]:
    if not request.is_json:
        raise ValueError("请求必须使用 application/json")
    payload = request.get_json(silent=False)
    if not isinstance(payload, dict):
        raise ValueError("JSON 顶层必须是对象")
    return payload


def _database(app: Flask):
    return app.extensions["deskguardian_database"]


def _call(database: Any, name: str, *args: Any, **kwargs: Any):
    function = getattr(database, name, None)
    if not callable(function):
        raise RuntimeError("数据库模块缺少接口：%s" % name)
    return function(*args, **kwargs)


def _current_settings(app: Flask) -> Dict[str, Any]:
    # 每次读取，确保 CLI 修改后 Web 无需重启即可看到最新配置。
    try:
        return load_config()
    except ValueError:
        return dict(app.extensions["deskguardian_config"])


def create_app(
    config: Optional[Mapping[str, Any]] = None,
    database: Any = None,
    db: Any = None,
    access_token: Optional[str] = None,
) -> Flask:
    """创建无启动副作用的 Flask 应用，便于 CLI 和测试注入依赖。"""
    application = Flask(
        __name__,
        template_folder=resource_path("templates"),
        static_folder=resource_path("static"),
    )
    settings = validate_config(config or load_config())
    database_facade = database if database is not None else db
    if database_facade is None:
        database_facade = default_database
    initializer = getattr(database_facade, "init_db", None)
    if callable(initializer):
        initializer()
    application.extensions["deskguardian_database"] = database_facade
    application.extensions["deskguardian_config"] = settings
    application.extensions["deskguardian_csrf_token"] = _PROCESS_CSRF_TOKEN
    # The short alias keeps the token discoverable for integrations and tests.
    application.extensions["csrf_token"] = _PROCESS_CSRF_TOKEN
    application.extensions["deskguardian_allowed_port"] = int(settings["port"])
    configured_host = str(settings.get("host", "127.0.0.1"))
    is_loopback = configured_host in ("127.0.0.1", "localhost", "::1")
    remote_token = access_token or os.environ.get("DESKGUARDIAN_ACCESS_TOKEN")
    if not is_loopback and (not isinstance(remote_token, str) or len(remote_token) < 16):
        raise RuntimeError(
            "非回环 Web 服务必须设置至少 16 个字符的 DESKGUARDIAN_ACCESS_TOKEN"
        )
    application.extensions["deskguardian_remote_token"] = remote_token
    application.extensions["deskguardian_remote_mode"] = not is_loopback
    application.test_client_class = _ConfiguredPortTestClient
    application.config.update(
        JSON_AS_ASCII=False,
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
    )

    @application.context_processor
    def security_template_context():
        return {"csrf_token": application.extensions["deskguardian_csrf_token"]}

    @application.before_request
    def enforce_local_request_security():
        host = _host_parts(request.headers.get("Host", ""))
        expected_port = application.extensions["deskguardian_allowed_port"]
        supplied_port = None if host is None else host[1]
        if supplied_port is None:
            supplied_port = 443 if request.is_secure else 80
        remote_mode = bool(application.extensions.get("deskguardian_remote_mode"))
        if (
            not remote_mode
            and (
                host is None
                or host[0] not in _ALLOWED_HOSTS
                or supplied_port != expected_port
            )
        ):
            return _error("Host 不受信任", 400, "INVALID_HOST")
        if remote_mode and request.path.startswith("/api/"):
            supplied_token = request.headers.get("Authorization", "")
            expected_token = application.extensions["deskguardian_remote_token"]
            expected_header = "Bearer " + str(expected_token)
            if not secrets.compare_digest(supplied_token, expected_header):
                return _error("需要访问令牌", 401, "AUTH_REQUIRED")

        request_origin = _url_origin(request.host_url, allow_path=False)
        origin = request.headers.get("Origin")
        origin_value = None if origin is None else _url_origin(origin, False)
        if origin is not None and origin_value != request_origin:
            return _error("请求来源不同源", 403, "ORIGIN_FORBIDDEN")
        referer = request.headers.get("Referer")
        referer_value = None if referer is None else _url_origin(referer, True)
        if referer is not None and referer_value != request_origin:
            return _error("请求来源不同源", 403, "ORIGIN_FORBIDDEN")

        if (
            request.path.startswith("/api/")
            and request.method not in _SAFE_METHODS
            and not application.testing
        ):
            supplied = request.headers.get("X-DeskGuardian-CSRF", "")
            expected = application.extensions["deskguardian_csrf_token"]
            if not supplied or not secrets.compare_digest(supplied, expected):
                return _error("CSRF 校验失败", 403, "CSRF_FAILED")
        return None

    @application.after_request
    def apply_security_headers(response):
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/")
    def page_today():
        return render_template("index.html")

    @application.get("/import")
    def page_import():
        return render_template("import.html")

    @application.get("/manage")
    def page_manage():
        return render_template("manage.html")

    @application.errorhandler(400)
    def bad_request(error):
        description = getattr(error, "description", "请求格式错误")
        return _error(description, 400)

    @application.errorhandler(404)
    def not_found(_error_object):
        if request.path.startswith("/api/"):
            return _error("资源不存在", 404, "NOT_FOUND")
        return "页面不存在", 404

    @application.errorhandler(405)
    def method_not_allowed(_error_object):
        if request.path.startswith("/api/"):
            return _error("请求方法不允许", 405, "METHOD_NOT_ALLOWED")
        return "请求方法不允许", 405

    @application.errorhandler(413)
    def request_too_large(_error_object):
        if request.path.startswith("/api/"):
            return _error("请求体不能超过 2MB", 413, "REQUEST_TOO_LARGE")
        return "请求体不能超过 2MB", 413

    @application.errorhandler(ValueError)
    def validation_error(error):
        return _error(str(error), 400)

    @application.errorhandler(ParseError)
    def ai_service_error(error):
        return _error(str(error), 503, "AI_UNAVAILABLE")

    @application.errorhandler(Exception)
    def unexpected_error(_error_object):
        incident_id = uuid.uuid4().hex
        application.logger.exception("请求处理失败，incident_id=%s", incident_id)
        return _error(
            "服务器内部错误", 500, "INTERNAL_ERROR", incident_id=incident_id
        )

    @application.post("/api/parse")
    def api_parse():
        payload = _json_body()
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text 不能为空")
        if len(text) > MAX_PARSE_TEXT_LENGTH:
            raise ValueError("text 不能超过 %d 个字符" % MAX_PARSE_TEXT_LENGTH)
        mode = str(payload.get("mode", "auto")).lower()
        if mode not in ("auto", "ai", "rule"):
            raise ValueError("mode 必须是 auto、ai 或 rule")
        current = _current_settings(application)
        if mode == "rule":
            result = parse_with_rules(text)
        elif mode == "ai":
            result = parse_with_ai(
                text,
                model=current["ollama_model"],
                ollama_url=current["ollama_url"],
                timeout=current["ollama_timeout"],
            )
        else:
            result = parse_timetable(
                text,
                model=current["ollama_model"],
                ollama_url=current["ollama_url"],
                timeout=current["ollama_timeout"],
            )
        return _success(result)

    @application.get("/api/courses")
    def api_courses_list():
        weekday = request.args.get("weekday") or None
        search = request.args.get("q") or request.args.get("search") or None
        active = request.args.get("active")
        active_only = parse_bool(active, "active") if active is not None else False
        rows = _call(
            _database(application),
            "list_courses",
            weekday=weekday,
            search=search,
            active_only=active_only,
        )
        return _success(rows)

    @application.post("/api/courses")
    def api_courses_create():
        row = _call(_database(application), "add_course", _json_body())
        return _success(row, 201)

    @application.put("/api/courses/<int:course_id>")
    def api_courses_update(course_id: int):
        row = _call(_database(application), "update_course", course_id, _json_body())
        if row is None:
            return _error("课程不存在", 404, "NOT_FOUND")
        return _success(row)

    @application.delete("/api/courses/<int:course_id>")
    def api_courses_delete(course_id: int):
        if not _call(_database(application), "delete_course", course_id):
            return _error("课程不存在", 404, "NOT_FOUND")
        return _success({"deleted": course_id})

    @application.get("/api/homeworks")
    def api_homeworks_list():
        rows = _call(_database(application), "list_homeworks")
        return _success(rows)

    @application.post("/api/homeworks")
    def api_homeworks_create():
        row = _call(_database(application), "add_homework", _json_body())
        return _success(row, 201)

    @application.put("/api/homeworks/<int:homework_id>")
    def api_homeworks_update(homework_id: int):
        row = _call(
            _database(application), "update_homework", homework_id, _json_body()
        )
        if row is None:
            return _error("作业不存在", 404, "NOT_FOUND")
        return _success(row)

    @application.delete("/api/homeworks/<int:homework_id>")
    def api_homeworks_delete(homework_id: int):
        if not _call(_database(application), "delete_homework", homework_id):
            return _error("作业不存在", 404, "NOT_FOUND")
        return _success({"deleted": homework_id})

    @application.post("/api/import/confirm")
    def api_import_confirm():
        payload = _json_body()
        courses = payload.get("courses")
        if not isinstance(courses, list) or not courses:
            raise ValueError("courses 必须是非空数组")
        if len(courses) > MAX_BATCH_ITEMS:
            raise ValueError("courses 每次最多导入 %d 条" % MAX_BATCH_ITEMS)
        selected = []
        for item in courses:
            if not isinstance(item, dict):
                raise ValueError("每条课程必须是对象")
            if item.get("selected", True):
                clean = dict(item)
                clean.pop("selected", None)
                selected.append(clean)
        if not selected:
            raise ValueError("没有选中的课程")

        if payload.get("deduplicate", False):
            existing = _call(_database(application), "list_courses")
            signatures = {
                (
                    row["name"].casefold(),
                    row["weekday"],
                    row["start_section"],
                    row["end_section"],
                    row.get("room", "").casefold(),
                )
                for row in existing
            }
            filtered = []
            for item in selected:
                signature = (
                    str(item.get("name", "")).strip().casefold(),
                    item.get("weekday"),
                    item.get("start_section"),
                    item.get("end_section"),
                    str(item.get("room", "")).strip().casefold(),
                )
                if signature not in signatures:
                    filtered.append(item)
                    signatures.add(signature)
            skipped = len(selected) - len(filtered)
            selected = filtered
        else:
            skipped = 0
        rows = (
            _call(_database(application), "bulk_add_courses", selected)
            if selected
            else []
        )
        return _success(
            {
                "inserted": len(rows),
                "imported": len(rows),
                "count": len(rows),
                "skipped": skipped,
                "courses": rows,
            },
            201,
        )

    @application.get("/api/today")
    def api_today():
        current = datetime.now().astimezone()
        settings_now = _current_settings(application)
        all_courses = _call(
            _database(application),
            "list_courses",
            weekday=current.isoweekday(),
            active_only=True,
        )
        courses = []
        for row in all_courses:
            item = dict(row)
            start_cfg = settings_now["section_times"].get(
                str(item["start_section"]), {}
            )
            end_cfg = settings_now["section_times"].get(str(item["end_section"]), {})
            item["start_time"] = start_cfg.get("start")
            item["end_time"] = end_cfg.get("end")
            item["time"] = "%s–%s" % (
                item["start_time"] or "?",
                item["end_time"] or "?",
            )
            courses.append(item)
        end_date = current.date() + timedelta(days=3)
        homeworks = _call(
            _database(application),
            "list_homeworks",
            due_after=current.date().isoformat(),
            due_before=end_date.isoformat(),
        )
        return _success(
            {
                "date": current.date().isoformat(),
                "date_label": current.strftime("%Y年%m月%d日"),
                "courses": courses,
                "homeworks": homeworks,
            }
        )

    @application.get("/api/settings")
    def api_settings_get():
        settings_now = _current_settings(application)
        if not application.testing:
            try:
                from autostart import is_autostart_enabled

                settings_now["autostart"] = is_autostart_enabled()
            except OSError:
                application.logger.warning("无法读取系统开机自启状态")
        return _success(settings_now)

    @application.put("/api/settings")
    def api_settings_put():
        updates = _json_body()
        allowed = {
            "reminder_minutes",
            "daily_summary_time",
            "scan_interval_seconds",
            "autostart",
            "beep",
            "section_times",
            "ollama_url",
            "ollama_model",
            "ollama_timeout",
            "host",
            "port",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError("未知配置项：%s" % ", ".join(sorted(unknown)))
        current = _current_settings(application)
        current.update(updates)
        current = validate_config(current)
        requested_host = current.get("host", "127.0.0.1")
        if requested_host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                "Web 默认只允许回环地址；远程模式请通过 CLI 的 --allow-remote 配置访问令牌"
            )
        if "autostart" in updates and not application.testing:
            from autostart import configure_autostart

            current["autostart"] = configure_autostart(
                parse_bool(updates["autostart"], "autostart")
            )
        saved = save_config(current)
        application.extensions["deskguardian_config"] = saved
        return _success(saved)

    @application.post("/api/notifications/test")
    def api_notification_test():
        settings_now = _current_settings(application)
        try:
            notifier = DesktopNotifier(beep=bool(settings_now.get("beep", False)))
        except TypeError:
            # Keep the endpoint easy to replace with a zero-argument fake in tests
            # and with lightweight third-party notification adapters.
            notifier = DesktopNotifier()
        notifier.notify("DeskGuardian 测试提醒", "通知功能工作正常。")
        return _success({"sent": True})

    @application.get("/api/export")
    def api_export():
        payload = _call(_database(application), "export_data")
        payload = dict(payload)
        payload["config"] = _current_settings(application)
        return _success(payload)

    @application.post("/api/import/json")
    def api_import_json():
        incoming = _json_body()
        mode = str(incoming.get("mode", "merge"))
        payload = incoming.get("payload", incoming.get("data", incoming))
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是对象")
        if "mode" in payload and payload is incoming:
            payload = dict(payload)
            payload.pop("mode", None)
        _validate_batch(payload)
        result = _call(_database(application), "import_data", payload, mode=mode)
        imported_config = payload.get("config")
        if incoming.get("apply_config") is True and isinstance(imported_config, dict):
            saved = save_config(imported_config)
            application.extensions["deskguardian_config"] = saved
            result = dict(result)
            result["config"] = 1
        return _success(result, 201)

    return application


make_app = create_app


def run_web(
    host: Optional[str] = None,
    port: Optional[int] = None,
    config: Optional[Mapping[str, Any]] = None,
    open_browser: bool = True,
) -> None:
    settings = validate_config(config or load_config())
    bind_host = host or settings["host"]
    bind_port = int(port or settings["port"])
    runtime_settings = dict(settings)
    runtime_settings["host"] = bind_host
    runtime_settings["port"] = bind_port
    app = create_app(runtime_settings)
    if open_browser:
        browser_host = "127.0.0.1" if bind_host in ("0.0.0.0", "::") else bind_host
        timer = threading.Timer(
            0.8, lambda: webbrowser.open("http://%s:%d" % (browser_host, bind_port))
        )
        timer.daemon = True
        timer.start()
    app.run(
        host=bind_host, port=bind_port, debug=False, use_reloader=False, threaded=True
    )


if __name__ == "__main__":
    run_web()
