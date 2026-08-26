"""DeskGuardian configuration and cross-platform path helpers."""

import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union


APP_NAME = "DeskGuardian"
ENV_DATA_DIR = "DESKGUARDIAN_DATA_DIR"
CURRENT_CONFIG_VERSION = 1

# All eight periods are deliberately data, not constants in the scheduler: users
# may change them in config.json or through the management page.
DEFAULT_SECTION_TIMES = {
    "1": {"start": "08:00", "end": "08:45"},
    "2": {"start": "08:55", "end": "09:40"},
    "3": {"start": "10:00", "end": "10:45"},
    "4": {"start": "10:55", "end": "11:40"},
    "5": {"start": "14:00", "end": "14:45"},
    "6": {"start": "14:55", "end": "15:40"},
    "7": {"start": "16:00", "end": "16:45"},
    "8": {"start": "16:55", "end": "17:40"},
}

DEFAULT_CONFIG = {
    "config_version": CURRENT_CONFIG_VERSION,
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen2.5:0.5b",
    "ollama_timeout": 30,
    "host": "127.0.0.1",
    "port": 8080,
    "reminder_minutes": 5,
    "daily_summary_time": "07:00",
    "scan_interval_seconds": 60,
    "autostart": False,
    "beep": False,
    "section_times": DEFAULT_SECTION_TIMES,
}

PathLike = Union[str, os.PathLike]


def _harden_directory(path: Path) -> None:
    """Best-effort owner-only permissions on POSIX; a no-op on Windows."""
    if os.name == "nt":
        return
    try:
        path.chmod(0o700)
    except OSError:
        # Some filesystems (for example FAT mounts) do not support POSIX modes.
        pass


def _harden_file(path: Path) -> None:
    """Best-effort owner read/write permissions on POSIX."""
    if os.name == "nt":
        return
    try:
        path.chmod(0o600)
    except OSError:
        pass


def resource_path(relative_path: PathLike = ".") -> str:
    """Return an absolute bundled-resource path (source and PyInstaller safe)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str((base / Path(relative_path)).resolve())


def get_data_dir(create: bool = True) -> str:
    """Return DeskGuardian's writable per-user data directory.

    ``DESKGUARDIAN_DATA_DIR`` takes precedence, which also makes tests and
    portable installations deterministic.
    """
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        path = Path(os.path.expandvars(override)).expanduser()
    elif sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        path = Path(root).expanduser() / APP_NAME if root else Path.home() / APP_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        root = os.environ.get("XDG_DATA_HOME")
        path = (
            Path(root).expanduser() if root else Path.home() / ".local" / "share"
        ) / APP_NAME.lower()
    path = path.resolve()
    if create:
        path.mkdir(parents=True, exist_ok=True)
        _harden_directory(path)
    return str(path)


def get_config_path() -> str:
    return str(Path(get_data_dir()) / "config.json")


def get_db_path() -> str:
    return str(Path(get_data_dir()) / "deskguardian.db")


def _deep_merge(
    defaults: Mapping[str, Any], supplied: Mapping[str, Any]
) -> Dict[str, Any]:
    result = copy.deepcopy(dict(defaults))
    for key, value in supplied.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _config_version(config: Mapping[str, Any], missing: int) -> int:
    """Return a strictly validated configuration format version."""
    value = config.get("config_version", missing)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("config_version 必须是非负整数")
    if value > CURRENT_CONFIG_VERSION:
        raise ValueError(
            "配置文件版本 %d 高于当前支持的版本 %d，请升级 DeskGuardian"
            % (value, CURRENT_CONFIG_VERSION)
        )
    return value


def _migrate_config_0_to_1(config: Mapping[str, Any]) -> Dict[str, Any]:
    migrated = copy.deepcopy(dict(config))
    migrated["config_version"] = 1
    return migrated


_CONFIG_MIGRATIONS = {0: _migrate_config_0_to_1}


def _migrate_config(config: Mapping[str, Any], version: int) -> Dict[str, Any]:
    migrated = copy.deepcopy(dict(config))
    current = version
    while current < CURRENT_CONFIG_VERSION:
        migration = _CONFIG_MIGRATIONS.get(current)
        if migration is None:
            raise ValueError("缺少从配置版本 %d 开始的迁移步骤" % current)
        migrated = migration(migrated)
        next_version = _config_version(migrated, missing=current)
        if next_version <= current:
            raise ValueError("配置迁移未推进版本号")
        current = next_version
    return migrated


def _config_backup_path(target: Path, version: int) -> Path:
    return target.with_name("%s.v%d.bak" % (target.name, version))


def _atomic_backup_bytes(target: Path, content: bytes, version: int) -> Path:
    """Create one complete pre-migration backup without exposing partial data."""
    backup = _config_backup_path(target, version)
    if backup.exists():
        return backup
    fd, temporary = tempfile.mkstemp(
        prefix=backup.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # The deterministic name prevents repeated backups after an interrupted
        # migration; replace keeps readers from ever observing a partial file.
        if not backup.exists():
            os.replace(temporary, str(backup))
            _harden_file(backup)
        else:
            os.unlink(temporary)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return backup


def _is_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        hour, minute = value.split(":")
        return (
            len(hour) == 2
            and len(minute) == 2
            and 0 <= int(hour) <= 23
            and 0 <= int(minute) <= 59
        )
    except (ValueError, AttributeError):
        return False


def validate_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a complete or partial configuration mapping."""
    if not isinstance(config, Mapping):
        raise ValueError("配置必须是 JSON 对象")
    merged = _deep_merge(DEFAULT_CONFIG, config)
    version = _config_version(merged, missing=CURRENT_CONFIG_VERSION)
    if version != CURRENT_CONFIG_VERSION:
        raise ValueError(
            "配置版本 %d 尚未迁移到版本 %d"
            % (version, CURRENT_CONFIG_VERSION)
        )
    merged["config_version"] = CURRENT_CONFIG_VERSION

    for key in ("ollama_url", "ollama_model", "host"):
        if not isinstance(merged.get(key), str) or not merged[key].strip():
            raise ValueError("%s 不能为空" % key)
        merged[key] = merged[key].strip()
    if not (
        merged["ollama_url"].startswith("http://")
        or merged["ollama_url"].startswith("https://")
    ):
        raise ValueError("ollama_url 必须以 http:// 或 https:// 开头")

    integer_ranges = {
        "port": (1, 65535),
        "ollama_timeout": (1, 600),
        "reminder_minutes": (0, 1440),
        "scan_interval_seconds": (1, 3600),
    }
    for key, bounds in integer_ranges.items():
        value = merged.get(key)
        if isinstance(value, bool) or (
            isinstance(value, float) and not value.is_integer()
        ):
            raise ValueError("%s 必须是整数" % key)
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError("%s 必须是整数" % key)
        if not bounds[0] <= value <= bounds[1]:
            raise ValueError("%s 必须在 %s 到 %s 之间" % (key, bounds[0], bounds[1]))
        merged[key] = value

    for key in ("autostart", "beep"):
        if not isinstance(merged.get(key), bool):
            raise ValueError("%s 必须是布尔值" % key)
    if not _is_time(merged.get("daily_summary_time")):
        raise ValueError("daily_summary_time 必须为 HH:MM")

    section_times = merged.get("section_times")
    if not isinstance(section_times, Mapping):
        raise ValueError("section_times 必须是对象")
    normalized_times = {}
    for section in range(1, 9):
        item = section_times.get(str(section), section_times.get(section))
        if not isinstance(item, Mapping):
            raise ValueError("section_times 缺少第 %d 节" % section)
        start, end = item.get("start"), item.get("end")
        if not _is_time(start) or not _is_time(end):
            raise ValueError("第 %d 节时间必须为 HH:MM" % section)
        if start >= end:
            raise ValueError("第 %d 节结束时间必须晚于开始时间" % section)
        normalized_times[str(section)] = {"start": start, "end": end}
    merged["section_times"] = normalized_times
    return merged


