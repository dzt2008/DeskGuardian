"""Conservative rule based timetable parser.

The parser intentionally only emits a course when both a weekday and a section can
be identified.  Ambiguous input is left for the preview/manual-selection stage
instead of being guessed.
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_WEEKDAY_NAMES = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    "天": 7,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
}
_ENGLISH_WEEKDAYS = {
    "monday": 1,
    "mon": 1,
    "tuesday": 2,
    "tue": 2,
    "tues": 2,
    "wednesday": 3,
    "wed": 3,
    "thursday": 4,
    "thu": 4,
    "thur": 4,
    "thurs": 4,
    "friday": 5,
    "fri": 5,
    "saturday": 6,
    "sat": 6,
    "sunday": 7,
    "sun": 7,
}
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8}

_DAY_RE = re.compile(
    r"(?:星期|周|礼拜)\s*([一二三四五六日天1-7])"
    r"|\b(Monday|Mon|Tuesday|Tue(?:s)?|Wednesday|Wed|Thursday|Thu(?:r|rs)?|Friday|Fri|Saturday|Sat|Sunday|Sun)\b",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(
    r"(?:第\s*)?([1-8一二三四五六七八])\s*"
    r"(?:[-~～—–至到、,，/]\s*([1-8一二三四五六七八]))?\s*(?:节|课时)",
    re.IGNORECASE,
)
_PLAIN_SECTION_RE = re.compile(
    r"^\s*(?:第\s*)?([1-8一二三四五六七八])\s*"
    r"(?:[-~～—–至到、,，/]\s*([1-8一二三四五六七八]))?\s*(?:节|课时)?\s*$",
    re.IGNORECASE,
)
_ROOM_LABEL_RE = re.compile(
    r"(?:教室|地点|上课地点|课室)\s*[:：]?\s*([^|\t,，;；]+)", re.IGNORECASE
)
_ROOM_BUILDING_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9]{1,12}(?:楼|馆|中心|校区|实验室)\s*[-－]?[A-Za-z]?\d{0,4}(?:室)?)"
)
_ROOM_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{1,4}\s*[-－]?\s*\d{2,4})(?![A-Za-z0-9])"
)
_SEPARATOR_RE = re.compile(r"^\s*:?-{3,}:?\s*$")
_HEADER_WORDS = {
    "课程",
    "课程名",
    "课程名称",
    "科目",
    "星期",
    "周次",
    "节次",
    "时间",
    "教室",
    "地点",
    "上课地点",
}


def _digit(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if text.isdigit():
        return int(text)
    return None


def normalize_weekday(value: Any) -> Optional[int]:
    """Convert a Chinese/English weekday representation to ISO weekday 1..7."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and int(value) == value:
        number = int(value)
        return number if 1 <= number <= 7 else None
    text = str(value or "").strip()
    if text.isdigit():
        number = int(text)
        return number if 1 <= number <= 7 else None
    match = _DAY_RE.search(text)
    if match:
        if match.group(1):
            return _WEEKDAY_NAMES.get(match.group(1))
        return _ENGLISH_WEEKDAYS.get(match.group(2).lower())
    return _ENGLISH_WEEKDAYS.get(text.lower().rstrip("."))


