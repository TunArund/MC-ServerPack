from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from monitor.src.manager_sdk import (
    API_PATH, STOP_ALL_PATH, ManagerSdk, ManagerSdkError,
)

ManagerApiError = ManagerSdkError


class Response:
    def __init__(self, document: object) -> None:
        self.body = json.dumps(document).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return self.body


class ManagerSdkTests(unittest.TestCase):
    def test_list_signs_request_and_parses_modpacks(self) -> None:
        key = "s" * 32
        client = ManagerSdk("203.0.113.10", 27016, key)
        document = {"ok": True, "data": {"modpacks": [{
            "id": "closing-song", "name": "落幕曲", "state": "running",
            "port": 27015, "address": "mc.example.com:27015",
        }]}}
        with patch("monitor.src.manager_sdk.time.time", return_value=123), patch(
            "monitor.src.manager_sdk.ManagerSdk._opener",
        ) as opener:
            open_url = opener.return_value.open
            open_url.return_value = Response(document)
            result = client.list("203.0.113.10")
        request = open_url.call_args.args[0]
        nonce = request.get_header("X-mcsp-nonce")
        body_hash = hashlib.sha256(b"").hexdigest()
        expected = hmac.new(key.encode(), f"GET\n{API_PATH}\n123\n{nonce}\n{body_hash}".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(request.full_url, "https://203.0.113.10:27016/v1/modpacks")
        self.assertEqual(request.get_header("X-mcsp-timestamp"), "123")
        self.assertEqual(request.get_header("X-mcsp-signature"), expected)
        self.assertEqual(result[0].name, "落幕曲")

    def test_invalid_response_is_rejected(self) -> None:
        client = ManagerSdk("203.0.113.10", 27016, "s" * 32)
        with patch(
            "monitor.src.manager_sdk.ManagerSdk._opener",
        ) as opener:
            opener.return_value.open.return_value = Response(
                {"ok": True, "data": {"modpacks": [{"id": "broken"}]}},
            )
            with self.assertRaisesRegex(ManagerApiError, "缺少字段"):
                client.list("203.0.113.10")

    def test_list_accepts_manager_runtime_states(self) -> None:
        client = ManagerSdk("203.0.113.10", 27016, "s" * 32)
        document = {"ok": True, "data": {"modpacks": [
            {"id": "healthy", "name": "健康", "state": "healthy", "port": 27015},
            {"id": "maintenance", "name": "维护", "state": "maintenance", "port": 27016},
        ]}}
        with patch(
            "monitor.src.manager_sdk.ManagerSdk._opener",
        ) as opener:
            opener.return_value.open.return_value = Response(document)
            result = client.list("203.0.113.10")
        self.assertEqual(
            [item.state for item in result], ["healthy", "maintenance"],
        )

    def test_stop_all_submits_and_waits_for_success(self) -> None:
        key = "s" * 32
        client = ManagerSdk("mc.example.com", 27016, key)
        documents = [
            {"ok": True, "data": {"job_id": "job-1", "state": "queued"}},
            {"ok": True, "data": {
                "job_id": "job-1", "state": "succeeded",
                "events": [{"seq": 1, "message": "done"}],
            }},
        ]
        with patch("monitor.src.manager_sdk.time.time", return_value=123), patch(
            "monitor.src.manager_sdk.ManagerSdk._opener",
        ) as opener:
            open_url = opener.return_value.open
            open_url.side_effect = [Response(document) for document in documents]
            client.stop_all()
        first, second = [call.args[0] for call in open_url.call_args_list]
        self.assertEqual(
            first.full_url, f"https://mc.example.com:27016{STOP_ALL_PATH}",
        )
        self.assertEqual(first.get_method(), "POST")
        nonce = first.get_header("X-mcsp-nonce")
        body_hash = hashlib.sha256(b"{}").hexdigest()
        expected = hmac.new(
            key.encode(),
            f"POST\n{STOP_ALL_PATH}\n123\n{nonce}\n{body_hash}".encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(first.get_header("X-mcsp-signature"), expected)
        self.assertEqual(
            second.full_url,
            "https://mc.example.com:27016/v1/jobs/job-1?after=0&wait=20",
        )

    def test_stop_all_reports_job_failure(self) -> None:
        client = ManagerSdk("mc.example.com", 27016, "s" * 32)
        documents = [
            {"ok": True, "data": {"job_id": "job-1", "state": "queued"}},
            {"ok": True, "data": {
                "job_id": "job-1", "state": "failed", "events": [],
                "error": {"message": "停止失败"},
            }},
        ]
        with patch(
            "monitor.src.manager_sdk.ManagerSdk._opener",
        ) as opener:
            opener.return_value.open.side_effect = [
                Response(document) for document in documents
            ]
            with self.assertRaisesRegex(ManagerApiError, "停止失败"):
                client.stop_all()

    def test_stop_all_times_out(self) -> None:
        client = ManagerSdk(
            "mc.example.com", 27016, "s" * 32, job_timeout=0,
        )
        documents = [
            {"ok": True, "data": {"job_id": "job-1", "state": "queued"}},
            {"ok": True, "data": {
                "job_id": "job-1", "state": "running", "events": [],
            }},
        ]
        with patch(
            "monitor.src.manager_sdk.ManagerSdk._opener",
        ) as opener:
            opener.return_value.open.side_effect = [
                Response(document) for document in documents
            ]
            with self.assertRaisesRegex(ManagerApiError, "超时"):
                client.stop_all()


if __name__ == "__main__":
    unittest.main()