def save_config(
    config: Mapping[str, Any], path: Optional[PathLike] = None
) -> Dict[str, Any]:
    """Validate and atomically persist configuration; return normalized data."""
    normalized = validate_config(config)
    target = (
        Path(path).expanduser().resolve()
        if path is not None
        else Path(get_config_path())
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    _harden_directory(target.parent)
    _harden_file(target)
    fd, temporary = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(normalized, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(target))
        _harden_file(target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return copy.deepcopy(normalized)


def load_config(
    path: Optional[PathLike] = None, create: bool = True
) -> Dict[str, Any]:
    """Load, migrate and validate config; generate it if it is absent.

    Existing version-0 files are backed up immediately before their migrated
    representation is atomically persisted. Invalid JSON and configurations
    written by a newer DeskGuardian release are never modified.
    """
    target = (
        Path(path).expanduser().resolve()
        if path is not None
        else Path(get_config_path())
    )
    if not target.exists():
        normalized = validate_config(DEFAULT_CONFIG)
        if create:
            return save_config(normalized, target)
        return normalized

    try:
        raw = target.read_bytes()
        supplied = json.loads(raw.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("配置文件不是有效 JSON: %s" % exc) from exc
    if not isinstance(supplied, Mapping):
        raise ValueError("配置必须是 JSON 对象")

    source_version = _config_version(supplied, missing=0)
    _harden_directory(target.parent)
    _harden_file(target)
    if create and source_version < CURRENT_CONFIG_VERSION:
        _atomic_backup_bytes(target, raw, source_version)
    migrated = _migrate_config(supplied, source_version)
    normalized = validate_config(migrated)
    if create and normalized != supplied:
        save_config(normalized, target)
    return normalized


def update_config(
    updates: Mapping[str, Any], path: Optional[PathLike] = None
) -> Dict[str, Any]:
    """Deep-merge updates into the current config and atomically save it."""
    if not isinstance(updates, Mapping):
        raise ValueError("配置更新必须是对象")
    current = load_config(path)
    return save_config(_deep_merge(current, updates), path)


# Convenience constants for callers that prefer constants. They are snapshots;
# functions above remain authoritative when DESKGUARDIAN_DATA_DIR changes after
# import (notably in tests). Avoid creating a user directory merely on import.
DATA_DIR = get_data_dir(create=False)
CONFIG_PATH = str(Path(DATA_DIR) / "config.json")
DB_PATH = str(Path(DATA_DIR) / "deskguardian.db")
