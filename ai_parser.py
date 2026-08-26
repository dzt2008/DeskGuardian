"""Ollama timetable parser and AI-to-rule fallback orchestration.

Only the Python standard library is used for the HTTP request so importing this
module does not require the optional ``ollama`` Python package.
"""

import json
import os
import re
import socket
from typing import Any, Callable, Dict, List, Optional
from urllib import error, request

from rule_parser import normalize_course_record, parse_with_rules


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:0.5b"
DEFAULT_TIMEOUT = 30.0
AI_CONFIDENCE_THRESHOLD = 0.6


class ParseError(RuntimeError):
    """An expected, user-facing failure while obtaining or decoding AI output."""


_SYSTEM_PROMPT = """你是课表结构化提取器。只提取输入中明确出现的信息，不猜测、不补全。
你必须只输出一个合法 JSON 对象，不要 Markdown，不要代码围栏，不要解释。
JSON 格式严格为：
{
  "courses": [
    {"name": "课程名", "weekday": 1, "start_section": 1, "end_section": 2, "room": "教室"}
  ],
  "confidence": 0.0,
  "warnings": []
}
约束：weekday 为 1(周一)到 7(周日)的整数；节次为 1 到 8 的整数且开始不大于结束；
room 不明确时使用空字符串；confidence 必须在 0 到 1 之间；无法确认的行不要放入 courses，
而是在 warnings 中用简短文字说明。"""


def _endpoint(base_url: str) -> str:
    value = (base_url or DEFAULT_OLLAMA_URL).strip().rstrip("/")
    if not value:
        value = DEFAULT_OLLAMA_URL
    if value.endswith("/api/chat") or value.endswith("/api/generate"):
        return value
    return value + "/api/chat"


