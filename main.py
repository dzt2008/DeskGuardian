#!/usr/bin/env python3
"""DeskGuardian 命令行入口。

本模块只负责用户交互与各业务模块的编排；数据库、Web 服务和调度器均
通过它们各自的公共接口工作。导入本模块不会启动服务或创建文件。
"""

import argparse
import inspect
import json
import os
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


APP_DESCRIPTION = "DeskGuardian —— 本地课业守护助手"

try:
    from version import VERSION, __version__
except ImportError:  # pragma: no cover - package/frozen fallback
    VERSION = __version__ = "0.1.0"

try:
    from logging_setup import setup_logging
except ImportError:  # pragma: no cover
    setup_logging = None
_WEEKDAYS = ("", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_DATABASE = None


class UserError(RuntimeError):
    """可以直接展示给用户、无需打印回溯的错误。"""


def _configure_console() -> None:
    """尽量让 Windows 控制台正确显示中文。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _call_compatible(function: Any, *args: Any, **kwargs: Any) -> Any:
    """按函数签名过滤可选参数，兼容模块级函数和轻量门面对象。"""
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*args, **kwargs)
    parameters = signature.parameters
    if any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        accepted = kwargs
    else:
        accepted = {key: value for key, value in kwargs.items() if key in parameters}
    return function(*args, **accepted)


def _find_callable(target: Any, names: Sequence[str]) -> Optional[Any]:
    for name in names:
        candidate = getattr(target, name, None)
        if callable(candidate):
            return candidate
    return None


def _initialize() -> Tuple[Dict[str, Any], str]:
    """初始化配置与数据库，并返回配置及数据库路径。"""
    global _DATABASE
    from config import get_db_path, load_config

    config = load_config()
    db_path = get_db_path()
    try:
        import db as database_module
    except ImportError as exc:
        raise UserError("数据库模块不可用：%s" % exc) from exc

    initializer = _find_callable(
        database_module, ("init_db", "initialize_db", "initialize")
    )
    if initializer is not None:
        try:
            result = _call_compatible(
                initializer,
                db_path=db_path,
                database_path=db_path,
                path=db_path,
            )
            if result is not None:
                _DATABASE = result
        except TypeError:
            # 常见公共契约为 init_db(path)。只有签名要求位置参数时才重试。
            try:
                result = initializer(db_path)
                if result is not None:
                    _DATABASE = result
            except TypeError:
                result = initializer()
                if result is not None:
                    _DATABASE = result
    if _DATABASE is None:
        database_class = getattr(database_module, "Database", None)
        if callable(database_class):
            _DATABASE = _call_compatible(
                database_class,
                db_path=db_path,
                database_path=db_path,
                path=db_path,
            )
    if _DATABASE is None:
        _DATABASE = database_module
    return config, db_path


def _db_function(names: Sequence[str]) -> Any:
    if _DATABASE is None:
        _initialize()
    function = _find_callable(_DATABASE, names)
    if function is None:
        raise UserError("当前数据库模块缺少接口：%s" % "/".join(names))
    return function


def _as_dict(row: Any) -> Dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        return {key: row[key] for key in keys()}
    if hasattr(row, "__dict__"):
        return {
            key: value for key, value in vars(row).items() if not key.startswith("_")
        }
    raise UserError("数据库返回了无法识别的数据行")


def _unwrap_rows(value: Any, keys: Sequence[str]) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        for key in keys:
            if key in value:
                value = value[key]
                break
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise UserError("数据库返回的数据格式无效")
    return [_as_dict(item) for item in value]


def _list_courses() -> List[Dict[str, Any]]:
    result = _call_compatible(
        _db_function(("list_courses", "get_courses", "get_all_courses"))
    )
    return _unwrap_rows(result, ("courses", "items", "data"))


def _list_homeworks() -> List[Dict[str, Any]]:
    result = _call_compatible(
        _db_function(("list_homeworks", "get_homeworks", "get_all_homeworks"))
    )
    return _unwrap_rows(result, ("homeworks", "items", "data"))


def _insert_course(course: Mapping[str, Any]) -> Any:
    function = _db_function(("add_course", "create_course", "insert_course"))
    payload = dict(course)
    try:
        return function(payload)
    except TypeError:
        return _call_compatible(function, **payload)


def _insert_courses(courses: Sequence[Mapping[str, Any]]) -> int:
    function = _find_callable(
        _DATABASE,
        ("add_courses", "create_courses", "bulk_add_courses", "import_courses"),
    )
    if function is not None:
        result = function([dict(course) for course in courses])
        if isinstance(result, int):
            return result
        if isinstance(result, Mapping):
            return int(result.get("count", result.get("imported", len(courses))))
        return len(courses)
    for course in courses:
        _insert_course(course)
    return len(courses)


def _insert_homework(homework: Mapping[str, Any]) -> Any:
    function = _db_function(("add_homework", "create_homework", "insert_homework"))
    payload = dict(homework)
    try:
        return function(payload)
    except TypeError:
        return _call_compatible(function, **payload)


def _delete_course(course_id: int) -> None:
    _db_function(("delete_course", "remove_course"))(course_id)


def _prompt(prompt: str, default: Optional[str] = None) -> str:
    suffix = " [%s]" % default if default not in (None, "") else ""
    try:
        value = input("%s%s：" % (prompt, suffix)).strip()
    except EOFError as exc:
        raise UserError("输入已结束，操作取消") from exc
    return value if value else (default or "")


def _confirm(prompt: str, default: bool = False) -> bool:
    marker = "Y/n" if default else "y/N"
    value = _prompt("%s (%s)" % (prompt, marker)).lower()
    if not value:
        return default
    if value in ("y", "yes", "是", "确认", "确定"):
        return True
    if value in ("n", "no", "否", "取消"):
        return False
    print("请输入 y 或 n。")
    return _confirm(prompt, default)


def _parse_int_input(value: str, field: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise UserError("%s 必须是 %d~%d 的整数" % (field, minimum, maximum)) from exc
    if not minimum <= number <= maximum:
        raise UserError("%s 必须在 %d~%d 之间" % (field, minimum, maximum))
    return number


def _manual_course(existing: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    old = dict(existing or {})
    name = _prompt("课程名", str(old.get("name", "")))
    if not name:
        raise UserError("课程名不能为空")
    weekday = _parse_int_input(
        _prompt("星期（1=周一，7=周日）", str(old.get("weekday", 1))), "星期", 1, 7
    )
    start = _parse_int_input(
        _prompt("开始节次", str(old.get("start_section", 1))), "开始节次", 1, 8
    )
    end = _parse_int_input(
        _prompt("结束节次", str(old.get("end_section", start))), "结束节次", 1, 8
    )
    if end < start:
        raise UserError("结束节次不能早于开始节次")
    room = _prompt("教室（可空）", str(old.get("room", "")))
    return {
        "name": name,
        "weekday": weekday,
        "start_section": start,
        "end_section": end,
        "room": room,
        "is_active": bool(old.get("is_active", True)),
    }


def _manual_courses() -> List[Dict[str, Any]]:
    courses: List[Dict[str, Any]] = []
    print("进入手工录入。")
    while True:
        try:
            courses.append(_manual_course())
        except UserError as exc:
            print("录入失败：%s" % exc)
            if not _confirm("重试这一条", True):
                break
            continue
        if not _confirm("继续录入下一门课程", False):
            break
    return courses


def _course_rows(courses: Sequence[Mapping[str, Any]]) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for index, item in enumerate(courses, 1):
        weekday = item.get("weekday", "")
        try:
            weekday_text = _WEEKDAYS[int(weekday)]
        except (ValueError, TypeError, IndexError):
            weekday_text = str(weekday)
        rows.append(
            [
                index,
                item.get("name", ""),
                weekday_text,
                "%s-%s" % (item.get("start_section", ""), item.get("end_section", "")),
                item.get("room", ""),
            ]
        )
    return rows


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    text_rows = [
        [str(value if value is not None else "") for value in row] for row in rows
    ]
    widths = [len(header) for header in headers]
    for row in text_rows:
        for index, value in enumerate(row[: len(widths)]):
            # 中文字符在多数终端占两格；这是简洁且足够稳定的近似。
            widths[index] = max(
                widths[index], sum(2 if ord(char) > 127 else 1 for char in value)
            )

    def pad(value: str, width: int) -> str:
        display = sum(2 if ord(char) > 127 else 1 for char in value)
        return value + " " * max(0, width - display)

    print(
        " | ".join(pad(header, widths[index]) for index, header in enumerate(headers))
    )
    print("-+-".join("-" * width for width in widths))
    for row in text_rows:
        print(
            " | ".join(
                pad(row[index] if index < len(row) else "", widths[index])
                for index in range(len(headers))
            )
        )


def _read_schedule_text(path: Optional[str]) -> str:
    if path:
        source = Path(path).expanduser()
        if not source.is_file():
            raise UserError("课表文件不存在：%s" % source)
        try:
            text = source.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise UserError("无法读取课表文件：%s" % exc) from exc
        if not text.strip():
            raise UserError("课表文件为空")
        return text
    if not sys.stdin.isatty():
        text = sys.stdin.read()
        if not text.strip():
            raise UserError("标准输入中没有课表文本")
        return text
    print("请粘贴课表文本；单独输入一行英文句点 . 后按回车结束：")
    lines: List[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == ".":
            break
        lines.append(line)
    text = "\n".join(lines)
    if not text.strip():
        raise UserError("课表文本不能为空")
    return text


def _parse_courses(text: str, mode: str, config: Mapping[str, Any]) -> Dict[str, Any]:
    if mode == "rule":
        from rule_parser import parse_with_rules

        return parse_with_rules(text)
    from ai_parser import parse_timetable, parse_with_ai

    options = {
        "model": config.get("ollama_model"),
        "ollama_url": config.get("ollama_url"),
        "timeout": config.get("ollama_timeout", 30),
    }
    if mode == "ai":
        return parse_with_ai(text, **options)
    return parse_timetable(text, use_ai=True, **options)


def _preview_and_confirm(
    courses: List[Dict[str, Any]],
    assume_yes: bool = False,
) -> Tuple[str, List[Dict[str, Any]]]:
    """返回 (动作, 课程)；动作为 save 或 restart。"""
    while True:
        if courses:
            print()
            _print_table(
                ("序号", "课程名", "星期", "节次", "教室"), _course_rows(courses)
            )
        else:
            print("当前没有可导入课程。")
        if assume_yes:
            if not courses:
                raise UserError("没有可导入课程，不能使用 --yes")
            return "save", courses
        print("\n操作：a=全部确认，e=编辑，d=删除，r=全部重来，m=手工新增，q=取消")
        action = _prompt("请选择").lower()
        if action in ("a", "all", "y", "yes", "确认"):
            if courses:
                return "save", courses
            print("没有可保存的课程，请手工新增或重来。")
        elif action in ("e", "edit", "编辑"):
            if not courses:
                print("没有可编辑的课程。")
                continue
            index = (
                _parse_int_input(_prompt("要编辑的序号"), "序号", 1, len(courses)) - 1
            )
            try:
                courses[index] = _manual_course(courses[index])
            except UserError as exc:
                print("编辑失败：%s" % exc)
        elif action in ("d", "delete", "删除"):
            if not courses:
                print("没有可删除的课程。")
                continue
            index = (
                _parse_int_input(_prompt("要删除的序号"), "序号", 1, len(courses)) - 1
            )
            removed = courses.pop(index)
            print("已从预览移除：%s" % removed.get("name", "未命名课程"))
        elif action in ("r", "restart", "重来"):
            return "restart", courses
        elif action in ("m", "manual", "手工", "新增"):
            try:
                courses.append(_manual_course())
            except UserError as exc:
                print("录入失败：%s" % exc)
        elif action in ("q", "quit", "取消", "n", "no"):
            raise UserError("操作已取消")
        else:
            print("无法识别该操作，请重新选择。")


def command_add_course(args: argparse.Namespace, config: Mapping[str, Any]) -> None:
    while True:
        text = _read_schedule_text(args.file)
        try:
            result = _parse_courses(text, args.mode, config)
        except Exception as exc:
            if args.mode == "ai":
                print("AI 解析失败：%s" % exc)
                if _confirm("改用规则解析", True):
                    result = _parse_courses(text, "rule", config)
                else:
                    result = {"courses": [], "source": "manual", "warnings": []}
            else:
                raise UserError("课表解析失败：%s" % exc) from exc
        source = result.get("source", args.mode)
        print("解析来源：%s；置信度：%s" % (source, result.get("confidence", "未知")))
        fallback_reason = result.get("fallback_reason")
        if fallback_reason:
            print("降级原因：%s" % fallback_reason)
        for warning in result.get("warnings", []):
            print("提示：%s" % warning)
        courses = [
            dict(item)
            for item in result.get("courses", [])
            if isinstance(item, Mapping)
        ]
        candidates = result.get("candidates", [])
        if not courses and candidates:
            print("未能自动结构化，以下行可能包含课程：")
            for index, line in enumerate(candidates, 1):
                print("  %d. %s" % (index, line))
            if not args.yes:
                chosen = _prompt("输入有效候选序号（逗号分隔，直接回车跳过）")
                if chosen:
                    parts = chosen.replace("，", ",").replace(" ", ",").split(",")
                    try:
                        indexes = {
                            _parse_int_input(
                                part.strip(), "候选序号", 1, len(candidates)
                            )
                            for part in parts
                            if part.strip()
                        }
                    except UserError as exc:
                        print("候选选择无效：%s" % exc)
                    else:
                        for index in sorted(indexes):
                            try:
                                courses.append(
                                    _manual_course({"name": candidates[index - 1]})
                                )
                            except UserError as exc:
                                print("候选行录入失败：%s" % exc)
        if (
            not courses
            and not args.yes
            and _confirm("自动解析无结果，进入手工录入", True)
        ):
            courses = _manual_courses()
        action, courses = _preview_and_confirm(courses, args.yes)
        if action == "restart":
            if args.file:
                print("将重新读取文件并解析。")
            continue
        count = _insert_courses(courses)
        print("已保存 %d 门课程。" % count)
        return


def _parse_deadline(value: str) -> str:
    from utils import normalize_deadline

    try:
        return normalize_deadline(value)
    except ValueError as exc:
        raise UserError(str(exc)) from exc


def command_add_homework(args: argparse.Namespace) -> None:
    subject = args.subject or _prompt("科目")
    content = args.content or _prompt("作业内容")
    deadline_text = args.deadline or _prompt(
        "截止日期（如 后天、2026-09-01、2026-09-01 20:00）"
    )
    if not subject.strip():
        raise UserError("科目不能为空")
    if not content.strip():
        raise UserError("作业内容不能为空")
    payload = {
        "subject": subject.strip(),
        "content": content.strip(),
        "deadline": _parse_deadline(deadline_text),
    }
    print(
        "作业预览：%s / %s / %s"
        % (payload["subject"], payload["content"], payload["deadline"])
    )
    if not args.yes and not _confirm("确认保存", True):
        raise UserError("操作已取消")
    result = _insert_homework(payload)
    identifier = result.get("id") if isinstance(result, Mapping) else result
    print(
        "作业已保存%s。"
        % ("（ID: %s）" % identifier if identifier not in (None, "") else "")
    )


def command_list_courses() -> None:
    courses = _list_courses()
    if not courses:
        print("暂无课程。")
        return
    rows = _course_rows(courses)
    # 列表中的第一列应优先显示数据库 ID。
    for index, course in enumerate(courses):
        rows[index][0] = course.get("id", index + 1)
    _print_table(("ID", "课程名", "星期", "节次", "教室"), rows)


def command_list_homeworks() -> None:
    homeworks = _list_homeworks()
    if not homeworks:
        print("暂无作业。")
        return
    rows = [
        [
            item.get("id", ""),
            item.get("subject", ""),
            item.get("content", item.get("name", "")),
            item.get("deadline", ""),
        ]
        for item in homeworks
    ]
    _print_table(("ID", "科目", "作业内容", "截止时间"), rows)


def _run_scheduler(
    config: Mapping[str, Any], stop_event: Optional[threading.Event] = None
) -> Any:
    try:
        import scheduler
    except ImportError as exc:
        raise UserError("调度器模块不可用：%s" % exc) from exc
    runner = _find_callable(
        scheduler, ("run_scheduler", "run", "scheduler_loop", "start_scheduler")
    )
    if runner is not None:
        return _call_compatible(
            runner,
            database=_DATABASE,
            db=_DATABASE,
            config=config,
            stop_event=stop_event,
            daemon=stop_event is not None,
        )
    scheduler_class = getattr(scheduler, "Scheduler", None) or getattr(
        scheduler, "DeskGuardianScheduler", None
    )
    if callable(scheduler_class):
        try:
            instance = _call_compatible(
                scheduler_class,
                database=_DATABASE,
                db=_DATABASE,
                config=config,
                stop_event=stop_event,
            )
        except TypeError:
            # Scheduler 的推荐契约是 Scheduler(database, config=...)。
            instance = _call_compatible(scheduler_class, _DATABASE, config=config)
        run = _find_callable(instance, ("run", "run_forever", "start"))
        if run is None:
            raise UserError("调度器对象缺少 run/start 接口")
        return _call_compatible(run, stop_event=stop_event)
    raise UserError("调度器模块缺少 run_scheduler 接口")


def command_run(config: Mapping[str, Any]) -> None:
    print("提醒守护进程已启动；按 Ctrl+C 停止。")
    stop_event = threading.Event()
    try:
        _run_scheduler(config, stop_event)
    except KeyboardInterrupt:
        stop_event.set()
        print("\n提醒守护进程已停止。")


def _start_scheduler_thread(
    config: Mapping[str, Any],
) -> Tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def target() -> None:
        try:
            _run_scheduler(config, stop_event)
        except Exception as exc:
            print("后台调度器已停止：%s" % exc, file=sys.stderr)

    thread = threading.Thread(target=target, name="DeskGuardianScheduler", daemon=True)
    thread.start()
    return stop_event, thread


def _create_web_app(config: Mapping[str, Any]) -> Any:
    try:
        import webserver
    except ImportError as exc:
        raise UserError("Web 模块不可用：%s" % exc) from exc
    factory = _find_callable(webserver, ("create_app", "make_app"))
    if factory is not None:
        return _call_compatible(
            factory, config=config, database=_DATABASE, db=_DATABASE
        )
    app = getattr(webserver, "app", None)
    if app is not None:
        return app
    return None


def command_web(args: argparse.Namespace, config: Mapping[str, Any]) -> None:
    host = args.host or str(config.get("host", "127.0.0.1"))
    if host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
        raise UserError("非回环监听需要显式使用 --allow-remote，并设置 DESKGUARDIAN_ACCESS_TOKEN")
    if host not in ("127.0.0.1", "localhost", "::1"):
        token = os.environ.get("DESKGUARDIAN_ACCESS_TOKEN")
        if not token or len(token) < 16:
            raise UserError("远程模式需要设置至少 16 个字符的 DESKGUARDIAN_ACCESS_TOKEN")
    port = args.port if args.port is not None else int(config.get("port", 8080))
    stop_event: Optional[threading.Event] = None
    if not args.no_scheduler:
        stop_event, _ = _start_scheduler_thread(config)
    url_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = "http://%s:%d" % (url_host, port)
    if not args.no_browser:
        timer = threading.Timer(0.8, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    print("DeskGuardian Web 服务：%s" % url)
    print("按 Ctrl+C 停止。")
    try:
        runtime_config = dict(config)
        runtime_config["host"] = host
        runtime_config["port"] = port
        app = _create_web_app(runtime_config)
        if app is not None:
            app.run(
                host=host, port=port, debug=False, use_reloader=False, threaded=True
            )
            return
        import webserver

        runner = _find_callable(webserver, ("run_web", "run_server", "serve"))
        if runner is None:
            raise UserError("Web 模块缺少 create_app 或 run_web 接口")
        _call_compatible(
            runner, host=host, port=port, config=config, open_browser=False
        )
    except KeyboardInterrupt:
        print("\nWeb 服务已停止。")
    finally:
        if stop_event is not None:
            stop_event.set()


def _get_dotted(data: Mapping[str, Any], key: str) -> Any:
    value: Any = data
    for part in key.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise UserError("配置项不存在：%s" % key)
        value = value[part]
    return value


def _set_dotted(data: Dict[str, Any], key: str, value: Any) -> None:
    parts = [part for part in key.split(".") if part]
    if not parts:
        raise UserError("配置项名称不能为空")
    target = data
    for part in parts[:-1]:
        current = target.get(part)
        if not isinstance(current, dict):
            current = {}
            target[part] = current
        target = current
    target[parts[-1]] = value


def _decode_config_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        lowered = raw.strip().lower()
        if lowered in ("true", "yes", "on", "是", "启用"):
            return True
        if lowered in ("false", "no", "off", "否", "禁用"):
            return False
        return raw


def command_doctor(args: argparse.Namespace) -> None:
    try:
        from diagnostics import collect_diagnostics
    except ImportError as exc:
        raise UserError("诊断模块不可用：%s" % exc) from exc
    result = collect_diagnostics(check_ollama=bool(args.check_ollama))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("DeskGuardian %s" % result["app"]["version"])
    print("数据目录：%s（可写：%s）" % (result["data_dir"]["path"], result["data_dir"]["writable"]))
    print("配置：%s；数据库完整性：%s" % (result["config"]["valid"], result["db"]["integrity"]))
    print("资源：%s；日志：%s" % (result["resources"]["all_present"], result["log"]["path"]))
    if args.check_ollama:
        print("Ollama：%s" % result["ollama"])


def command_config(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    from config import save_config

    if args.config_action == "show":
        print(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True))
    elif args.config_action == "get":
        value = _get_dotted(config, args.key)
        if isinstance(value, (dict, list, bool)) or value is None:
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            print(value)
    elif args.config_action == "set":
        value = _decode_config_value(args.value)
        updated = dict(config)
        _set_dotted(updated, args.key, value)
        if args.key == "autostart":
            from autostart import configure_autostart
            from utils import parse_bool

            value = configure_autostart(parse_bool(value, "autostart"))
            updated["autostart"] = value
        saved = save_config(updated)
        print(
            "已设置 %s = %s"
            % (args.key, json.dumps(_get_dotted(saved, args.key), ensure_ascii=False))
        )
    else:
        raise UserError("未知配置操作")


def _export_payload(config: Mapping[str, Any]) -> Dict[str, Any]:
    exporter = _find_callable(_DATABASE, ("export_data", "export_json", "dump_data"))
    if exporter is not None:
        result = _call_compatible(exporter)
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                result = None
        if isinstance(result, Mapping):
            payload = dict(result)
            payload.setdefault("config", dict(config))
            return payload
    return {
        "format": "DeskGuardian",
        "version": 1,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "courses": _list_courses(),
        "homeworks": _list_homeworks(),
        "config": dict(config),
    }


def command_export(args: argparse.Namespace, config: Mapping[str, Any]) -> None:
    payload = _export_payload(config)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.file == "-":
        print(text, end="")
        return
    target = Path(args.file).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise UserError("导出失败：%s" % exc) from exc
    print("数据已导出到：%s" % target.resolve())


def _validate_import_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise UserError("导入 JSON 顶层必须是对象")
    courses = payload.get("courses", [])
    homeworks = payload.get("homeworks", [])
    if not isinstance(courses, list) or not all(
        isinstance(item, dict) for item in courses
    ):
        raise UserError("courses 必须是对象数组")
    if not isinstance(homeworks, list) or not all(
        isinstance(item, dict) for item in homeworks
    ):
        raise UserError("homeworks 必须是对象数组")
    return payload


def command_import(args: argparse.Namespace, config: Mapping[str, Any]) -> None:
    source = Path(args.file).expanduser()
    try:
        payload = _validate_import_payload(
            json.loads(source.read_text(encoding="utf-8-sig"))
        )
    except FileNotFoundError as exc:
        raise UserError("导入文件不存在：%s" % source) from exc
    except (OSError, UnicodeError) as exc:
        raise UserError("无法读取导入文件：%s" % exc) from exc
    except json.JSONDecodeError as exc:
        raise UserError(
            "导入文件不是有效 JSON：第 %d 行第 %d 列" % (exc.lineno, exc.colno)
        ) from exc

    importer = _find_callable(_DATABASE, ("import_data", "import_json", "restore_data"))
    if importer is not None:
        try:
            result = _call_compatible(importer, payload, replace=args.replace)
        except TypeError:
            result = _call_compatible(importer, data=payload, replace=args.replace)
        print(
            "数据导入完成%s。" % ("：%s" % result if result not in (None, "") else "")
        )
    else:
        if args.replace:
            for item in _list_courses():
                if item.get("id") is not None:
                    _delete_course(int(item["id"]))
            delete_homework = _db_function(("delete_homework", "remove_homework"))
            for item in _list_homeworks():
                if item.get("id") is not None:
                    delete_homework(int(item["id"]))
        course_count = _insert_courses(payload.get("courses", []))
        for homework in payload.get("homeworks", []):
            clean = {
                key: homework[key]
                for key in ("subject", "content", "deadline")
                if key in homework
            }
            _insert_homework(clean)
        print(
            "已导入 %d 门课程、%d 项作业。"
            % (course_count, len(payload.get("homeworks", [])))
        )

    imported_config = payload.get("config", payload.get("settings"))
    if args.with_config and isinstance(imported_config, Mapping):
        from config import save_config

        save_config(imported_config)
        print("配置也已导入。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=APP_DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", metavar="命令")

    add_parser = subparsers.add_parser("add", help="新增课程或作业")
    add_subparsers = add_parser.add_subparsers(
        dest="add_type", metavar="类型", required=True
    )
    course_parser = add_subparsers.add_parser("course", help="解析并新增课程")
    course_parser.add_argument(
        "--file", "-f", help="从 UTF-8 文本文件读取课表；省略则交互粘贴"
    )
    course_parser.add_argument(
        "--mode",
        choices=("auto", "ai", "rule"),
        default="auto",
        help="解析方式（默认 auto）",
    )
    course_parser.add_argument(
        "--yes", "-y", action="store_true", help="预览后直接确认（适合脚本）"
    )
    homework_parser = add_subparsers.add_parser("homework", help="新增作业")
    homework_parser.add_argument("--subject", "-s", help="科目")
    homework_parser.add_argument("--content", "-c", help="作业内容")
    homework_parser.add_argument("--deadline", "-d", help="截止日期或自然语言日期")
    homework_parser.add_argument(
        "--yes", "-y", action="store_true", help="无需再次确认"
    )

    list_parser = subparsers.add_parser("list", help="列出课程或作业")
    list_subparsers = list_parser.add_subparsers(
        dest="list_type", metavar="类型", required=True
    )
    list_subparsers.add_parser("courses", aliases=["course"], help="列出课程")
    list_subparsers.add_parser("homeworks", aliases=["homework"], help="列出作业")

    subparsers.add_parser("run", help="启动提醒守护进程")

    doctor_parser = subparsers.add_parser("doctor", help="检查运行环境并输出脱敏诊断")
    doctor_parser.add_argument("--json", action="store_true", help="输出 JSON")
    doctor_parser.add_argument("--check-ollama", action="store_true", help="显式检查 Ollama 网络连接")

    web_parser = subparsers.add_parser("web", help="启动 Web 仪表板")
    web_parser.add_argument("--host", help="监听地址（默认读取配置）")
    web_parser.add_argument(
        "--port", type=int, choices=range(1, 65536), metavar="PORT", help="监听端口"
    )
    web_parser.add_argument(
        "--no-browser", action="store_true", help="不自动打开浏览器"
    )
    web_parser.add_argument(
        "--no-scheduler", action="store_true", help="不在 Web 进程中启动调度器"
    )
    web_parser.add_argument(
        "--allow-remote", action="store_true", help="允许非回环监听（必须设置 DESKGUARDIAN_ACCESS_TOKEN）"
    )

    config_parser = subparsers.add_parser("config", help="查看或修改配置")
    config_subparsers = config_parser.add_subparsers(
        dest="config_action", metavar="操作", required=True
    )
    config_subparsers.add_parser("show", help="显示完整配置")
    config_get = config_subparsers.add_parser("get", help="读取配置项")
    config_get.add_argument("key", help="配置项，支持 section_times.1.start")
    config_set = config_subparsers.add_parser("set", help="设置配置项")
    config_set.add_argument("key", help="配置项，支持点号路径")
    config_set.add_argument("value", help="值；支持 JSON 数字、布尔值、对象或普通文本")

    export_parser = subparsers.add_parser("export", help="导出 JSON 数据")
    export_parser.add_argument(
        "file",
        nargs="?",
        default="deskguardian-export.json",
        help="输出文件，- 表示标准输出",
    )
    import_parser = subparsers.add_parser("import", help="导入 JSON 数据")
    import_parser.add_argument("file", help="JSON 文件")
    import_parser.add_argument(
        "--replace", action="store_true", help="导入前删除现有课程和作业"
    )
    import_parser.add_argument(
        "--with-config", action="store_true", help="同时导入配置"
    )
    return parser


def dispatch(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    if args.command == "add" and args.add_type == "course":
        command_add_course(args, config)
    elif args.command == "add" and args.add_type == "homework":
        command_add_homework(args)
    elif args.command == "list" and args.list_type in ("courses", "course"):
        command_list_courses()
    elif args.command == "list" and args.list_type in ("homeworks", "homework"):
        command_list_homeworks()
    elif args.command == "run":
        command_run(config)
    elif args.command == "doctor":
        command_doctor(args)
    elif args.command == "web":
        command_web(args, config)
    elif args.command == "config":
        command_config(args, config)
    elif args.command == "export":
        command_export(args, config)
    elif args.command == "import":
        command_import(args, config)
    else:
        raise UserError("未知命令，请使用 --help 查看帮助")


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_console()
    if callable(setup_logging):
        try:
            setup_logging(debug=bool(os.environ.get("DESKGUARDIAN_DEBUG")))
        except OSError:
            pass
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    # 无参数即启动 Web，保持双击程序与 `python main.py` 的预期行为一致。
    if not arguments:
        arguments = ["web"]
    args = parser.parse_args(arguments)
    try:
        config, _ = _initialize()
        dispatch(args, config)
        return 0
    except KeyboardInterrupt:
        print("\n操作已中止。", file=sys.stderr)
        return 130
    except (UserError, ValueError) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("系统错误：%s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:
        if os.environ.get("DESKGUARDIAN_DEBUG"):
            raise
        print("运行失败：%s" % exc, file=sys.stderr)
        print("如需查看完整回溯，请设置 DESKGUARDIAN_DEBUG=1。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
