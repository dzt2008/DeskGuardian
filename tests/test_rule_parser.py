import pytest

import rule_parser


def test_parse_explicit_markdown_table_and_deduplicates_generic_pass():
    text = """
| 课程名 | 星期 | 节次 | 教室 |
| --- | --- | --- | --- |
| 高等数学 | 周一 | 第1-2节 | 教学楼A101 |
| 大学英语 | Friday | 3至4节 | B203 |
"""
    result = rule_parser.parse_with_rules(text)

    assert result["source"] == "rule"
    assert result["confidence"] > 0.6
    assert result["fallback_reason"] is None
    assert result["candidates"] == []
    assert result["courses"] == [
        {
            "name": "高等数学",
            "weekday": 1,
            "start_section": 1,
            "end_section": 2,
            "room": "教学楼A101",
        },
        {
            "name": "大学英语",
            "weekday": 5,
            "start_section": 3,
            "end_section": 4,
            "room": "B203",
        },
    ]


def test_parse_grid_table():
    text = """
| 节次 | 周一 | 周二 | 周三 |
| --- | --- | --- | --- |
| 1-2节 | 高等数学 教学楼A101 | - | 程序设计（B203） |
"""
    courses = rule_parser.extract_courses(text)

    assert [(item["name"], item["weekday"]) for item in courses] == [
        ("高等数学", 1),
        ("程序设计", 3),
    ]
    assert all(
        (item["start_section"], item["end_section"]) == (1, 2) for item in courses
    )
    assert courses[1]["room"] == "B203"


def test_parse_generic_chinese_and_english_lines():
    result = rule_parser.parse_timetable(
        "数据结构 周三 第5-6节 地点：实验楼A302\nPhysics Mon 7-8节 room: C204"
    )

    assert len(result["courses"]) == 2
    assert result["courses"][0] == {
        "name": "数据结构",
        "weekday": 3,
        "start_section": 5,
        "end_section": 6,
        "room": "实验楼A302",
    }
    assert result["courses"][1]["weekday"] == 1
    assert result["courses"][1]["start_section"] == 7


def test_ambiguous_text_returns_candidates_instead_of_guessing():
    result = rule_parser.parse_with_rules(
        "高等数学 教学楼101\n大学英语\n课程 星期 节次"
    )

    assert result["courses"] == []
    assert result["confidence"] == 0.0
    assert result["candidates"] == ["高等数学 教学楼101", "大学英语"]
    assert result["warnings"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("周日", 7), ("星期天", 7), ("Tue", 2), (6, 6), ("0", None), (True, None)],
)
def test_normalize_weekday(value, expected):
    assert rule_parser.normalize_weekday(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("第1-2节", (1, 2)), ("三至四节", (3, 4)), ([2, 4], (2, 4)), (8, (8, 8))],
)
def test_parse_sections_valid(value, expected):
    assert rule_parser.parse_sections(value) == expected


@pytest.mark.parametrize("value", ["第0节", "第8-7节", "第9节", True, None, []])
def test_parse_sections_rejects_invalid(value):
    assert rule_parser.parse_sections(value) is None


def test_normalize_course_record_accepts_aliases_and_rejects_invalid():
    assert rule_parser.normalize_course_record(
        {"课程名称": "  操作系统 ", "星期": "星期四", "节次": "1-2节", "地点": "A101"}
    ) == {
        "name": "操作系统",
        "weekday": 4,
        "start_section": 1,
        "end_section": 2,
        "room": "A101",
    }
    assert (
        rule_parser.normalize_course_record(
            {"name": "坏数据", "weekday": 8, "sections": "1节"}
        )
        is None
    )
    assert rule_parser.normalize_course_record("not a mapping") is None


def test_non_string_input_is_rejected():
    with pytest.raises(TypeError, match="string"):
        rule_parser.parse_with_rules(None)