def _post_json(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        suffix = "：" + detail[:300] if detail else ""
        raise ParseError(
            "Ollama HTTP 请求失败（状态码 {}）{}".format(exc.code, suffix)
        ) from exc
    except (
        error.URLError,
        ConnectionError,
        socket.timeout,
        TimeoutError,
        OSError,
    ) as exc:
        reason = getattr(exc, "reason", exc)
        raise ParseError(
            "无法连接本地 Ollama 服务（{}）：{}".format(url, reason)
        ) from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError("Ollama HTTP 响应不是有效 JSON：{}".format(exc.msg)) from exc
    if not isinstance(result, dict):
        raise ParseError("Ollama HTTP 响应格式错误：顶层必须是 JSON 对象")
    if result.get("error"):
        raise ParseError("Ollama 返回错误：{}".format(result["error"]))
    return result


def _response_content(response: Dict[str, Any]) -> str:
    message = response.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    generated = response.get("response")
    if isinstance(generated, str):
        return generated
    # A custom/mock transport may directly return the model's structured object.
    if "courses" in response:
        return json.dumps(response, ensure_ascii=False)
    raise ParseError("Ollama 响应缺少 message.content 或 response 字段")


def strip_code_fences(text: str) -> str:
    """Remove a surrounding Markdown JSON fence while preserving its content."""
    value = str(text or "").strip().lstrip("\ufeff")
    fenced = re.match(r"^```(?:json|JSON)?\s*([\s\S]*?)\s*```\s*$", value)
    if fenced:
        return fenced.group(1).strip()
    # Tolerate a model that emits only one side of a fence.
    value = re.sub(r"^```(?:json|JSON)?\s*", "", value)
    value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _decode_model_json(content: str) -> Any:
    cleaned = strip_code_fences(content)
    if not cleaned:
        raise ParseError("AI 返回内容为空")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as direct_error:
        # Some small models prepend one sentence despite the strict prompt. Find
        # the first independently decodable object/array, but never eval content.
        decoder = json.JSONDecoder()
        for index, char in enumerate(cleaned):
            if char not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(cleaned[index:])
                return value
            except json.JSONDecodeError:
                continue
        raise ParseError(
            "AI 输出不是有效 JSON：{}".format(direct_error.msg)
        ) from direct_error


def _confidence(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, str):
            text = value.strip()
            percent = text.endswith("%")
            number = float(text.rstrip("%"))
            if percent:
                number /= 100.0
        else:
            number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1.0 and number <= 100.0:
        number /= 100.0
    if not 0.0 <= number <= 1.0:
        return None
    return number


def _as_warning_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def normalize_ai_output(content: Any) -> Dict[str, Any]:
    """Validate and normalize raw model JSON into the public parse structure."""
    parsed = _decode_model_json(content) if isinstance(content, str) else content
    warnings: List[str] = []
    confidence_value: Any = None

    if isinstance(parsed, list):
        raw_courses = parsed
        warnings.append("AI 未提供总体置信度，已使用保守估值。")
    elif isinstance(parsed, dict):
        raw_courses = parsed.get("courses")
        if raw_courses is None:
            raw_courses = parsed.get("data", parsed.get("result"))
        if isinstance(raw_courses, dict):
            raw_courses = [raw_courses]
        confidence_value = parsed.get("confidence")
        warnings.extend(_as_warning_list(parsed.get("warnings")))
    else:
        raise ParseError("AI JSON 顶层必须是对象或课程数组")

    if not isinstance(raw_courses, list):
        raise ParseError("AI JSON 缺少 courses 数组")

    courses: List[Dict[str, Any]] = []
    seen = set()
    invalid_count = 0
    item_confidences: List[float] = []
    for raw_course in raw_courses:
        if isinstance(raw_course, dict):
            item_confidence = _confidence(raw_course.get("confidence"))
            if item_confidence is not None:
                item_confidences.append(item_confidence)
        course = normalize_course_record(raw_course)
        if not course:
            invalid_count += 1
            continue
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

    if invalid_count:
        warnings.append(
            "已忽略 {} 条字段缺失或范围无效的 AI 结果。".format(invalid_count)
        )
    score = _confidence(confidence_value)
    if confidence_value is not None and score is None:
        warnings.append("AI 置信度格式无效，已使用保守估值。")
    if score is None and item_confidences:
        score = sum(item_confidences) / len(item_confidences)
    if score is None:
        # Missing confidence must not be treated as highly trusted. A structurally
        # valid result can still pass the >0.6 gate, but only by a small margin.
        score = 0.65 if courses else 0.0
        if not any("置信度" in warning for warning in warnings):
            warnings.append("AI 未提供总体置信度，已使用保守估值。")
    if not courses:
        score = 0.0
        warnings.append("AI 未返回可用课程。")

    return {
        "source": "ai",
        "confidence": round(score, 4),
        "fallback_reason": None,
        "warnings": warnings,
        "courses": courses,
        "candidates": [],
    }


def call_ollama(
    text: str,
    model: Optional[str] = None,
    ollama_url: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: Optional[Callable[[str, Dict[str, Any], float], Dict[str, Any]]] = None,
) -> str:
    """Call Ollama's local HTTP API and return the assistant text."""
    if not isinstance(text, str) or not text.strip():
        raise ParseError("待解析课表文本不能为空")
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ParseError("Ollama 超时时间必须是正数") from exc
    if timeout_value <= 0:
        raise ParseError("Ollama 超时时间必须是正数")

    url = _endpoint(ollama_url or os.getenv("OLLAMA_URL", DEFAULT_OLLAMA_URL))
    model_name = (model or os.getenv("OLLAMA_MODEL", DEFAULT_MODEL)).strip()
    if not model_name:
        raise ParseError("Ollama 模型名称不能为空")
    payload: Dict[str, Any]
    if url.endswith("/api/generate"):
        payload = {
            "model": model_name,
            "prompt": _SYSTEM_PROMPT + "\n\n待解析课表文本：\n" + text,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
    else:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": "请解析以下课表文本：\n" + text},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
    sender = transport or _post_json
    try:
        response = sender(url, payload, timeout_value)
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("调用 Ollama 时发生异常：{}".format(exc)) from exc
    if not isinstance(response, dict):
        raise ParseError("Ollama HTTP 响应格式错误：顶层必须是 JSON 对象")
    return _response_content(response)


def parse_with_ai(
    text: str,
    model: Optional[str] = None,
    ollama_url: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: Optional[Callable[[str, Dict[str, Any], float], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Parse through Ollama, converting every expected failure to ParseError."""
    try:
        content = call_ollama(text, model, ollama_url, timeout, transport)
        return normalize_ai_output(content)
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("AI 解析失败：{}".format(exc)) from exc


def parse_timetable(
    text: str,
    use_ai: bool = True,
    confidence_threshold: float = AI_CONFIDENCE_THRESHOLD,
    model: Optional[str] = None,
    ollama_url: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: Optional[Callable[[str, Dict[str, Any], float], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Try AI first and automatically fall back to conservative rules.

    The returned dictionary always contains ``source``, ``confidence``,
    ``fallback_reason``, ``warnings``, ``courses`` and ``candidates``.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not text.strip():
        result = parse_with_rules(text)
        result["fallback_reason"] = "输入文本为空"
        return result
    try:
        threshold = float(confidence_threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "confidence_threshold must be a number between 0 and 1"
        ) from exc
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("confidence_threshold must be between 0 and 1")

    reason: Optional[str] = None
    if use_ai:
        try:
            ai_result = parse_with_ai(text, model, ollama_url, timeout, transport)
            if ai_result["courses"] and ai_result["confidence"] > threshold:
                return ai_result
            reason = "AI 置信度 {:.2f} 未超过阈值 {:.2f}".format(
                ai_result["confidence"], threshold
            )
            if not ai_result["courses"]:
                reason = "AI 未返回可用课程"
        except ParseError as exc:
            reason = str(exc)
    else:
        reason = "已禁用 AI 解析"

    rule_result = parse_with_rules(text)
    rule_result["fallback_reason"] = reason
    if reason:
        rule_result["warnings"] = ["已降级到规则解析：" + reason] + rule_result[
            "warnings"
        ]
    return rule_result


# Common aliases for CLI and Flask integrations.
parse = parse_timetable
parse_courses = parse_timetable
parse_ai = parse_with_ai


__all__ = [
    "AI_CONFIDENCE_THRESHOLD",
    "DEFAULT_MODEL",
    "DEFAULT_OLLAMA_URL",
    "ParseError",
    "call_ollama",
    "normalize_ai_output",
    "parse",
    "parse_ai",
    "parse_courses",
    "parse_timetable",
    "parse_with_ai",
    "strip_code_fences",
]
