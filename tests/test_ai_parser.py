import json

import pytest

import ai_parser


def _course_payload(confidence=0.91):
    return {
        "courses": [
            {
                "name": "高等数学",
                "weekday": "周一",
                "start_section": 1,
                "end_section": 2,
                "room": "A101",
            }
        ],
        "confidence": confidence,
        "warnings": [],
    }


def test_parse_with_ai_uses_injected_transport_without_network():
    calls = []

    def fake_transport(url, payload, timeout):
        calls.append((url, payload, timeout))
        return {
            "message": {"content": json.dumps(_course_payload(), ensure_ascii=False)}
        }

    result = ai_parser.parse_with_ai(
        "高等数学 周一 1-2节 A101",
        model="test-model",
        ollama_url="http://127.0.0.1:9999",
        timeout=1.5,
        transport=fake_transport,
    )

    assert result["source"] == "ai"
    assert result["confidence"] == 0.91
    assert result["courses"][0]["weekday"] == 1
    assert calls[0][0] == "http://127.0.0.1:9999/api/chat"
    assert calls[0][1]["model"] == "test-model"
    assert calls[0][1]["stream"] is False
    assert calls[0][2] == 1.5


def test_generate_endpoint_builds_generate_payload():
    captured = {}

    def fake_transport(url, payload, timeout):
        captured.update(url=url, payload=payload, timeout=timeout)
        return {"response": json.dumps(_course_payload())}

    ai_parser.call_ollama(
        "course text",
        ollama_url="http://localhost:11434/api/generate",
        transport=fake_transport,
    )

    assert captured["url"].endswith("/api/generate")
    assert "prompt" in captured["payload"]
    assert "messages" not in captured["payload"]


def test_normalize_fenced_json_drops_invalid_and_duplicates():
    payload = _course_payload(confidence="92%")
    payload["courses"] += [
        payload["courses"][0],
        {"name": "坏数据", "weekday": 9, "sections": "1节"},
    ]
    content = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"

    result = ai_parser.normalize_ai_output(content)

    assert result["confidence"] == 0.92
    assert len(result["courses"]) == 1
    assert any("忽略 1 条" in warning for warning in result["warnings"])


def test_normalize_accepts_leading_model_comment_and_course_list():
    result = ai_parser.normalize_ai_output(
        "这是结果：\n"
        + json.dumps([_course_payload()["courses"][0]], ensure_ascii=False)
    )

    assert result["source"] == "ai"
    assert result["confidence"] == 0.65
    assert result["courses"][0]["name"] == "高等数学"
    assert any("置信度" in warning for warning in result["warnings"])


@pytest.mark.parametrize("content", ["", "not json", "{}", "42", '{"courses": "bad"}'])
def test_invalid_ai_output_raises_parse_error(content):
    with pytest.raises(ai_parser.ParseError):
        ai_parser.normalize_ai_output(content)


def test_high_confidence_ai_result_does_not_fall_back():
    def fake_transport(url, payload, timeout):
        return {"courses": _course_payload(0.8)["courses"], "confidence": 0.8}

    result = ai_parser.parse_timetable(
        "高等数学 周一 第1-2节 A101", transport=fake_transport
    )

    assert result["source"] == "ai"
    assert result["fallback_reason"] is None


def test_low_confidence_ai_falls_back_to_rules():
    def fake_transport(url, payload, timeout):
        return {"courses": _course_payload(0.6)["courses"], "confidence": 0.6}

    result = ai_parser.parse_timetable(
        "高等数学 周一 第1-2节 教室：A101",
        confidence_threshold=0.6,
        transport=fake_transport,
    )

    assert result["source"] == "rule"
    assert result["courses"]
    assert "未超过阈值" in result["fallback_reason"]
    assert result["warnings"][0].startswith("已降级到规则解析")


def test_transport_exception_falls_back_and_never_calls_real_ollama():
    def failing_transport(url, payload, timeout):
        raise OSError("offline in test")

    result = ai_parser.parse_timetable(
        "数据结构 周三 第3-4节 教室：B201", transport=failing_transport
    )

    assert result["source"] == "rule"
    assert result["courses"][0]["name"] == "数据结构"
    assert "offline in test" in result["fallback_reason"]


def test_ai_can_be_explicitly_disabled():
    def forbidden_transport(*args):
        pytest.fail("transport should not be called when AI is disabled")

    result = ai_parser.parse_timetable(
        "数据结构 周三 第3-4节 B201", use_ai=False, transport=forbidden_transport
    )
    assert result["source"] == "rule"
    assert result["fallback_reason"] == "已禁用 AI 解析"


def test_empty_input_does_not_call_transport():
    def forbidden_transport(*args):
        pytest.fail("empty input must not call Ollama")

    result = ai_parser.parse_timetable("  ", transport=forbidden_transport)
    assert result["courses"] == []
    assert result["fallback_reason"] == "输入文本为空"


def test_call_ollama_validates_input_before_transport():
    with pytest.raises(ai_parser.ParseError, match="不能为空"):
        ai_parser.call_ollama("", transport=lambda *args: {})
    with pytest.raises(ai_parser.ParseError, match="正数"):
        ai_parser.call_ollama("text", timeout=0, transport=lambda *args: {})
