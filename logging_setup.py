"""Privacy-conscious file logging for DeskGuardian.

Application code should log operational events (state changes, record IDs, counts,
and error types), never course names, homework text, imported timetable text, AI
prompts, or AI responses.  The filter below additionally redacts common business
content fields and supports explicitly marking an entire record as business text.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

try:
    from config import get_data_dir
except ImportError:  # pragma: no cover - package-style import
    from .config import get_data_dir


LOG_DIRECTORY = "logs"
LOG_FILENAME = "deskguardian.log"
DEFAULT_MAX_BYTES = 1 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3
_HANDLER_MARKER = "_deskguardian_rotating_file_handler"
_REDACTED = "[REDACTED]"
_SAFE_TEXT_FIELDS = frozenset(
    {"component", "entity_type", "event", "event_type", "operation", "status"}
)
_BUSINESS_FIELDS = frozenset(
    {
        "body",
        "business_content",
        "content",
        "course",
        "course_name",
        "homework",
        "homework_content",
        "import_text",
        "message_body",
        "prompt",
        "raw_text",
        "response",
        "subject",
        "text",
    }
)
PathLike = Union[str, Path]


def get_log_path(data_dir: Optional[PathLike] = None) -> str:
    """Return the canonical UTF-8 log file path without creating the file."""
    root = Path(data_dir) if data_dir is not None else Path(get_data_dir())
    return str((root.expanduser().resolve() / LOG_DIRECTORY / LOG_FILENAME))


def _redact_mapping(value: Mapping[Any, Any]) -> Dict[Any, Any]:
    """Copy a logging mapping while removing known business-content values."""
    sanitized = {}  # type: Dict[Any, Any]
    for key, item in value.items():
        normalized_key = str(key).lower()
        if normalized_key in _BUSINESS_FIELDS:
            sanitized[key] = _REDACTED
        elif isinstance(item, Mapping):
            sanitized[key] = _redact_mapping(item)
        elif isinstance(item, str) and normalized_key not in _SAFE_TEXT_FIELDS:
            sanitized[key] = _REDACTED
        else:
            sanitized[key] = item
    return sanitized


def _redact_argument(value: Any) -> Any:
    """Keep safe scalar metadata while suppressing free-form logging values."""
    if isinstance(value, Mapping):
        return _redact_mapping(value)
    if isinstance(value, BaseException):
        return type(value).__name__
    if isinstance(value, str):
        return _REDACTED
    if isinstance(value, tuple):
        return tuple(_redact_argument(item) for item in value)
    if isinstance(value, list):
        return [_redact_argument(item) for item in value]
    return value


class BusinessContentFilter(logging.Filter):
    """Prevent explicitly identified business正文 from reaching the log file.

    Arbitrary prose cannot be reliably classified as private.  Callers therefore
    must follow this module's operational-only logging contract.  For defensive
    protection, mappings are sanitized and a record created with
    ``extra={"business_content": True}`` has its complete message suppressed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "business_content", False):
            record.msg = "[REDACTED BUSINESS CONTENT]"
            record.args = ()
            return True

        if isinstance(record.msg, Mapping):
            record.msg = _redact_mapping(record.msg)
        if isinstance(record.args, Mapping):
            record.args = _redact_mapping(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(_redact_argument(item) for item in record.args)
        for field in _BUSINESS_FIELDS:
            if hasattr(record, field):
                setattr(record, field, _REDACTED)
        return True


def setup_logging(
    debug: bool = False,
    data_dir: Optional[PathLike] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> logging.Logger:
    """Configure and return the root logger.

    Repeated calls are idempotent: DeskGuardian's previous file handler is
    replaced so tests, portable data directories, and runtime reconfiguration do
    not duplicate records.  Other handlers installed by an embedding
    application are left untouched.
    """
    if isinstance(max_bytes, bool) or int(max_bytes) < 1:
        raise ValueError("max_bytes must be a positive integer")
    if isinstance(backup_count, bool) or int(backup_count) < 0:
        raise ValueError("backup_count must be a non-negative integer")

    log_path = Path(get_log_path(data_dir))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)
            handler.close()

    handler = RotatingFileHandler(
        str(log_path),
        maxBytes=int(max_bytes),
        backupCount=int(backup_count),
        encoding="utf-8",
    )
    setattr(handler, _HANDLER_MARKER, True)
    handler.setLevel(logging.DEBUG if debug else logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler.addFilter(BusinessContentFilter())
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    return root


__all__ = [
    "BusinessContentFilter",
    "DEFAULT_BACKUP_COUNT",
    "DEFAULT_MAX_BYTES",
    "LOG_DIRECTORY",
    "LOG_FILENAME",
    "get_log_path",
    "setup_logging",
]
