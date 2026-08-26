import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

import config
import db
import diagnostics
import logging_setup
import version


def _close_deskguardian_handlers():
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_deskguardian_rotating_file_handler", False):
            root.removeHandler(handler)
            handler.close()


def test_version_has_single_public_value():
    assert version.VERSION == "0.1.0"
    assert version.__version__ == version.VERSION


def test_logging_uses_utf8_rotation_debug_and_redacts_business_content(tmp_path):
    try:
        root = logging_setup.setup_logging(
            debug=True, data_dir=tmp_path, max_bytes=4096, backup_count=2
        )
        handlers = [
            item
            for item in root.handlers
            if getattr(item, "_deskguardian_rotating_file_handler", False)
        ]
        assert len(handlers) == 1
        handler = handlers[0]
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 4096
        assert handler.backupCount == 2
        assert handler.encoding.lower().replace("-", "") == "utf8"
        assert handler.level == logging.DEBUG

        logger = logging.getLogger("deskguardian.test")
        logger.debug("调试模式已启用")
        logger.info(
            {
                "event": "homework_saved",
                "subject": "高度私密的作业正文",
                "unknown_field": "另一段业务正文",
            }
        )
        logger.warning("record %s failed: %s", 42, "高度私密的错误正文")
        logger.warning(
            "another private timetable body",
            extra={"business_content": True},
        )
        handler.flush()

        path = tmp_path / "logs" / "deskguardian.log"
        content = path.read_text(encoding="utf-8")
        assert "调试模式已启用" in content
        assert "homework_saved" in content
        assert "高度私密的作业正文" not in content
        assert "另一段业务正文" not in content
        assert "高度私密的错误正文" not in content
        assert "record 42 failed: [REDACTED]" in content
        assert "another private timetable body" not in content
        assert "[REDACTED]" in content
    finally:
        _close_deskguardian_handlers()


def test_setup_logging_is_idempotent_and_info_is_default(tmp_path):
    try:
        logging_setup.setup_logging(data_dir=tmp_path)
        root = logging_setup.setup_logging(data_dir=tmp_path)
        handlers = [
            item
            for item in root.handlers
            if getattr(item, "_deskguardian_rotating_file_handler", False)
        ]
        assert len(handlers) == 1
        assert handlers[0].level == logging.INFO
        assert Path(logging_setup.get_log_path(tmp_path)) == (
            tmp_path.resolve() / "logs" / "deskguardian.log"
        )
    finally:
        _close_deskguardian_handlers()


def test_collect_diagnostics_is_offline_json_serializable(monkeypatch):
    config.load_config()
    db.init_db()

    def unexpected_network(*args, **kwargs):
        raise AssertionError("default diagnostics must not access the network")

    monkeypatch.setattr(diagnostics.urllib.request, "urlopen", unexpected_network)
    result = diagnostics.collect_diagnostics()

    assert result["app"] == {"name": "DeskGuardian", "version": "0.1.0"}
    assert result["python"]["version"]
    assert result["platform"]["system"]
    assert isinstance(result["frozen"], bool)
    assert result["data_dir"]["writable"] is True
    assert result["config"]["valid"] is True
    assert result["db"]["integrity"] == "ok"
    assert result["resources"]["all_present"] is True
    assert isinstance(result["autostart"]["enabled"], bool)
    assert result["log"]["path"].endswith("logs\\deskguardian.log") or result[
        "log"
    ]["path"].endswith("logs/deskguardian.log")
    assert result["ollama"] == {"checked": False}
    json.dumps(result, ensure_ascii=False)


def test_collect_diagnostics_accepts_injected_modules_and_reports_failures(tmp_path):
    data_dir = tmp_path / "missing-data"
    config_path = data_dir / "config.json"
    db_path = data_dir / "deskguardian.db"

    fake_config = SimpleNamespace(
        get_data_dir=lambda create=False: str(data_dir),
        get_config_path=lambda: str(config_path),
        get_db_path=lambda: str(db_path),
        resource_path=lambda relative: str(tmp_path / "missing" / relative),
        load_config=lambda *args, **kwargs: {},
    )
    fake_db = SimpleNamespace(connect=lambda path: None)

    result = diagnostics.collect_diagnostics(
        config_module=fake_config, db_module=fake_db
    )

    assert result["data_dir"]["exists"] is False
    assert result["data_dir"]["writable"] is True
    assert result["config"]["exists"] is False
    assert result["config"]["valid"] is True
    assert result["db"]["integrity"] == "missing"
    assert result["resources"]["all_present"] is False
    json.dumps(result)


def test_ollama_is_checked_only_when_explicitly_requested(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = data_dir / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    requested = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def getcode(self):
            return 200

    def fake_urlopen(request, timeout):
        requested["url"] = request.full_url
        requested["method"] = request.get_method()
        requested["timeout"] = timeout
        return Response()

    fake_config = SimpleNamespace(
        get_data_dir=lambda create=False: str(data_dir),
        get_config_path=lambda: str(config_path),
        get_db_path=lambda: str(data_dir / "absent.db"),
        resource_path=config.resource_path,
        load_config=lambda *args, **kwargs: {
            "ollama_url": "http://127.0.0.1:11434"
        },
    )
    monkeypatch.setattr(diagnostics.urllib.request, "urlopen", fake_urlopen)

    result = diagnostics.collect_diagnostics(
        config_module=fake_config,
        db_module=db,
        check_ollama=True,
        ollama_timeout=0.25,
    )

    assert requested == {
        "url": "http://127.0.0.1:11434",
        "method": "GET",
        "timeout": 0.25,
    }
    assert result["ollama"] == {
        "checked": True,
        "reachable": True,
        "status_code": 200,
        "error": None,
    }
