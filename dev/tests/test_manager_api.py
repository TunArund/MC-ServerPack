from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from manager.src.app import Nonces, _keys, signature
from manager.src.manager_sdk import ManagerSdk
from manager.src.jobs import JobStore
from manager.src.router import Request, Router
from manager.src.transfer import CHUNK_SIZE, TransferStore


class ManagerProtocolTests(unittest.TestCase):
    def test_signature_covers_the_entire_request(self) -> None:
        body = b'{"now":true}'
        value = signature("secret", "POST", "/v1/x?a=1", "123", "nonce", body)
        canonical = "POST\n/v1/x?a=1\n123\nnonce\n" + hashlib.sha256(body).hexdigest()
        self.assertEqual(value, hmac.new(b"secret", canonical.encode(), hashlib.sha256).hexdigest())

    def test_single_key_registers_both_identities_with_separate_scopes(self) -> None:
        with patch.dict(
            "os.environ", {"MANAGER_API_KEY": "k" * 32}, clear=True,
        ):
            keys = _keys()
        self.assertEqual(keys["admin"][0], "k" * 32)
        self.assertEqual(keys["monitor"][0], "k" * 32)
        self.assertIn("*", keys["admin"][1])
        self.assertIn("modpacks:stop", keys["monitor"][1])
        self.assertNotIn("*", keys["monitor"][1])

    def test_short_api_key_is_rejected(self) -> None:
        with patch.dict(
            "os.environ", {"MANAGER_API_KEY": "short"}, clear=True,
        ), self.assertRaisesRegex(Exception, "至少需要 32 个字符"):
            _keys()

    def test_nonce_cannot_be_replayed(self) -> None:
        nonces = Nonces()
        now = int(time.time())
        self.assertTrue(nonces.accept("terminal", "one", now))
        self.assertFalse(nonces.accept("terminal", "one", now))

    def test_router_uses_envelope_and_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            router = Router(Path(raw))
            denied = router.dispatch(Request("GET", "/v1/modpacks", scopes=frozenset({"health"})))
            self.assertEqual(denied.status, 403)
            self.assertEqual(denied.document["error"]["code"], "permission_denied")
            healthy = router.dispatch(Request("GET", "/v1/health", scopes=frozenset({"health"})))
            self.assertTrue(healthy.document["ok"])
            self.assertTrue(healthy.document["request_id"])

    def test_completed_job_survives_store_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = JobStore(root, workers=1)
            request = Request("POST", "/work", idempotency_key="same")
            job = store.submit(request, "request", "work", lambda _: {"done": True})
            deadline = time.monotonic() + 2
            while job.state not in ("succeeded", "failed") and time.monotonic() < deadline:
                time.sleep(0.01)
            restored = JobStore(root, workers=1)
            loaded = restored.get(job.job_id)
            self.assertEqual(loaded.state, "succeeded")
            self.assertEqual(loaded.result, {"done": True})
            store.close()
            restored.close()

    def test_sdk_does_not_route_manager_api_through_environment_proxy(self) -> None:
        sdk = ManagerSdk("manager.example", 27016, "k" * 32)
        handlers = sdk._opener().handlers
        self.assertFalse(
            any(
                handler.proxies
                for handler in handlers
                if isinstance(handler, urllib.request.ProxyHandler)
            )
        )

    def test_upload_chunks_are_hashed_and_reassembled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = TransferStore(Path(raw)); content = b"archive"
            created = store.create_upload("Pack-1", len(content), hashlib.sha256(content).hexdigest(), force=False)
            store.put_chunk(created["transfer_id"], 0, content, hashlib.sha256(content).hexdigest())
            archive, metadata = store.assemble(created["transfer_id"])
            self.assertEqual(archive.read_bytes(), content)
            self.assertEqual(metadata["chunk_size"], CHUNK_SIZE)


if __name__ == "__main__":
    unittest.main()
