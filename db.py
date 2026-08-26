"""SQLite persistence for DeskGuardian.

The module keeps SQL and validation out of the web/CLI layers. Public records
are plain dictionaries, making them directly JSON serializable.
"""

import json
import os
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

try:
    from config import _harden_directory, _harden_file, get_db_path
    from utils import (
        normalize_deadline,
        normalize_weekday,
        optional_text,
        parse_bool,
        parse_int,
        require_text,
        utc_now_iso,
        validate_sections,
    )
except ImportError:  # pragma: no cover - permits importing as a package
    from .config import _harden_directory, _harden_file, get_db_path
    from .utils import (
        normalize_deadline,
        normalize_weekday,
        optional_text,
        parse_bool,
        parse_int,
        require_text,
        utc_now_iso,
        validate_sections,
    )

try:
    from version import VERSION as APP_VERSION
except ImportError:  # pragma: no cover - permits importing as a package
    try:
        from .version import VERSION as APP_VERSION
    except ImportError:  # pragma: no cover - defensive frozen-build fallback
        APP_VERSION = "unknown"


PathLike = Union[str, Path]
SCHEMA_VERSION = 1
DATA_FORMAT_VERSION = 1
DATA_FORMAT_NAME = "DeskGuardian"
_THREAD_CLAIMS = threading.local()


def _claim_tokens() -> Dict[str, str]:
    tokens = getattr(_THREAD_CLAIMS, "tokens", None)
    if tokens is None:
        tokens = {}
        _THREAD_CLAIMS.tokens = tokens
    return tokens


class _DeskGuardianConnection(sqlite3.Connection):
    """Connection whose context manager also secures and releases the handle."""

    def close(self) -> None:
        try:
            _harden_connection_files(self)
        except sqlite3.Error:
            pass
        super().close()

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    start_section INTEGER NOT NULL CHECK (start_section BETWEEN 1 AND 8),
    end_section INTEGER NOT NULL CHECK (end_section BETWEEN 1 AND 8 AND end_section >= start_section),
    room TEXT NOT NULL DEFAULT '',
    last_notified TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_courses_weekday_active
    ON courses(weekday, is_active, start_section);

