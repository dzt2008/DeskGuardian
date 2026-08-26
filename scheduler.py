"""Polling scheduler for course, homework and daily-summary reminders.

The database object is intentionally injected.  Its preferred notification
contract is::

    claim_notification_event(event_key, event_type, entity_id, scheduled_for)
        -> bool
    complete_notification_event(event_key)
    fail_notification_event(event_key, error)

``claim_notification_event`` must atomically insert/claim a unique event and
return false if another scan/process already claimed it.  For compatibility,
``claim_event``/``complete_event``/``fail_event`` are also understood, and a
SQLite connection can be used directly as a final fallback.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import sqlite3
import threading
import time
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from config import load_config
from notifier import DesktopNotifier
from utils import parse_iso_datetime


LOGGER = logging.getLogger(__name__)


class SchedulerError(RuntimeError):
    """Raised for an invalid scheduler/database integration."""


def _row_dict(row: Any) -> Dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    if hasattr(row, "__dict__"):
        return {
            key: value for key, value in vars(row).items() if not key.startswith("_")
        }
    raise SchedulerError("数据库返回了无法识别的记录类型: %s" % type(row).__name__)


def _as_rows(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    # Some database layers return {"items": [...]} for API convenience.
    if isinstance(value, Mapping):
        for key in ("items", "courses", "homeworks", "data"):
            if key in value and isinstance(value[key], (list, tuple)):
                value = value[key]
                break
        else:
            value = [value]
    return [_row_dict(row) for row in value]


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "否")
    return bool(value)


def _stable_id(kind: str, row: Mapping[str, Any]) -> str:
    if row.get("id") is not None:
        return str(row["id"])
    material = "|".join("%s=%s" % (key, row.get(key, "")) for key in sorted(row))
    return hashlib.sha256((kind + "|" + material).encode("utf-8")).hexdigest()[:16]


class _EventStore:
    """Adapt database event methods, with an atomic SQLite fallback."""

    def __init__(self, database: Any) -> None:
        self.database = database

    @staticmethod
    def _invoke(method: Callable[..., Any], values: Mapping[str, Any]) -> Any:
        """Call a method by matching its named parameters.

        This permits DB implementations to omit optional metadata while not
        hiding TypeError raised *inside* the database method.
        """
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            return method(values["event_key"])
        kwargs: Dict[str, Any] = {}
        aliases = {
            "key": "event_key",
            "kind": "event_type",
            "type": "event_type",
            "scheduled_at": "scheduled_for",
            "event_time": "scheduled_for",
            "message": "error",
            "error_message": "error",
        }
        has_var_kw = False
        for name, parameter in signature.parameters.items():
            if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                has_var_kw = True
                continue
            source = name if name in values else aliases.get(name)
            if source in values:
                kwargs[name] = values[source]
        if has_var_kw:
            kwargs.update(
                {key: value for key, value in values.items() if key not in kwargs}
            )
        try:
            signature.bind(**kwargs)
        except TypeError as exc:
            raise SchedulerError("数据库通知事件方法签名不兼容: %s" % exc) from exc
        return method(**kwargs)

    def _find_method(self, names: Sequence[str]) -> Optional[Callable[..., Any]]:
        for name in names:
            method = getattr(self.database, name, None)
            if callable(method):
                return method
        return None

    def _connection(self) -> Optional[sqlite3.Connection]:
        if isinstance(self.database, sqlite3.Connection):
            return self.database
        for name in ("connection", "conn", "_connection", "_conn"):
            candidate = getattr(self.database, name, None)
            if isinstance(candidate, sqlite3.Connection):
                return candidate
        getter = getattr(self.database, "get_connection", None)
        if callable(getter):
            candidate = getter()
            if isinstance(candidate, sqlite3.Connection):
                return candidate
        return None

    @staticmethod
    def _ensure_sqlite_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_events (
                event_key TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                entity_id TEXT,
                scheduled_for TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'claimed',
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.commit()

    @staticmethod
    def _columns(connection: sqlite3.Connection) -> List[str]:
        return [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(notification_events)")
        ]

    def claim(
        self,
        event_key: str,
        event_type: str,
        entity_id: Optional[str],
        scheduled_for: datetime,
        now: datetime,
    ) -> bool:
        values = {
            "event_key": event_key,
            "event_type": event_type,
            "entity_id": entity_id,
            "scheduled_for": scheduled_for.isoformat(timespec="seconds"),
            "now": now.isoformat(timespec="seconds"),
            "created_at": now.isoformat(timespec="seconds"),
        }
        method = self._find_method(("claim_notification_event", "claim_event"))
        if method is not None:
            return bool(self._invoke(method, values))

        connection = self._connection()
        if connection is None:
            raise SchedulerError(
                "数据库必须实现 claim_notification_event，或公开 sqlite3.Connection"
            )
        self._ensure_sqlite_table(connection)
        columns = set(self._columns(connection))
        candidates = {
            "event_key": event_key,
            "event_type": event_type,
            "entity_id": entity_id,
            "scheduled_for": values["scheduled_for"],
            "status": "claimed",
            "error": None,
            "created_at": values["created_at"],
            "updated_at": values["created_at"],
        }
        selected = [name for name in candidates if name in columns]
        if "event_key" not in selected:
            raise SchedulerError("notification_events 表缺少 event_key 字段")
        placeholders = ", ".join("?" for _ in selected)
        sql = "INSERT OR IGNORE INTO notification_events (%s) VALUES (%s)" % (
            ", ".join(selected),
            placeholders,
        )
        try:
            # A single INSERT with a UNIQUE event_key is the atomic claim.
            cursor = connection.execute(sql, [candidates[name] for name in selected])
            connection.commit()
            return cursor.rowcount == 1
        except sqlite3.Error as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise SchedulerError("原子 claim 通知事件失败: %s" % exc) from exc

    def complete(self, event_key: str, now: datetime) -> None:
        values = {
            "event_key": event_key,
            "now": now.isoformat(timespec="seconds"),
            "completed_at": now.isoformat(timespec="seconds"),
        }
        method = self._find_method(
            (
                "complete_notification_event",
                "mark_notification_event_completed",
                "complete_event",
            )
        )
        if method is not None:
            self._invoke(method, values)
            return
        self._update_sqlite(event_key, "completed", None, now)

    def fail(self, event_key: str, error: str, now: datetime) -> None:
        values = {
            "event_key": event_key,
            "error": str(error),
            "now": now.isoformat(timespec="seconds"),
            "failed_at": now.isoformat(timespec="seconds"),
        }
        method = self._find_method(
            ("fail_notification_event", "mark_notification_event_failed", "fail_event")
        )
        if method is not None:
            self._invoke(method, values)
            return
        self._update_sqlite(event_key, "failed", str(error), now)

    def _update_sqlite(
        self, event_key: str, status: str, error: Optional[str], now: datetime
    ) -> None:
        connection = self._connection()
        if connection is None:
            raise SchedulerError("数据库未提供通知事件完成/失败记录方法")
        columns = set(self._columns(connection))
        assignments: List[str] = []
        parameters: List[Any] = []
        for column, value in (
            ("status", status),
            ("error", error),
            ("updated_at", now.isoformat(timespec="seconds")),
            (
                "completed_at",
                now.isoformat(timespec="seconds") if status == "completed" else None,
            ),
            (
                "failed_at",
                now.isoformat(timespec="seconds") if status == "failed" else None,
            ),
        ):
            if column in columns and (value is not None or column == "error"):
                assignments.append("%s = ?" % column)
                parameters.append(value)
        if not assignments:
            raise SchedulerError("notification_events 表无法记录事件状态")
        parameters.append(event_key)
        try:
            connection.execute(
                "UPDATE notification_events SET %s WHERE event_key = ?"
                % ", ".join(assignments),
                parameters,
            )
            connection.commit()
        except sqlite3.Error as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise SchedulerError("更新通知事件状态失败: %s" % exc) from exc


class Scheduler:
    """DeskGuardian polling scheduler.

    ``now`` may be a callable returning a datetime (ideal for tests) or a fixed
    datetime.  ``notifier`` may expose ``notify(title, message)`` or itself be
    callable.
    """

    def __init__(
        self,
        database: Any,
        config: Optional[Mapping[str, Any]] = None,
        notifier: Optional[Any] = None,
        now: Optional[Any] = None,
    ) -> None:
        self.database = database
        self.config: Dict[str, Any] = (
            dict(config) if config is not None else load_config()
        )
        self.notifier = notifier or DesktopNotifier(
            beep=bool(self.config.get("beep", False))
        )
        self._now_source = now
        self._events = _EventStore(database)

    def _now(self) -> datetime:
        source = self._now_source
        current = source() if callable(source) else source
        if current is None:
            return datetime.now().astimezone()
        if not isinstance(current, datetime):
            raise SchedulerError("now 必须是 datetime 或返回 datetime 的函数")
        return current

    def _list(self, kind: str) -> List[Dict[str, Any]]:
        if kind == "courses":
            names = ("list_courses", "get_courses", "get_all_courses", "all_courses")
            kwargs = {"active_only": True}
        else:
            names = (
                "list_homeworks",
                "get_homeworks",
                "get_all_homeworks",
                "all_homeworks",
            )
            kwargs = {}
        for name in names:
            member = getattr(self.database, name, None)
            if callable(member):
                try:
                    value = member(**kwargs)
                except TypeError:
                    # Compatibility with simple DB/fake methods with no filters.
                    value = member()
                rows = _as_rows(value)
                if kind == "courses":
                    rows = [row for row in rows if _truthy(row.get("is_active", True))]
                return rows
            if member is not None:
                rows = _as_rows(member)
                if kind == "courses":
                    rows = [row for row in rows if _truthy(row.get("is_active", True))]
                return rows
        raise SchedulerError("数据库未提供 %s 查询方法" % kind)

    @staticmethod
    def _at(day: date, hhmm: str, reference: datetime) -> datetime:
        try:
            parsed = datetime.strptime(str(hhmm), "%H:%M").time()
        except (TypeError, ValueError) as exc:
            raise SchedulerError("无效时间配置 %r，应为 HH:MM" % hhmm) from exc
        return datetime.combine(day, parsed, tzinfo=reference.tzinfo)

    def _course_start(self, course: Mapping[str, Any], now: datetime) -> datetime:
        try:
            section = str(int(course["start_section"]))
            item = self.config["section_times"][section]
            return self._at(now.date(), item["start"], now)
        except (KeyError, TypeError, ValueError) as exc:
            raise SchedulerError(
                "课程 %s 的节次或时间配置无效" % course.get("id", "?")
            ) from exc

    @staticmethod
    def _deadline(homework: Mapping[str, Any]) -> datetime:
        try:
            return parse_iso_datetime(homework.get("deadline"))
        except (ValueError, TypeError) as exc:
            raise SchedulerError(
                "作业 %s 的截止时间无效" % homework.get("id", "?")
            ) from exc

    @staticmethod
    def _comparable(value: datetime, reference: datetime) -> datetime:
        """Make a parsed deadline comparable to the injected clock."""
        if reference.tzinfo is None:
            return value.replace(tzinfo=None)
        if value.tzinfo is None:
            return value.replace(tzinfo=reference.tzinfo)
        return value.astimezone(reference.tzinfo)

    def _notify(self, title: str, message: str) -> None:
        target = getattr(self.notifier, "notify", None)
        result = (
            target(title, message)
            if callable(target)
            else self.notifier(title, message)
        )
        if result is False:
            detail = getattr(self.notifier, "last_error", None)
            raise RuntimeError(str(detail or "通知器返回失败"))

    def _deliver(
        self,
        event_key: str,
        event_type: str,
        entity_id: Optional[str],
        scheduled_for: datetime,
        title: str,
        message: str,
        now: datetime,
        result: Dict[str, Any],
    ) -> bool:
        if not self._events.claim(event_key, event_type, entity_id, scheduled_for, now):
            result["skipped"] += 1
            return False
        result["claimed"] += 1
        try:
            self._notify(title, message)
        except Exception as exc:
            try:
                self._events.fail(event_key, "%s: %s" % (type(exc).__name__, exc), now)
            except Exception:
                LOGGER.exception("记录通知失败事件时出错: %s", event_key)
            result["failed"] += 1
            result["errors"].append({"event_key": event_key, "error": str(exc)})
            LOGGER.warning("通知发送失败 %s: %s", event_key, exc)
            return False
        try:
            self._events.complete(event_key, now)
        except Exception as exc:
            result["failed"] += 1
            result["errors"].append({"event_key": event_key, "error": str(exc)})
            LOGGER.exception("通知已发送，但完成状态记录失败: %s", event_key)
            return False
        result["sent"] += 1
        return True

    def _scan_daily_summary(
        self, now: datetime, courses: List[Dict[str, Any]], result: Dict[str, Any]
    ) -> None:
        scheduled = self._at(
            now.date(), self.config.get("daily_summary_time", "07:00"), now
        )
        if now < scheduled:
            return
        todays = [
            course
            for course in courses
            if int(course.get("weekday", 0)) == now.isoweekday()
        ]
        todays.sort(key=lambda item: int(item.get("start_section", 99)))
        lines = []
        for course in todays:
            start = self._course_start(course, now).strftime("%H:%M")
            room = str(course.get("room") or "地点待定")
            lines.append(
                "%s %s（%s）" % (start, course.get("name", "未命名课程"), room)
            )
        message = "\n".join(lines) if lines else "今天没有课程安排。"
        self._deliver(
            "daily_summary:%s" % now.date().isoformat(),
            "daily_summary",
            None,
            scheduled,
            "今日课程简报",
            message,
            now,
            result,
        )

    def _scan_courses(
        self, now: datetime, courses: List[Dict[str, Any]], result: Dict[str, Any]
    ) -> None:
        reminder = timedelta(minutes=int(self.config.get("reminder_minutes", 5)))
        for course in courses:
            try:
                if int(course.get("weekday", 0)) != now.isoweekday():
                    continue
                starts = self._course_start(course, now)
                scheduled = starts - reminder
                # Catch up after the configured reminder instant, but never send
                # a stale "upcoming" notification once class has started.
                if not (scheduled <= now < starts):
                    continue
                entity_id = _stable_id("course", course)
                room = str(course.get("room") or "地点待定")
                minutes = max(0, int((starts - now).total_seconds() // 60))
                message = "%s 将在 %d 分钟后开始，地点：%s" % (
                    course.get("name", "课程"),
                    minutes,
                    room,
                )
                delivered = self._deliver(
                    "course:%s:%s" % (entity_id, now.date().isoformat()),
                    "course",
                    entity_id,
                    scheduled,
                    "课程即将开始",
                    message,
                    now,
                    result,
                )
                marker = getattr(self.database, "mark_course_notified", None)
                if delivered and callable(marker) and course.get("id") is not None:
                    marker(course["id"], now.isoformat(timespec="seconds"))
            except Exception as exc:
                result["errors"].append(
                    {
                        "event_key": "course:%s" % course.get("id", "?"),
                        "error": str(exc),
                    }
                )
                LOGGER.warning("跳过无效课程记录: %s", exc)

    def _scan_homeworks(
        self, now: datetime, homeworks: List[Dict[str, Any]], result: Dict[str, Any]
    ) -> None:
        eight = datetime_time(8, 0)
        for homework in homeworks:
            try:
                deadline = self._comparable(self._deadline(homework), now)
                days_left = (deadline.date() - now.date()).days
                if days_left not in (0, 1):
                    continue
                scheduled_day = deadline.date() - timedelta(days=days_left)
                scheduled = datetime.combine(scheduled_day, eight, tzinfo=now.tzinfo)
                # Both reminders are catch-up capable after 08:00.  Never send
                # an overdue same-day reminder for an explicit early deadline.
                if now < scheduled or (days_left == 0 and now > deadline):
                    continue
                entity_id = _stable_id("homework", homework)
                phase = "day1" if days_left == 1 else "day0"
                subject = str(homework.get("subject") or "未分类")
                content = str(
                    homework.get("content") or homework.get("name") or "未命名作业"
                )
                when = "明天" if days_left == 1 else "今天"
                message = "%s：%s，%s %s 截止" % (
                    subject,
                    content,
                    when,
                    deadline.strftime("%H:%M"),
                )
                delivered = self._deliver(
                    "homework:%s:%s:%s"
                    % (entity_id, phase, deadline.date().isoformat()),
                    "homework_%s" % phase,
                    entity_id,
                    scheduled,
                    "作业截止提醒",
                    message,
                    now,
                    result,
                )
                marker = getattr(self.database, "mark_homework_notified", None)
                if delivered and callable(marker) and homework.get("id") is not None:
                    marker(homework["id"], 1 if days_left == 1 else 0)
            except Exception as exc:
                result["errors"].append(
                    {
                        "event_key": "homework:%s" % homework.get("id", "?"),
                        "error": str(exc),
                    }
                )
                LOGGER.warning("跳过无效作业记录: %s", exc)

    def scan_once(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Scan and deliver all currently due events once.

        The returned counters make CLI reporting and deterministic tests easy.
        Individual malformed records or notification failures are recorded in
        ``errors`` and do not prevent unrelated reminders.
        """
        current = now if now is not None else self._now()
        if not isinstance(current, datetime):
            raise SchedulerError("scan_once(now=...) 必须传入 datetime")
        result: Dict[str, Any] = {
            "claimed": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "errors": [],
        }
        courses = self._list("courses")
        homeworks = self._list("homeworks")
        self._scan_daily_summary(current, courses, result)
        self._scan_courses(current, courses, result)
        self._scan_homeworks(current, homeworks, result)
        return result

    def run(self, stop_event: Optional[threading.Event] = None) -> None:
        """Run until interrupted or until ``stop_event`` is set."""
        interval = max(1, int(self.config.get("scan_interval_seconds", 60)))
        stopper = stop_event
        while stopper is None or not stopper.is_set():
            started = time.monotonic()
            try:
                # Web/CLI settings are persisted atomically. Reload before each
                # production scan so reminder times and beep changes take effect
                # without restarting a long-running guardian process.
                self.config = load_config()
                if isinstance(self.notifier, DesktopNotifier):
                    self.notifier.beep = bool(self.config.get("beep", False))
                self.scan_once()
            except Exception:
                # A transient DB failure must not terminate the guardian.
                LOGGER.exception("调度扫描失败")
            remaining = max(0.0, interval - (time.monotonic() - started))
            if stopper is not None:
                stopper.wait(remaining)
            else:
                try:
                    time.sleep(remaining)
                except KeyboardInterrupt:
                    break


