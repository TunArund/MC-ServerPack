"""manager.sh 的容器内 JSON 边界；宿主无需 Python 或 jq。"""

from __future__ import annotations

import json
import sys

from .manager_sdk import format_job_event


def _document() -> dict:
    try:
        value = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Manager API 返回了无效 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Manager API 返回格式无效")
    return value


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    try:
        document = _document()
        if arguments == ["response"]:
            if document.get("ok") is not True:
                error = document.get("error")
                message = error.get("message") if isinstance(error, dict) else None
                print(message or "Manager API 请求失败", file=sys.stderr)
                return 2
            print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
            return 0
        data = document.get("data")
        if document.get("ok") is not True or not isinstance(data, dict):
            raise ValueError("Manager API 响应格式无效")
        if arguments == ["job-id"]:
            job_id = data.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise ValueError("Manager API 未返回任务编号")
            print(job_id)
            return 0
        if len(arguments) == 2 and arguments[0] == "job-update":
            sequence = int(arguments[1])
            events = data.get("events")
            state = data.get("state")
            if not isinstance(events, list) or state not in {
                "queued", "running", "succeeded", "failed", "cancelled",
            }:
                raise ValueError("Manager Job 响应格式无效")
            for event in events:
                if not isinstance(event, dict):
                    raise ValueError("Manager Job 事件格式无效")
                sequence = max(sequence, int(event.get("seq", sequence)))
                line = format_job_event(event)
                if line:
                    print(line, file=sys.stderr)
            print(f"{state}\t{sequence}")
            return 0
        raise ValueError("未知 JSON bridge 操作")
    except (TypeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
