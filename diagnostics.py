"""Offline, JSON-serializable runtime diagnostics for DeskGuardian."""

import json
import os
import platform as platform_module
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Mapping, Optional, Tuple

try:
    import config as default_config_module
    import db as default_db_module
    from logging_setup import get_log_path
    from version import __version__
except ImportError:  # pragma: no cover - package-style import
    from . import config as default_config_module
    from . import db as default_db_module
    from .logging_setup import get_log_path
    from .version import __version__


APP_NAME = "DeskGuardian"
_RESOURCE_NAMES = (
    "templates/base.html",
    "templates/index.html",
    "templates/import.html",
    "templates/manage.html",
    "static/style.css",
    "static/app.js",
)


def _exception_name(error: BaseException) -> str:
    """Return useful failure metadata without copying user content into output."""
    return type(error).__name__


def _call_path(module: ModuleType, name: str, fallback: Path) -> Path:
    function = getattr(module, name, None)
    if callable(function):
        value = str(function())
        if value == ":memory:":
            return Path(value)
        return Path(value).expanduser().resolve()
    return fallback.expanduser().resolve()


def _data_directory(config_module: ModuleType) -> Path:
    getter = getattr(config_module, "get_data_dir", None)
    if not callable(getter):
        return Path.cwd().resolve()
    try:
        value = getter(create=False)
    except TypeError:  # Lightweight injected modules need not accept ``create``.
        value = getter()
    return Path(value).expanduser().resolve()


def _writable(directory: Path) -> Tuple[bool, Optional[str]]:
    """Probe whether an existing data directory is writable or can be created."""
    probe_directory = directory
    if directory.exists() and not directory.is_dir():
        return False, "not_a_directory"
    while not probe_directory.exists() and probe_directory != probe_directory.parent:
        probe_directory = probe_directory.parent
    if not probe_directory.is_dir():
        return False, "missing_parent"
    try:
        descriptor, probe = tempfile.mkstemp(
            prefix=".deskguardian-diagnostic-", dir=str(probe_directory)
        )
        os.close(descriptor)
        os.unlink(probe)
        return True, None
    except OSError as error:
        return False, _exception_name(error)