# Descriptive aliases and module-level entry points keep CLI integrations small.
ReminderScheduler = Scheduler
DeskGuardianScheduler = Scheduler


def run_scheduler(
    config: Optional[Mapping[str, Any]] = None,
    stop_event: Optional[threading.Event] = None,
    database: Optional[Any] = None,
    db: Optional[Any] = None,
    notifier: Optional[Any] = None,
    now: Optional[Any] = None,
    **_kwargs: Any,
) -> Scheduler:
    """Create and run a scheduler, returning it after the loop stops.

    When no database object is injected, the project's module-level ``db``
    facade is used.  This matches the CLI while tests can still inject a fake.
    """
    target = database if database is not None else db
    if target is None:
        try:
            import db as database_module
        except ImportError as exc:
            raise SchedulerError("无法加载数据库模块: %s" % exc) from exc
        target = database_module
    instance = Scheduler(target, config=config, notifier=notifier, now=now)
    instance.run(stop_event=stop_event)
    return instance


def scan_once(
    database: Any,
    config: Optional[Mapping[str, Any]] = None,
    notifier: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Convenience entry point for one deterministic scan."""
    return Scheduler(database, config=config, notifier=notifier, now=now).scan_once()


run = run_scheduler
scheduler_loop = run_scheduler


__all__ = [
    "DeskGuardianScheduler",
    "ReminderScheduler",
    "Scheduler",
    "SchedulerError",
    "run",
    "run_scheduler",
    "scan_once",
    "scheduler_loop",
]