CREATE TABLE IF NOT EXISTS homeworks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    content TEXT NOT NULL,
    deadline TEXT NOT NULL CHECK (
        length(deadline) = 10 AND
        deadline GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    ),
    notified_day1 INTEGER NOT NULL DEFAULT 0 CHECK (notified_day1 IN (0, 1)),
    notified_day0 INTEGER NOT NULL DEFAULT 0 CHECK (notified_day0 IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_homeworks_deadline ON homeworks(deadline);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    scheduled_for TEXT,
    payload TEXT,
    status TEXT NOT NULL CHECK (status IN ('claimed', 'completed', 'failed')),
    claim_token TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    completed_at TEXT,
    failed_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notification_events_status
    ON notification_events(status, claimed_at);
"""


def _execute_schema(connection: sqlite3.Connection) -> None:
    """Execute the static schema without ``executescript``'s implicit COMMIT."""
    statement = ""
    for line in _SCHEMA.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                connection.execute(sql)
            statement = ""
    if statement.strip():  # pragma: no cover - protects future schema edits
        raise RuntimeError("数据库 schema 包含不完整的 SQL")


def _migrate_schema_0_to_1(connection: sqlite3.Connection) -> None:
    # Version 0 covers both an empty SQLite file and installations that already
    # had the first public tables before user_version tracking was introduced.
    _execute_schema(connection)


# Maps the current version to (next version, migration). Keeping each edge
# explicit makes future upgrades reviewable and independently transactional.
MIGRATIONS = {0: (1, _migrate_schema_0_to_1)}


def _harden_connection_files(connection: sqlite3.Connection) -> None:
    if os.name == "nt":
        return
    for row in connection.execute("PRAGMA database_list").fetchall():
        filename = row[2]
        if not filename:
            continue
        database = Path(filename)
        _harden_directory(database.parent)
        _harden_file(database)
        _harden_file(Path(str(database) + "-wal"))
        _harden_file(Path(str(database) + "-shm"))


def _database_backup_path(target: Path, version: int) -> Path:
    return target.with_name("%s.v%d.bak" % (target.name, version))


def _backup_database(
    connection: sqlite3.Connection, target: Path, version: int
) -> Path:
    """Atomically create one discoverable SQLite backup for an upgrade."""
    backup = _database_backup_path(target, version)
    if backup.exists():
        _harden_file(backup)
        return backup
    fd, temporary_name = tempfile.mkstemp(
        prefix=backup.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    os.close(fd)
    temporary = Path(temporary_name)
    destination = None
    try:
        destination = sqlite3.connect(str(temporary))
        connection.backup(destination)
        destination.close()
        destination = None
        _harden_file(temporary)
        os.replace(str(temporary), str(backup))
        _harden_file(backup)
    except Exception:
        if destination is not None:
            destination.close()
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return backup


def _normalized_db_path(db_path: Optional[PathLike]) -> str:
    path = str(db_path) if db_path is not None else get_db_path()
    if path == ":memory:":
        return path
    return str(Path(path).expanduser().resolve())


def _open_connection(path: str, enable_wal: bool) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path,
        timeout=15.0,
        isolation_level=None,
        factory=_DeskGuardianConnection,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        # Do not invoke the subclass close hook: a future-version database must
        # be rejected without even a best-effort metadata permission rewrite.
        sqlite3.Connection.close(connection)
        raise RuntimeError(
            "数据库版本 %d 高于当前支持的版本 %d，请升级 DeskGuardian"
            % (current, SCHEMA_VERSION)
        )
    if enable_wal:
        # SQLite silently uses a suitable alternative for memory-only databases.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    _harden_connection_files(connection)
    return connection


def connect(db_path: Optional[PathLike] = None) -> sqlite3.Connection:
    """Open a configured connection with foreign keys, timeout and WAL enabled."""
    path = _normalized_db_path(db_path)
    if path != ":memory:":
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)
        _harden_directory(parent)
    return _open_connection(path, enable_wal=True)


def init_db(db_path: Optional[PathLike] = None) -> None:
    """Create or migrate the schema one explicit transaction at a time.

    A database produced by a newer application is rejected. Existing file
    databases receive one SQLite-consistent, versioned backup before the first
    pending migration. ``user_version`` changes in the same transaction as its
    migration and therefore cannot be left ahead after a failed step.
    """
    path = _normalized_db_path(db_path)
    existed = path != ":memory:" and Path(path).is_file()
    if path != ":memory:":
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)
        _harden_directory(parent)
    # Do not switch journal modes before backing up an existing old database:
    # the backup must represent the exact pre-upgrade SQLite state.
    with _open_connection(path, enable_wal=False) as connection:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                "数据库版本 %d 高于当前支持的版本 %d，请升级 DeskGuardian"
                % (current, SCHEMA_VERSION)
            )
        if current < SCHEMA_VERSION and existed:
            _backup_database(connection, Path(path), current)

        while current < SCHEMA_VERSION:
            step = MIGRATIONS.get(current)
            if step is None:
                raise RuntimeError("缺少从数据库版本 %d 开始的迁移步骤" % current)
            next_version, migration = step
            if next_version <= current or next_version > SCHEMA_VERSION:
                raise RuntimeError("数据库迁移版本定义无效")
            connection.execute("BEGIN IMMEDIATE")
            try:
                migration(connection)
                connection.execute("PRAGMA user_version = %d" % next_version)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            current = next_version
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        _harden_connection_files(connection)


def _connection(db_path: Optional[PathLike]) -> sqlite3.Connection:
    init_db(db_path)
    return connect(db_path)


