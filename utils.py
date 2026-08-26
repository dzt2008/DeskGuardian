"""Shared validation, serialization and date/time helpers."""

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple, Union


WEEKDAY_NAMES = {
    1: "星期一",
    2: "星期二",
    3: "星期三",
    4: "星期四",
    5: "星期五",
    6: "星期六",
    7: "星期日",
}
WEEKDAY_ALIASES = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    "天": 7,
    "周一": 1,
    "周二": 2,
    "周三": 3,
    "周四": 4,
    "周五": 5,
    "周六": 6,
    "周日": 7,
    "周天": 7,
    "星期一": 1,
    "星期二": 2,
    "星期三": 3,
    "星期四": 4,
    "星期五": 5,
    "星期六": 6,
    "星期日": 7,
    "星期天": 7,
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
    "sunday": 7,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
    "sun": 7,
}


def require_text(value: Any, field: str, max_length: int = 500) -> str:
    if not isinstance(value, str):
        raise ValueError("%s 必须是文本" % field)
    value = value.strip()
    if not value:
        raise ValueError("%s 不能为空" % field)
    if len(value) > max_length:
        raise ValueError("%s 不能超过 %d 个字符" % (field, max_length))
    return value


def optional_text(
    value: Any, field: str, max_length: int = 500, default: str = ""
) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("%s 必须是文本" % field)
    value = value.strip()
    if len(value) > max_length:
        raise ValueError("%s 不能超过 %d 个字符" % (field, max_length))
    return value


def parse_bool(value: Any, field: str = "值") -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on", "是", "启用"):
            return True
        if normalized in ("0", "false", "no", "off", "否", "禁用"):
            return False
    raise ValueError("%s 必须是布尔值" % field)


def parse_int(
    value: Any, field: str, minimum: Optional[int] = None, maximum: Optional[int] = None
) -> int:
    if isinstance(value, bool):
        raise ValueError("%s 必须是整数" % field)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s 必须是整数" % field)
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("%s 必须是整数" % field)
    if minimum is not None and parsed < minimum:
        raise ValueError("%s 不能小于 %d" % (field, minimum))
    if maximum is not None and parsed > maximum:
        raise ValueError("%s 不能大于 %d" % (field, maximum))
    return parsed


def normalize_weekday(value: Any) -> int:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            value = int(stripped)
        else:
            found = WEEKDAY_ALIASES.get(stripped.lower())
            if found is None:
                raise ValueError("weekday 必须为 1~7 或有效星期名称")
            return found
    return parse_int(value, "weekday", 1, 7)


def weekday_name(value: Any) -> str:
    return WEEKDAY_NAMES[normalize_weekday(value)]


def validate_sections(start_section: Any, end_section: Any = None) -> Tuple[int, int]:
    start = parse_int(start_section, "start_section", 1, 8)
    end = (
        start
        if end_section in (None, "")
        else parse_int(end_section, "end_section", 1, 8)
    )
    if end < start:
        raise ValueError("end_section 不能小于 start_section")
    return start, end


def parse_section_range(value: Any) -> Tuple[int, int]:
    """Parse 1, '1-2', '第1至2节', or a two-item sequence."""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 2:
            raise ValueError("节次序列必须包含开始和结束节次")
        return validate_sections(value[0], value[1])
    if isinstance(value, int):
        return validate_sections(value)
    if not isinstance(value, str):
        raise ValueError("节次格式无效")
    numbers = re.findall(r"\d+", value)
    if not numbers or len(numbers) > 2:
        raise ValueError("节次格式无效")
    return validate_sections(numbers[0], numbers[-1])


def parse_iso_datetime(value: Any, field: str = "deadline") -> datetime:
    """Parse an ISO date/datetime.

    This helper is retained for event timestamps. Homework deadlines should use
    :func:`parse_deadline_date` / :func:`normalize_deadline`, which deliberately
    store date-only values.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time(23, 59, 59))
    text = require_text(value, field, 64)
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
            # A date-only homework remains valid throughout that calendar day.
            # Storage is still date-only via normalize_deadline().
            return datetime.combine(date.fromisoformat(normalized), time(23, 59, 59))
        return datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError("%s 必须是 ISO 日期或日期时间" % field)


def parse_deadline_date(
    value: Any, relative_base: Optional[Union[date, datetime]] = None
) -> date:
    """Parse a homework deadline into a calendar date.

    ``dateparser`` is used when installed, enabling Chinese natural-language
    input. The dependency-free fallback supports ISO dates/datetimes and the
    common Chinese expressions 今天/明天/后天 (plus today/tomorrow).
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = require_text(value, "deadline", 128)
    base_dt = (
        relative_base
        if isinstance(relative_base, datetime)
        else datetime.combine(relative_base or date.today(), time.min)
    )
    relative_days = {
        "今天": 0,
        "今日": 0,
        "today": 0,
        "明天": 1,
        "明日": 1,
        "tomorrow": 1,
        "后天": 2,
    }
    normalized_relative = text.strip().lower()
    if normalized_relative in relative_days:
        return base_dt.date() + timedelta(days=relative_days[normalized_relative])

    # Accept date-only ISO as the canonical and fastest path. ISO datetimes are
    # accepted for import compatibility but intentionally truncated to a date.
    try:
        return date.fromisoformat(text)
    except ValueError:
        try:
            iso_text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
            return datetime.fromisoformat(iso_text).date()
        except ValueError:
            pass

    try:
        import dateparser  # type: ignore
    except ImportError:
        raise ValueError(
            "deadline 必须为 YYYY-MM-DD，或今天/明天/后天（安装 dateparser 可支持更多自然语言）"
        )
    parsed = dateparser.parse(
        text,
        languages=["zh", "en"],
        settings={
            "RELATIVE_BASE": base_dt.replace(tzinfo=None),
            "PREFER_DATES_FROM": "future",
            "DATE_ORDER": "YMD",
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    if parsed is None:
        raise ValueError("无法识别 deadline: %s" % text)
    return parsed.date()


def normalize_deadline(
    value: Any, relative_base: Optional[Union[date, datetime]] = None
) -> str:
    """Return the canonical homework deadline format: ``YYYY-MM-DD``."""
    return parse_deadline_date(value, relative_base).isoformat()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_now() -> datetime:
    return datetime.now().astimezone()


def combine_date_time(day: date, hhmm: str) -> datetime:
    try:
        parsed_time = datetime.strptime(hhmm, "%H:%M").time()
    except (TypeError, ValueError):
        raise ValueError("时间必须为 HH:MM")
    return datetime.combine(day, parsed_time).astimezone()


def course_start_datetime(
    course: Mapping[str, Any],
    section_times: Mapping[str, Any],
    day: Optional[date] = None,
) -> datetime:
    day = day or date.today()
    section = str(parse_int(course.get("start_section"), "start_section", 1, 8))
    item = section_times.get(section)
    if not isinstance(item, Mapping) or "start" not in item:
        raise ValueError("缺少第 %s 节时间配置" % section)
    return combine_date_time(day, item["start"])


def json_dumps(data: Any, pretty: bool = False) -> str:
    return json.dumps(
        data,
        ensure_ascii=False,
        indent=2 if pretty else None,
        sort_keys=pretty,
        default=_json_default,
    )


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    raise TypeError("对象不可 JSON 序列化: %s" % type(value).__name__)


def chunks(items: Iterable[Any], size: int) -> Iterable[Tuple[Any, ...]]:
    if size < 1:
        raise ValueError("size 必须大于 0")
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield tuple(batch)
            batch = []
    if batch:
        yield tuple(batch)
