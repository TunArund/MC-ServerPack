"""MCSP 共享可观测性：日志配置、上下文和敏感信息脱敏。"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.config
import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any


SECRET_OPTION_RE = re.compile(
    r"(?i)(secret|token|password|api[-_]?key|credential|authorization)"
)
ENV_SECRET_RE = re.compile(
    r"(?i)^(?P<name>[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY)[A-Z0-9_]*)=.*$"
)
REDACTED = "<redacted>"
operation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "operation_id", default="-",
)


def redact_argv(arguments: Iterable[object]) -> list[str]:
    """隐藏敏感选项的下一参数以及 KEY=value 形式的密钥。"""
    values = [str(value) for value in arguments]
    output: list[str] = []
    hide_next = False
    for value in values:
        if hide_next:
            output.append(REDACTED)
            hide_next = False
            continue
        environment = ENV_SECRET_RE.fullmatch(value)
        if environment:
            output.append(f"{environment.group('name')}={REDACTED}")
            continue
        if "=" in value and value.startswith("-"):
            name, _ = value.split("=", 1)
            if SECRET_OPTION_RE.search(name):
                output.append(f"{name}={REDACTED}")
                continue
        output.append(value)
        if value.startswith("-") and SECRET_OPTION_RE.search(value):
            hide_next = True
    return output


def redact_data(value: Any, key: str = "") -> Any:
    """递归脱敏日志的结构化字段，不修改业务对象。"""
    if key and SECRET_OPTION_RE.search(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(name): redact_data(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_data(item) for item in value]
    return value


class OperationContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.operation_id = operation_id.get()
        return True


class JsonFormatter(logging.Formatter):
    """将一个 LogRecord 格式化为一个 UTF-8 JSON 事件。"""

    _reserved = set(logging.makeLogRecord({}).__dict__) | {
        "message", "asctime", "operation_id",
    }

    def __init__(self, timezone=None) -> None:
        super().__init__()
        self.timezone = timezone

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, self.timezone)
        if self.timezone is None:
            timestamp = timestamp.astimezone()
        value = {
            "time": timestamp.isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "operation_id": record.operation_id,
            "message": record.getMessage(),
        }
        extra = {
            key: item for key, item in record.__dict__.items()
            if key not in self._reserved and not key.startswith("_")
        }
        value.update(redact_data(extra))
        if record.exc_info:
            value["exception"] = self.formatException(record.exc_info)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=repr)


def configure_logging(
    targets: Mapping[str, Path], *, stream: bool = False, timezone=None,
) -> None:
    """入口一次配置；空 logger 名表示 root，其余 logger 互不传播。"""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    if not isinstance(logging.getLevelName(level), int):
        level = "INFO"
    handlers: dict[str, dict] = {}
    loggers: dict[str, dict] = {}
    root_handlers: list[str] = []
    for index, (name, path) in enumerate(targets.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = f"file_{index}"
        target_handlers = [file_handler]
        handlers[file_handler] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(path), "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3, "encoding": "utf-8", "level": level,
            "formatter": "json", "filters": ["operation"],
        }
        if stream:
            stream_handler = f"stream_{index}"
            handlers[stream_handler] = {
                "class": "logging.StreamHandler", "stream": "ext://sys.stderr",
                "level": level, "formatter": "json", "filters": ["operation"],
            }
            target_handlers.append(stream_handler)
        if name:
            loggers[name] = {
                "handlers": target_handlers, "level": level, "propagate": False,
            }
        else:
            root_handlers = target_handlers
    logging.config.dictConfig({
        "version": 1, "disable_existing_loggers": False,
        "filters": {"operation": {"()": OperationContextFilter}},
        "formatters": {"json": {"()": JsonFormatter, "timezone": timezone}},
        "handlers": handlers, "loggers": loggers,
        "root": {"handlers": root_handlers, "level": level},
    })


logging.getLogger(__name__).addHandler(logging.NullHandler())