def _row(record: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if record is None:
        return None
    result = dict(record)
    for field in ("is_active", "notified_day1", "notified_day0"):
        if field in result:
            result[field] = bool(result[field])
    if "payload" in result and isinstance(result["payload"], str):
        try:
            result["payload"] = json.loads(result["payload"])
        except json.JSONDecodeError:
            pass
    return result


def _positive_id(value: Any) -> int:
    return parse_int(value, "id", 1)


def _normalize_iso_timestamp(
    value: Any, field: str, optional: bool = True
) -> Optional[str]:
    if value is None or value == "":
        if optional:
            return None
        raise ValueError("%s 不能为空" % field)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    text = require_text(value, field, 64)
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError("%s 必须是 ISO 日期时间" % field)
    return text


def _validate_course(
    data: Mapping[str, Any], current: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError("课程必须是对象")
    # Parser metadata is harmless at the persistence boundary and is ignored;
    # all persisted fields below are still strictly validated.
    allowed = {
        "name",
        "weekday",
        "start_section",
        "end_section",
        "room",
        "last_notified",
        "is_active",
    }
    metadata = {"confidence", "source", "parser", "selected"}
    unknown = set(data) - allowed - metadata
    if unknown:
        raise ValueError("未知课程字段: %s" % ", ".join(sorted(unknown)))
    merged = dict(current or {})
    merged.update(data)
    name = require_text(merged.get("name"), "name", 200)
    weekday = normalize_weekday(merged.get("weekday"))
    start, end = validate_sections(
        merged.get("start_section"), merged.get("end_section")
    )
    room = optional_text(merged.get("room"), "room", 200)
    last_notified = merged.get("last_notified")
    if last_notified is not None:
        last_notified = _normalize_iso_timestamp(last_notified, "last_notified")
    active = parse_bool(merged.get("is_active", True), "is_active")
    return {
        "name": name,
        "weekday": weekday,
        "start_section": start,
        "end_section": end,
        "room": room,
        "last_notified": last_notified,
        "is_active": int(active),
    }


def _validate_homework(
    data: Mapping[str, Any], current: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError("作业必须是对象")
    allowed = {"subject", "content", "deadline", "notified_day1", "notified_day0"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError("未知作业字段: %s" % ", ".join(sorted(unknown)))
    merged = dict(current or {})
    merged.update(data)
    return {
        "subject": require_text(merged.get("subject"), "subject", 200),
        "content": require_text(merged.get("content"), "content", 2000),
        "deadline": normalize_deadline(merged.get("deadline")),
        "notified_day1": int(
            parse_bool(merged.get("notified_day1", False), "notified_day1")
        ),
        "notified_day0": int(
            parse_bool(merged.get("notified_day0", False), "notified_day0")
        ),
    }


def _insert_course(
    connection: sqlite3.Connection,
    data: Mapping[str, Any],
    record_id: Optional[int] = None,
) -> int:
    item = _validate_course(data)
    now = utc_now_iso()
    fields = "name, weekday, start_section, end_section, room, last_notified, is_active, created_at, updated_at"
    values = [
        item["name"],
        item["weekday"],
        item["start_section"],
        item["end_section"],
        item["room"],
        item["last_notified"],
        item["is_active"],
        now,
        now,
    ]
    if record_id is None:
        cursor = connection.execute(
            "INSERT INTO courses (%s) VALUES (%s)"
            % (fields, ",".join("?" for _ in values)),
            values,
        )
    else:
        cursor = connection.execute(
            "INSERT INTO courses (id, %s) VALUES (?, %s)"
            % (fields, ",".join("?" for _ in values)),
            [record_id] + values,
        )
    return int(cursor.lastrowid)


def add_course(
    name: Union[str, Mapping[str, Any], None] = None,
    weekday: Any = None,
    start_section: Any = None,
    end_section: Any = None,
    room: str = "",
    is_active: Any = True,
    db_path: Optional[PathLike] = None,
    last_notified: Optional[str] = None,
    **fields: Any
) -> Dict[str, Any]:
    """Insert one course. Accept a mapping, positional fields, or keywords."""
    if isinstance(name, Mapping):
        if (
            any(value is not None for value in (weekday, start_section, end_section))
            or fields
        ):
            raise ValueError("使用课程对象时不能再传单独字段")
        data = dict(name)
    else:
        if name is None:
            name = fields.pop("course_name", fields.pop("name", None))
        data = {
            "name": name,
            "weekday": weekday,
            "start_section": start_section,
            "end_section": end_section,
            "room": room,
            "is_active": is_active,
            "last_notified": last_notified,
        }
        data.update(fields)
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        record_id = _insert_course(connection, data)
        connection.commit()
        return get_course(record_id, db_path, connection)


def get_course(
    course_id: Any,
    db_path: Optional[PathLike] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    own = connection is None
    connection = connection or _connection(db_path)
    try:
        return _row(
            connection.execute(
                "SELECT * FROM courses WHERE id = ?", (_positive_id(course_id),)
            ).fetchone()
        )
    finally:
        if own:
            connection.close()


def list_courses(
    weekday: Any = None,
    search: Optional[str] = None,
    active_only: bool = False,
    db_path: Optional[PathLike] = None,
) -> List[Dict[str, Any]]:
    clauses, values = [], []
    if weekday is not None:
        clauses.append("weekday = ?")
        values.append(normalize_weekday(weekday))
    if search:
        clauses.append("name LIKE ? ESCAPE '\\'")
        escaped = (
            str(search).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        values.append("%%%s%%" % escaped)
    if active_only:
        clauses.append("is_active = 1")
    sql = "SELECT * FROM courses" + (
        " WHERE " + " AND ".join(clauses) if clauses else ""
    )
    sql += " ORDER BY weekday, start_section, end_section, name, id"
    with _connection(db_path) as connection:
        return [_row(item) for item in connection.execute(sql, values).fetchall()]


def update_course(
    course_id: Any,
    data: Optional[Mapping[str, Any]] = None,
    db_path: Optional[PathLike] = None,
    **changes: Any
) -> Optional[Dict[str, Any]]:
    if data is None:
        data = changes
    elif changes:
        merged_changes = dict(data)
        merged_changes.update(changes)
        data = merged_changes
    if not isinstance(data, Mapping):
        raise ValueError("课程更新必须是对象")
    record_id = _positive_id(course_id)
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = _row(
            connection.execute(
                "SELECT * FROM courses WHERE id = ?", (record_id,)
            ).fetchone()
        )
        if existing is None:
            connection.rollback()
            return None
        if not data:
            connection.rollback()
            return existing
        item = _validate_course(data, existing)
        connection.execute(
            "UPDATE courses SET name=?, weekday=?, start_section=?, end_section=?, room=?, "
            "last_notified=?, is_active=?, updated_at=? WHERE id=?",
            (
                item["name"],
                item["weekday"],
                item["start_section"],
                item["end_section"],
                item["room"],
                item["last_notified"],
                item["is_active"],
                utc_now_iso(),
                record_id,
            ),
        )
        connection.commit()
        return get_course(record_id, db_path, connection)


def delete_course(course_id: Any, db_path: Optional[PathLike] = None) -> bool:
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = (
            connection.execute(
                "DELETE FROM courses WHERE id = ?", (_positive_id(course_id),)
            ).rowcount
            == 1
        )
        connection.commit()
        return changed


def bulk_add_courses(
    courses: Iterable[Mapping[str, Any]], db_path: Optional[PathLike] = None
) -> List[Dict[str, Any]]:
    """Validate and insert all courses in one transaction (all-or-nothing)."""
    items = list(courses)
    if not items:
        return []
    validated = [_validate_course(item) for item in items]
    ids = []
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for item in validated:
                ids.append(_insert_course(connection, item))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return [get_course(record_id, db_path, connection) for record_id in ids]


# Web/CLI terminology alias.
import_courses = bulk_add_courses


def _insert_homework(
    connection: sqlite3.Connection,
    data: Mapping[str, Any],
    record_id: Optional[int] = None,
) -> int:
    item = _validate_homework(data)
    now = utc_now_iso()
    values = [
        item["subject"],
        item["content"],
        item["deadline"],
        item["notified_day1"],
        item["notified_day0"],
        now,
        now,
    ]
    if record_id is None:
        cursor = connection.execute(
            "INSERT INTO homeworks (subject, content, deadline, notified_day1, notified_day0, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    else:
        cursor = connection.execute(
            "INSERT INTO homeworks (id, subject, content, deadline, notified_day1, notified_day0, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [record_id] + values,
        )
    return int(cursor.lastrowid)


def add_homework(
    subject: Union[str, Mapping[str, Any], None] = None,
    content: Any = None,
    deadline: Any = None,
    db_path: Optional[PathLike] = None,
    **fields: Any
) -> Dict[str, Any]:
    if isinstance(subject, Mapping):
        if content is not None or deadline is not None or fields:
            raise ValueError("使用作业对象时不能再传单独字段")
        data = dict(subject)
    else:
        if subject is None:
            subject = fields.pop("subject", None)
        data = {"subject": subject, "content": content, "deadline": deadline}
        data.update(fields)
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        record_id = _insert_homework(connection, data)
        connection.commit()
        return get_homework(record_id, db_path, connection)


def get_homework(
    homework_id: Any,
    db_path: Optional[PathLike] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    own = connection is None
    connection = connection or _connection(db_path)
    try:
        return _row(
            connection.execute(
                "SELECT * FROM homeworks WHERE id = ?", (_positive_id(homework_id),)
            ).fetchone()
        )
    finally:
        if own:
            connection.close()


def list_homeworks(
    due_before: Any = None,
    due_after: Any = None,
    db_path: Optional[PathLike] = None,
    **filters: Any
) -> List[Dict[str, Any]]:
    # Common API vocabulary aliases.
    if due_before is None:
        due_before = filters.pop("deadline_before", filters.pop("end_date", None))
    if due_after is None:
        due_after = filters.pop("deadline_after", filters.pop("start_date", None))
    if filters:
        raise ValueError("未知作业筛选条件: %s" % ", ".join(sorted(filters)))
    clauses, values = [], []
    if due_before is not None:
        clauses.append("deadline <= ?")
        values.append(normalize_deadline(due_before))
    if due_after is not None:
        clauses.append("deadline >= ?")
        values.append(normalize_deadline(due_after))
    sql = "SELECT * FROM homeworks" + (
        " WHERE " + " AND ".join(clauses) if clauses else ""
    )
    sql += " ORDER BY deadline, subject, id"
    with _connection(db_path) as connection:
        return [_row(item) for item in connection.execute(sql, values).fetchall()]


def update_homework(
    homework_id: Any,
    data: Optional[Mapping[str, Any]] = None,
    db_path: Optional[PathLike] = None,
    **changes: Any
) -> Optional[Dict[str, Any]]:
    if data is None:
        data = changes
    elif changes:
        merged_changes = dict(data)
        merged_changes.update(changes)
        data = merged_changes
    if not isinstance(data, Mapping):
        raise ValueError("作业更新必须是对象")
    record_id = _positive_id(homework_id)
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = _row(
            connection.execute(
                "SELECT * FROM homeworks WHERE id = ?", (record_id,)
            ).fetchone()
        )
        if existing is None:
            connection.rollback()
            return None
        if not data:
            connection.rollback()
            return existing
        item = _validate_homework(data, existing)
        # A changed due date defines a new reminder lifecycle unless the caller
        # explicitly supplies notification state (useful for trusted imports).
        if "deadline" in data and item["deadline"] != existing["deadline"]:
            if "notified_day1" not in data:
                item["notified_day1"] = 0
            if "notified_day0" not in data:
                item["notified_day0"] = 0
        connection.execute(
            "UPDATE homeworks SET subject=?, content=?, deadline=?, notified_day1=?, notified_day0=?, updated_at=? WHERE id=?",
            (
                item["subject"],
                item["content"],
                item["deadline"],
                item["notified_day1"],
                item["notified_day0"],
                utc_now_iso(),
                record_id,
            ),
        )
        connection.commit()
        return get_homework(record_id, db_path, connection)


def delete_homework(homework_id: Any, db_path: Optional[PathLike] = None) -> bool:
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = (
            connection.execute(
                "DELETE FROM homeworks WHERE id = ?", (_positive_id(homework_id),)
            ).rowcount
            == 1
        )
        connection.commit()
        return changed


def mark_course_notified(
    course_id: Any,
    notified_at: Optional[str] = None,
    db_path: Optional[PathLike] = None,
) -> bool:
    value = _normalize_iso_timestamp(
        notified_at or utc_now_iso(), "notified_at", optional=False
    )
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = (
            connection.execute(
                "UPDATE courses SET last_notified=?, updated_at=? WHERE id=?",
                (value, utc_now_iso(), _positive_id(course_id)),
            ).rowcount
            == 1
        )
        connection.commit()
        return changed


def mark_homework_notified(
    homework_id: Any, day: Any, db_path: Optional[PathLike] = None
) -> bool:
    if isinstance(day, str):
        normalized_day = day.strip().lower().replace("_", "")
        aliases = {
            "day0": 0,
            "当天": 0,
            "today": 0,
            "day1": 1,
            "提前一天": 1,
            "tomorrow": 1,
        }
        day = aliases.get(normalized_day, day)
    day_number = parse_int(day, "day", 0, 1)
    column = "notified_day%d" % day_number
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = (
            connection.execute(
                "UPDATE homeworks SET %s=1, updated_at=? WHERE id=?" % column,
                (utc_now_iso(), _positive_id(homework_id)),
            ).rowcount
            == 1
        )
        connection.commit()
        return changed


def _encode_setting(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ValueError("设置值必须可 JSON 序列化")


def _decode_setting(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def set_setting(key: str, value: Any, db_path: Optional[PathLike] = None) -> Any:
    normalized_key = require_text(key, "key", 100)
    encoded = _encode_setting(value)
    now = utc_now_iso()
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (normalized_key, encoded, now),
        )
        connection.commit()
    return value


def get_setting(
    key: str, default: Any = None, db_path: Optional[PathLike] = None
) -> Any:
    with _connection(db_path) as connection:
        record = connection.execute(
            "SELECT value FROM settings WHERE key=?", (require_text(key, "key", 100),)
        ).fetchone()
    return default if record is None else _decode_setting(record["value"])


def list_settings(db_path: Optional[PathLike] = None) -> Dict[str, Any]:
    with _connection(db_path) as connection:
        records = connection.execute(
            "SELECT key, value FROM settings ORDER BY key"
        ).fetchall()
    return {record["key"]: _decode_setting(record["value"]) for record in records}


def delete_setting(key: str, db_path: Optional[PathLike] = None) -> bool:
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = (
            connection.execute(
                "DELETE FROM settings WHERE key=?", (require_text(key, "key", 100),)
            ).rowcount
            == 1
        )
        connection.commit()
        return changed


def claim_notification(
    event_key: str,
    event_type: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[Any] = None,
    scheduled_for: Optional[str] = None,
    payload: Any = None,
    stale_after_seconds: int = 300,
    db_path: Optional[PathLike] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically claim a unique notification event.

    Returns the claimed event (including an unguessable ``claim_token``), or
    ``None`` when another worker owns it or the event has completed. Failed and
    stale claims are retryable. Completion should pass the token to prevent one
    worker from completing another worker's reclaimed event.
    """
    key = require_text(event_key, "event_key", 300)
    kind = require_text(event_type, "event_type", 100)
    entity_kind = optional_text(entity_type, "entity_type", 100, default="") or None
    normalized_id = (
        optional_text(str(entity_id), "entity_id", 200)
        if entity_id is not None
        else None
    )
    stale_seconds = parse_int(stale_after_seconds, "stale_after_seconds", 1, 86400)
    normalized_scheduled = _normalize_iso_timestamp(scheduled_for, "scheduled_for")
    payload_json = None if payload is None else _encode_setting(payload)
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    stale_before = (now_dt - timedelta(seconds=stale_seconds)).isoformat(
        timespec="seconds"
    )
    token = uuid.uuid4().hex

    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM notification_events WHERE event_key=?", (key,)
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO notification_events(event_key,event_type,entity_type,entity_id,scheduled_for,payload,status,"
                "claim_token,claimed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,'claimed',?,?,?,?)",
                (
                    key,
                    kind,
                    entity_kind,
                    normalized_id,
                    normalized_scheduled,
                    payload_json,
                    token,
                    now,
                    now,
                    now,
                ),
            )
        elif existing["status"] == "completed" or (
            existing["status"] == "claimed" and existing["claimed_at"] > stale_before
        ):
            connection.rollback()
            return None
        else:
            connection.execute(
                "UPDATE notification_events SET event_type=?,entity_type=?,entity_id=?,scheduled_for=?,payload=?,"
                "status='claimed',claim_token=?,claimed_at=?,completed_at=NULL,failed_at=NULL,error=NULL,updated_at=? "
                "WHERE event_key=?",
                (
                    kind,
                    entity_kind,
                    normalized_id,
                    normalized_scheduled,
                    payload_json,
                    token,
                    now,
                    now,
                    key,
                ),
            )
        connection.commit()
        return _row(
            connection.execute(
                "SELECT * FROM notification_events WHERE event_key=?", (key,)
            ).fetchone()
        )


def complete_notification(
    event_key: str,
    claim_token: Optional[str] = None,
    db_path: Optional[PathLike] = None,
) -> bool:
    """Complete an active event; passing its token is strongly recommended."""
    key = require_text(event_key, "event_key", 300)
    clauses, values = ["event_key=?", "status='claimed'"], [key]
    if claim_token is not None:
        clauses.append("claim_token=?")
        values.append(require_text(claim_token, "claim_token", 100))
    now = utc_now_iso()
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = (
            connection.execute(
                "UPDATE notification_events SET status='completed',completed_at=?,updated_at=? WHERE "
                + " AND ".join(clauses),
                [now, now] + values,
            ).rowcount
            == 1
        )
        connection.commit()
        return changed


def fail_notification(
    event_key: str,
    error: Any,
    claim_token: Optional[str] = None,
    db_path: Optional[PathLike] = None,
) -> bool:
    """Mark an active event failed; failed events may later be reclaimed."""
    key = require_text(event_key, "event_key", 300)
    message = optional_text(str(error), "error", 2000)
    clauses, values = ["event_key=?", "status='claimed'"], [key]
    if claim_token is not None:
        clauses.append("claim_token=?")
        values.append(require_text(claim_token, "claim_token", 100))
    now = utc_now_iso()
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = (
            connection.execute(
                "UPDATE notification_events SET status='failed',failed_at=?,error=?,updated_at=? WHERE "
                + " AND ".join(clauses),
                [now, message, now] + values,
            ).rowcount
            == 1
        )
        connection.commit()
        return changed


def get_notification_event(
    event_key: str, db_path: Optional[PathLike] = None
) -> Optional[Dict[str, Any]]:
    with _connection(db_path) as connection:
        return _row(
            connection.execute(
                "SELECT * FROM notification_events WHERE event_key=?",
                (require_text(event_key, "event_key", 300),),
            ).fetchone()
        )


def list_notification_events(
    status: Optional[str] = None, limit: Any = 100, db_path: Optional[PathLike] = None
) -> List[Dict[str, Any]]:
    values = []
    sql = "SELECT * FROM notification_events"
    if status is not None:
        if status not in ("claimed", "completed", "failed"):
            raise ValueError("status 无效")
        sql += " WHERE status=?"
        values.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    values.append(parse_int(limit, "limit", 1, 10000))
    with _connection(db_path) as connection:
        return [_row(item) for item in connection.execute(sql, values).fetchall()]


def delete_notification_event(
    event_key: str, db_path: Optional[PathLike] = None
) -> bool:
    """Delete one event, primarily for administrative recovery/testing."""
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = (
            connection.execute(
                "DELETE FROM notification_events WHERE event_key=?",
                (require_text(event_key, "event_key", 300),),
            ).rowcount
            == 1
        )
        connection.commit()
        return changed


def purge_notification_events(
    before: Optional[str] = None, db_path: Optional[PathLike] = None
) -> int:
    """Delete completed/failed history, optionally only rows before ISO time."""
    sql = "DELETE FROM notification_events WHERE status IN ('completed','failed')"
    values = []
    if before is not None:
        cutoff = require_text(before, "before", 64)
        try:
            datetime.fromisoformat(
                cutoff[:-1] + "+00:00" if cutoff.endswith(("Z", "z")) else cutoff
            )
        except ValueError:
            raise ValueError("before 必须是 ISO 日期时间")
        sql += " AND updated_at < ?"
        values.append(cutoff)
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        count = connection.execute(sql, values).rowcount
        connection.commit()
        return int(count)


# Scheduler-facing claim follows its documented boolean contract. Call
# claim_notification() directly when the claim token/record is required.
def claim_notification_event(
    event_key: str,
    event_type: str,
    entity_id: Optional[Any] = None,
    scheduled_for: Optional[str] = None,
    entity_type: Optional[str] = None,
    payload: Any = None,
    stale_after_seconds: int = 300,
    db_path: Optional[PathLike] = None,
) -> bool:
    claimed = claim_notification(
        event_key=event_key,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        scheduled_for=scheduled_for,
        payload=payload,
        stale_after_seconds=stale_after_seconds,
        db_path=db_path,
    )
    if claimed is None:
        return False
    _claim_tokens()[event_key] = claimed["claim_token"]
    return True


def complete_notification_event(
    event_key: str, db_path: Optional[PathLike] = None
) -> bool:
    tokens = _claim_tokens()
    token = tokens.get(event_key)
    if token is None:
        return False
    changed = complete_notification(event_key, claim_token=token, db_path=db_path)
    if changed:
        tokens.pop(event_key, None)
    return changed


def fail_notification_event(
    event_key: str, error: Any, db_path: Optional[PathLike] = None
) -> bool:
    tokens = _claim_tokens()
    token = tokens.get(event_key)
    if token is None:
        return False
    changed = fail_notification(event_key, error, claim_token=token, db_path=db_path)
    if changed:
        tokens.pop(event_key, None)
    return changed


claim_event = claim_notification_event
complete_event = complete_notification_event
fail_event = fail_notification_event


def export_data(
    db_path: Optional[PathLike] = None, include_notification_events: bool = False
) -> Dict[str, Any]:
    result = {
        "format": DATA_FORMAT_NAME,
        # ``version`` is retained so data exported by this release remains
        # understandable to the original version-1 importer.
        "version": DATA_FORMAT_VERSION,
        "format_version": DATA_FORMAT_VERSION,
        "app_version": APP_VERSION,
        "exported_at": utc_now_iso(),
        "courses": list_courses(db_path=db_path),
        "homeworks": list_homeworks(db_path=db_path),
        "settings": list_settings(db_path=db_path),
    }
    if include_notification_events:
        result["notification_events"] = list_notification_events(
            limit=10000, db_path=db_path
        )
    return result


def export_json(
    path: Optional[PathLike] = None,
    db_path: Optional[PathLike] = None,
    include_notification_events: bool = False,
) -> str:
    """Return pretty JSON and optionally write it atomically to ``path``."""
    text = (
        json.dumps(
            export_data(db_path, include_notification_events),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    if path is not None:
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()
    return text


def _clean_import_record(
    record: Mapping[str, Any], fields: Sequence[str]
) -> Dict[str, Any]:
    return {key: record[key] for key in fields if key in record}


def _validate_import_format(data: Mapping[str, Any]) -> None:
    """Validate official export metadata while allowing simple user objects."""
    has_format = "format" in data
    if has_format and data.get("format") != DATA_FORMAT_NAME:
        raise ValueError("format 必须是 %s" % DATA_FORMAT_NAME)

    if "format_version" in data:
        value = data["format_version"]
        field = "format_version"
    elif has_format and "version" in data:
        # Compatibility with exports created before ``format_version`` existed.
        value = data["version"]
        field = "version"
    elif has_format:
        raise ValueError("DeskGuardian 导入数据缺少 format_version")
    else:
        # A hand-written payload containing only courses/homeworks/settings has
        # always been a supported interchange form. A supplied format_version,
        # however, is never ignored merely because ``format`` was omitted.
        return

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("%s 必须是正整数" % field)
    if "format_version" in data and "version" in data:
        legacy = data["version"]
        if (
            isinstance(legacy, bool)
            or not isinstance(legacy, int)
            or legacy != value
        ):
            raise ValueError("version 与 format_version 不一致")
    if value > DATA_FORMAT_VERSION:
        raise ValueError(
            "导入格式版本 %d 高于当前支持的版本 %d，请升级 DeskGuardian"
            % (value, DATA_FORMAT_VERSION)
        )
    if value != DATA_FORMAT_VERSION:
        raise ValueError("不支持的导入格式版本: %d" % value)


def import_data(
    data: Mapping[str, Any],
    mode: str = "merge",
    db_path: Optional[PathLike] = None,
    replace: Optional[bool] = None,
) -> Dict[str, int]:
    """Import exported data transactionally.

    ``merge`` appends courses/homeworks and upserts settings. ``replace`` first
    clears those three datasets. IDs and notification history are intentionally
    ignored, preventing imported state from suppressing future reminders.
    The optional boolean ``replace`` is a CLI compatibility synonym for mode.
    """
    if not isinstance(data, Mapping):
        raise ValueError("导入数据必须是对象")
    _validate_import_format(data)
    if replace is not None:
        mode = "replace" if parse_bool(replace, "replace") else "merge"
    if mode not in ("merge", "replace"):
        raise ValueError("mode 必须是 merge 或 replace")
    courses = data.get("courses", [])
    homeworks = data.get("homeworks", [])
    settings = data.get("settings", {})
    if not isinstance(courses, list) or not all(
        isinstance(item, Mapping) for item in courses
    ):
        raise ValueError("courses 必须是对象数组")
    if not isinstance(homeworks, list) or not all(
        isinstance(item, Mapping) for item in homeworks
    ):
        raise ValueError("homeworks 必须是对象数组")
    if not isinstance(settings, Mapping):
        raise ValueError("settings 必须是对象")

    course_fields = (
        "name",
        "weekday",
        "start_section",
        "end_section",
        "room",
        "is_active",
    )
    homework_fields = (
        "subject",
        "content",
        "deadline",
        "notified_day1",
        "notified_day0",
    )
    validated_courses = [
        _validate_course(_clean_import_record(item, course_fields)) for item in courses
    ]
    validated_homeworks = [
        _validate_homework(_clean_import_record(item, homework_fields))
        for item in homeworks
    ]
    encoded_settings = []
    for key, value in settings.items():
        if not isinstance(key, str):
            raise ValueError("settings 的 key 必须是文本")
        encoded_settings.append((require_text(key, "key", 100), _encode_setting(value)))

    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if mode == "replace":
                connection.execute("DELETE FROM courses")
                connection.execute("DELETE FROM homeworks")
                connection.execute("DELETE FROM settings")
            for item in validated_courses:
                _insert_course(connection, item)
            for item in validated_homeworks:
                _insert_homework(connection, item)
            now = utc_now_iso()
            connection.executemany(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                [(key, value, now) for key, value in encoded_settings],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "courses": len(validated_courses),
        "homeworks": len(validated_homeworks),
        "settings": len(encoded_settings),
    }


def import_json(
    source: Union[str, bytes, Path, Mapping[str, Any]],
    mode: str = "merge",
    db_path: Optional[PathLike] = None,
    replace: Optional[bool] = None,
) -> Dict[str, int]:
    """Import from a mapping, JSON text/bytes, or an existing JSON file path."""
    if isinstance(source, Mapping):
        data = source
    elif isinstance(source, bytes):
        data = json.loads(source.decode("utf-8-sig"))
    elif isinstance(source, Path):
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    elif isinstance(source, str):
        stripped = source.lstrip()
        if stripped.startswith(("{", "[")):
            data = json.loads(source)
        else:
            try:
                candidate = Path(source)
                is_file = candidate.is_file()
            except (OSError, ValueError):
                is_file = False
            if is_file:
                data = json.loads(candidate.read_text(encoding="utf-8-sig"))
            else:
                data = json.loads(source)
    else:
        raise ValueError("source 必须是 JSON、路径或对象")
    return import_data(data, mode, db_path, replace=replace)


# Compatibility aliases used by the CLI/Web adapters and convenient for third
# party integrations. They retain the contracts of their canonical functions.
add_courses = bulk_add_courses
create_course = add_course
insert_course = add_course
get_courses = list_courses
get_all_courses = list_courses
remove_course = delete_course
create_homework = add_homework
insert_homework = add_homework
get_homeworks = list_homeworks
get_all_homeworks = list_homeworks
remove_homework = delete_homework
initialize_db = init_db
get_connection = connect