def parse_sections(value: Any, allow_plain: bool = True) -> Optional[Tuple[int, int]]:
    """Parse common section forms, e.g. ``第1-2节`` or ``三至四节``."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return (value, value) if 1 <= value <= 8 else None
    if isinstance(value, (list, tuple)) and value:
        numbers = [_digit(item) for item in value]
        numbers = [number for number in numbers if number is not None]
        if numbers and all(1 <= number <= 8 for number in numbers):
            return min(numbers), max(numbers)
        return None
    text = str(value).strip()
    match = _SECTION_RE.search(text)
    if not match and allow_plain:
        match = _PLAIN_SECTION_RE.match(text)
    if not match:
        return None
    start = _digit(match.group(1))
    end = _digit(match.group(2)) if match.group(2) else start
    if start is None or end is None or not (1 <= start <= end <= 8):
        return None
    return start, end


def normalize_course_record(record: Any) -> Optional[Dict[str, Any]]:
    """Normalize one AI/rule record to the database-facing course schema."""
    if not isinstance(record, dict):
        return None

    def first(keys: Sequence[str], default: Any = None) -> Any:
        for key in keys:
            if key in record and record[key] not in (None, ""):
                return record[key]
        return default

    name = str(
        first(
            ("name", "course", "course_name", "subject", "课程", "课程名", "课程名称"),
            "",
        )
    ).strip()
    weekday = normalize_weekday(first(("weekday", "day", "week_day", "星期", "周")))
    section_value = first(("sections", "section", "period", "节次", "课时"))
    sections = parse_sections(section_value) if section_value is not None else None
    start = _digit(first(("start_section", "start", "section_start", "开始节次")))
    end = _digit(first(("end_section", "end", "section_end", "结束节次")))
    if sections:
        start = start or sections[0]
        end = end or sections[1]
    if start is not None and end is None:
        end = start
    room = str(first(("room", "classroom", "location", "教室", "地点"), "")).strip()
    name = re.sub(r"^[\s|,，;；:：\-–—]+|[\s|,，;；:：\-–—]+$", "", name)
    if not name or weekday is None or start is None or end is None:
        return None
    if not (1 <= start <= end <= 8) or _looks_like_header(name):
        return None
    return {
        "name": name,
        "weekday": weekday,
        "start_section": start,
        "end_section": end,
        "room": room,
    }


def _looks_like_header(text: str) -> bool:
    compact = re.sub(r"[\s|:：]", "", text).lower()
    return compact in _HEADER_WORDS or compact in {
        "name",
        "course",
        "subject",
        "weekday",
        "day",
        "room",
        "location",
    }


def _split_table_line(line: str) -> Optional[List[str]]:
    if "\t" in line:
        return [part.strip() for part in line.split("\t")]
    if "|" in line:
        text = line.strip()
        if text.startswith("|"):
            text = text[1:]
        if text.endswith("|"):
            text = text[:-1]
        return [part.strip() for part in text.split("|")]
    return None


def _is_separator_cells(cells: Sequence[str]) -> bool:
    return bool(cells) and all(not cell or _SEPARATOR_RE.match(cell) for cell in cells)


def _header_kind(cell: str) -> Optional[str]:
    value = re.sub(r"\s+", "", cell).lower()
    if value in {"课程", "课程名", "课程名称", "科目", "name", "course", "subject"}:
        return "name"
    if value in {"星期", "周", "周几", "weekday", "day"}:
        return "weekday"
    if value in {"节次", "课时", "时间", "section", "sections", "period"}:
        return "sections"
    if value in {"教室", "课室", "地点", "上课地点", "room", "location"}:
        return "room"
    return None


def _extract_room(text: str, allow_bare_number: bool = False) -> Tuple[str, str]:
    """Return ``(room, text_without_room)``."""
    label = _ROOM_LABEL_RE.search(text)
    if label:
        room = label.group(1).strip()
        return room, (text[: label.start()] + " " + text[label.end() :]).strip()
    matches = list(_ROOM_BUILDING_RE.finditer(text))
    if not matches:
        matches = list(_ROOM_CODE_RE.finditer(text))
    if matches:
        match = matches[-1]
        room = match.group(1).strip()
        return room, (text[: match.start()] + " " + text[match.end() :]).strip()
    if allow_bare_number and re.fullmatch(r"\s*\d{2,4}(?:室)?\s*", text):
        return text.strip(), ""
    return "", text


def _clean_name(text: str) -> str:
    text = re.sub(r"^\s*(?:\d+\s*[.、)])\s*", "", text)
    text = re.sub(r"(?:课程(?:名称|名)?|科目)\s*[:：]", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?:教室|地点|上课地点|课室)\s*[:：]?\s*$", "", text, flags=re.IGNORECASE
    )
    # Room extraction may leave an empty pair of Chinese/ASCII parentheses.
    text = re.sub(r"[（(]\s*[）)]", " ", text)
    text = re.sub(r"[|\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" ,，;；:：-–—|\t")


def _parse_generic_line(line: str) -> Optional[Dict[str, Any]]:
    day_match = _DAY_RE.search(line)
    section_match = _SECTION_RE.search(line)
    if not day_match or not section_match:
        return None
    weekday = normalize_weekday(day_match.group(0))
    sections = parse_sections(section_match.group(0), allow_plain=False)
    if weekday is None or sections is None:
        return None
    remainder = line
    # Remove from right to left so the recorded spans stay valid.
    for match in sorted(
        (day_match, section_match), key=lambda item: item.start(), reverse=True
    ):
        remainder = remainder[: match.start()] + " " + remainder[match.end() :]
    room, remainder = _extract_room(remainder)
    name = _clean_name(remainder)
    return normalize_course_record(
        {
            "name": name,
            "weekday": weekday,
            "start_section": sections[0],
            "end_section": sections[1],
            "room": room,
        }
    )


def _parse_explicit_tables(lines: Sequence[str]) -> Iterable[Dict[str, Any]]:
    header: Optional[Dict[str, int]] = None
    for line in lines:
        cells = _split_table_line(line)
        if not cells or _is_separator_cells(cells):
            continue
        kinds = [_header_kind(cell) for cell in cells]
        if "name" in kinds and ("weekday" in kinds or "sections" in kinds):
            header = {kind: index for index, kind in enumerate(kinds) if kind}
            continue
        if not header:
            continue
        try:
            raw = {
                kind: cells[index]
                for kind, index in header.items()
                if index < len(cells)
            }
        except (IndexError, TypeError):
            continue
        course = normalize_course_record(raw)
        if course:
            yield course


def _course_from_grid_cell(
    cell: str, weekday: int, sections: Tuple[int, int]
) -> Optional[Dict[str, Any]]:
    value = cell.strip()
    if not value or value in {"-", "—", "/", "无"}:
        return None
    room, name = _extract_room(value)
    # A common grid cell separates the room with whitespace or parentheses.
    if not room:
        parenthesized = re.search(r"[（(]([^()（）]{2,20})[）)]\s*$", name)
        if parenthesized and (
            _ROOM_CODE_RE.search(parenthesized.group(1))
            or "楼" in parenthesized.group(1)
            or "室" in parenthesized.group(1)
        ):
            room = parenthesized.group(1).strip()
            name = name[: parenthesized.start()]
    return normalize_course_record(
        {
            "name": _clean_name(name),
            "weekday": weekday,
            "start_section": sections[0],
            "end_section": sections[1],
            "room": room,
        }
    )


def _parse_grid_tables(lines: Sequence[str]) -> Iterable[Dict[str, Any]]:
    weekdays: Optional[List[Optional[int]]] = None
    section_column = 0
    for line in lines:
        cells = _split_table_line(line)
        if not cells or _is_separator_cells(cells):
            continue
        detected = [normalize_weekday(cell) for cell in cells]
        if sum(day is not None for day in detected) >= 1 and any(
            _header_kind(cell) == "sections" for cell in cells
        ):
            weekdays = detected
            section_column = next(
                (
                    index
                    for index, cell in enumerate(cells)
                    if _header_kind(cell) == "sections"
                ),
                0,
            )
            continue
        if weekdays is None or section_column >= len(cells):
            continue
        sections = parse_sections(cells[section_column], allow_plain=True)
        if not sections:
            continue
        for index, weekday in enumerate(weekdays):
            if weekday is None or index >= len(cells):
                continue
            course = _course_from_grid_cell(cells[index], weekday, sections)
            if course:
                yield course


def _candidate_lines(text: str) -> List[str]:
    candidates: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or len(line) > 500:
            continue
        cells = _split_table_line(line)
        if cells and _is_separator_cells(cells):
            continue
        compact = re.sub(r"[\s|:：]", "", line)
        header_tokens = [token for token in re.split(r"[\s|,，;；]+", line) if token]
        if (
            not compact
            or _looks_like_header(compact)
            or (header_tokens and all(_header_kind(token) for token in header_tokens))
        ):
            continue
        candidates.append(line)
    return candidates[:100]


def parse_with_rules(text: str) -> Dict[str, Any]:
    """Parse timetable text and return the common ``/api/parse`` structure."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line for line in normalized_text.split("\n") if line.strip()]
    courses: List[Dict[str, Any]] = []
    seen = set()

    def add(course: Optional[Dict[str, Any]]) -> None:
        if not course:
            return
        signature = (
            course["name"].casefold(),
            course["weekday"],
            course["start_section"],
            course["end_section"],
            course["room"].casefold(),
        )
        if signature not in seen:
            seen.add(signature)
            courses.append(course)

    for course in _parse_explicit_tables(lines):
        add(course)
    for course in _parse_grid_tables(lines):
        add(course)
    for line in lines:
        add(_parse_generic_line(line))

    warnings: List[str] = []
    if courses:
        confidence = min(0.88, 0.68 + min(len(courses), 5) * 0.03)
        candidates: List[str] = []
    else:
        confidence = 0.0
        candidates = _candidate_lines(normalized_text)
        warnings.append(
            "规则解析未识别出同时包含星期和节次的课程，请从候选行中手工确认。"
        )
    return {
        "source": "rule",
        "confidence": round(confidence, 2),
        "fallback_reason": None,
        "warnings": warnings,
        "courses": courses,
        "candidates": candidates,
    }


# Stable, descriptive aliases used by CLI/Web callers.
parse_rule = parse_with_rules
parse_timetable = parse_with_rules
parse_courses = parse_with_rules


def extract_courses(text: str) -> List[Dict[str, Any]]:
    """Compatibility helper for callers that only need the list."""
    return parse_with_rules(text)["courses"]


__all__ = [
    "extract_courses",
    "normalize_course_record",
    "normalize_weekday",
    "parse_courses",
    "parse_rule",
    "parse_sections",
    "parse_timetable",
    "parse_with_rules",
]