def _config_status(
    config_module: ModuleType, path: Path
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    result = {
        "path": str(path),
        "exists": path.is_file(),
        "valid": None,
        "error": None,
    }  # type: Dict[str, Any]
    loaded = {}  # type: Dict[str, Any]
    loader = getattr(config_module, "load_config", None)
    if not callable(loader):
        result["error"] = "load_config unavailable"
        return result, loaded
    try:
        try:
            value = loader(path=str(path), create=False)
        except TypeError:
            try:
                value = loader(str(path), create=False)
            except TypeError:
                value = loader(str(path))
        if not isinstance(value, Mapping):
            raise TypeError("configuration is not a mapping")
        loaded = dict(value)
        result["valid"] = True
    except Exception as error:  # Diagnostics must report, not fail the program.
        result["valid"] = False
        result["error"] = _exception_name(error)
    return result, loaded


def _db_status(db_module: ModuleType, path: Path) -> Dict[str, Any]:
    exists = str(path) == ":memory:" or path.is_file()
    result = {
        "path": str(path),
        "exists": exists,
        "integrity": "not_checked" if not exists else "unknown",
        "error": None,
    }  # type: Dict[str, Any]
    if not exists:
        result["integrity"] = "missing"
        return result

    connector = getattr(db_module, "connect", None)
    if not callable(connector):
        result["error"] = "connect unavailable"
        return result

    connection = None
    try:
        connection = connector(str(path))
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        messages = [str(row[0]) for row in rows]
        result["integrity"] = (
            "ok"
            if messages and all(item.lower() == "ok" for item in messages)
            else "failed"
        )
        if result["integrity"] == "failed":
            # Do not expose database contents returned by SQLite diagnostics.
            result["issues"] = len(messages)
    except (OSError, sqlite3.Error, TypeError, AttributeError) as error:
        result["integrity"] = "error"
        result["error"] = _exception_name(error)
    except Exception as error:  # Supports injected facades with custom failures.
        result["integrity"] = "error"
        result["error"] = _exception_name(error)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    return result


def _resource_status(config_module: ModuleType) -> Dict[str, Any]:
    resolver = getattr(config_module, "resource_path", None)
    result = {}  # type: Dict[str, Any]
    for relative in _RESOURCE_NAMES:
        try:
            if callable(resolver):
                resolved = Path(resolver(relative))
            else:
                resolved = Path(__file__).resolve().parent / relative
            result[relative] = resolved.is_file()
        except (OSError, TypeError, ValueError):
            result[relative] = False
    result["all_present"] = all(result.values())
    return result


def _autostart_status() -> Dict[str, Any]:
    result = {"enabled": None, "error": None}  # type: Dict[str, Any]
    try:
        try:
            import autostart
        except ImportError:  # pragma: no cover - package-style import
            from . import autostart

        result["enabled"] = bool(autostart.is_autostart_enabled())
    except Exception as error:
        result["error"] = _exception_name(error)
    return result


def _ollama_status(settings: Mapping[str, Any], timeout: float) -> Dict[str, Any]:
    """Perform the only optional network operation in this module."""
    result = {
        "checked": True,
        "reachable": False,
        "status_code": None,
        "error": None,
    }  # type: Dict[str, Any]
    url = settings.get("ollama_url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        result["error"] = "invalid_url"
        return result

    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result["status_code"] = int(response.getcode())
            result["reachable"] = True
    except urllib.error.HTTPError as error:
        # An HTTP response proves that the configured service is reachable.
        result["status_code"] = int(error.code)
        result["reachable"] = True
        result["error"] = "HTTPError"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        result["error"] = _exception_name(error)
    return result


def collect_diagnostics(
    config_module: Optional[ModuleType] = None,
    db_module: Optional[ModuleType] = None,
    check_ollama: bool = False,
    ollama_timeout: float = 2.0,
) -> Dict[str, Any]:
    """Collect safe diagnostics without notifications or network by default.

    ``config_module`` and ``db_module`` are injectable module-like facades for
    tests and embedding.  Configuration values and database rows are never
    included.  Only an explicit ``check_ollama=True`` permits one short GET to
    the configured Ollama base URL.
    """
    config_module = config_module or default_config_module
    db_module = db_module or default_db_module
    timeout = float(ollama_timeout)
    if timeout <= 0 or timeout > 10:
        raise ValueError("ollama_timeout must be greater than 0 and at most 10 seconds")

    try:
        data_dir = _data_directory(config_module)
        data_error = None
    except Exception as error:
        data_dir = Path.cwd().resolve()
        data_error = _exception_name(error)
    writable, writable_error = _writable(data_dir)

    try:
        config_path = _call_path(
            config_module, "get_config_path", data_dir / "config.json"
        )
    except Exception:
        config_path = data_dir / "config.json"
    config_status, settings = _config_status(config_module, config_path)

    try:
        db_path = _call_path(
            config_module,
            "get_db_path",
            data_dir / "deskguardian.db",
        )
    except Exception:
        db_path = data_dir / "deskguardian.db"

    system = platform_module.system()
    result = {
        "app": {"name": APP_NAME, "version": __version__},
        "python": {
            "version": platform_module.python_version(),
            "implementation": platform_module.python_implementation(),
        },
        "platform": {
            "system": system,
            "release": platform_module.release(),
            "machine": platform_module.machine(),
        },
        "frozen": bool(getattr(sys, "frozen", False)),
        "data_dir": {
            "path": str(data_dir),
            "exists": data_dir.is_dir(),
            "writable": writable,
            "error": data_error or writable_error,
        },
        "config": config_status,
        "db": _db_status(db_module, db_path),
        "resources": _resource_status(config_module),
        "autostart": _autostart_status(),
        "log": {"path": get_log_path(data_dir)},
        "ollama": {"checked": False},
    }  # type: Dict[str, Any]
    if check_ollama:
        result["ollama"] = _ollama_status(settings, timeout)

    # Enforce the public contract now, not only in callers/tests.
    json.dumps(result, ensure_ascii=False)
    return result


__all__ = ["collect_diagnostics"]
