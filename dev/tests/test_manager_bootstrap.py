from __future__ import annotations

import ipaddress
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding

from manager.src.bootstrap import (
    BootstrapError, ensure, ensure_api_key, ensure_certificate,
)


class ApiKeyBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = self.root / ".env"

    def test_generates_key_and_preserves_other_values(self) -> None:
        self.env.write_text(
            "POWER_SECRET_ID=id\nMANAGER_API_KEY=\nCOPY=value\n",
            encoding="utf-8",
        )
        key, generated = ensure_api_key(self.env)
        self.assertTrue(generated)
        self.assertGreaterEqual(len(key), 32)
        content = self.env.read_text(encoding="utf-8")
        self.assertIn("POWER_SECRET_ID=id\n", content)
        self.assertIn("COPY=value\n", content)
        self.assertNotIn("MANAGER_API_KEY=\n", content)
        self.assertIn(f"MANAGER_API_KEY={key}\n", content)
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)

    def test_existing_key_is_not_replaced(self) -> None:
        key = "k" * 48
        self.env.write_text(f"MANAGER_API_KEY={key}\n", encoding="utf-8")
        result, generated = ensure_api_key(self.env)
        self.assertEqual(result, key)
        self.assertFalse(generated)
        self.assertEqual(
            self.env.read_text(encoding="utf-8"), f"MANAGER_API_KEY={key}\n",
        )

    def test_missing_key_line_is_appended(self) -> None:
        self.env.write_text("POWER_SECRET_KEY=token\n", encoding="utf-8")
        key, generated = ensure_api_key(self.env)
        self.assertTrue(generated)
        self.assertEqual(
            self.env.read_text(encoding="utf-8"),
            f"POWER_SECRET_KEY=token\nMANAGER_API_KEY={key}\n",
        )


@unittest.skipUnless(shutil.which("openssl"), "需要 openssl")
class CertificateBootstrapTests(unittest.TestCase):
    def test_generates_ca_and_server_certificate_for_all_hosts(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name) / "tls"
        files = ensure_certificate(
            ("mc.example.com", "203.0.113.10"), directory,
        )
        self.assertIsNotNone(files.ca)
        self.assertTrue(files.created)
        for path, mode in (
            (files.ca, 0o644),
            (files.certificate, 0o644),
            (files.private_key, 0o600),
        ):
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, mode)

        ca = x509.load_pem_x509_certificate(files.ca.read_bytes())
        server = x509.load_pem_x509_certificate(files.certificate.read_bytes())
        ca.public_key().verify(
            server.signature,
            server.tbs_certificate_bytes,
            padding.PKCS1v15(),
            server.signature_hash_algorithm,
        )
        self.assertTrue(
            ca.extensions.get_extension_for_class(
                x509.BasicConstraints,
            ).value.ca,
        )
        san = server.extensions.get_extension_for_class(
            x509.SubjectAlternativeName,
        ).value
        self.assertIn(x509.DNSName("mc.example.com"), san)
        self.assertIn(
            x509.IPAddress(ipaddress.ip_address("203.0.113.10")), san,
        )
        self.assertFalse(
            server.extensions.get_extension_for_class(
                x509.BasicConstraints,
            ).value.ca,
        )

        before = {
            path: path.read_bytes()
            for path in (files.ca, files.certificate, files.private_key)
        }
        again = ensure_certificate(("mc.example.com",), directory)
        self.assertFalse(again.created)
        self.assertEqual(again.ca, files.ca)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_existing_certificate_pair_is_reused(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        directory.mkdir(exist_ok=True)
        certificate = directory / "server.crt"
        private_key = directory / "server.key"
        certificate.write_text("public certificate\n", encoding="utf-8")
        private_key.write_text("private key\n", encoding="utf-8")
        files = ensure_certificate(("mc.example.com",), directory)
        self.assertIsNone(files.ca)
        self.assertFalse(files.created)
        self.assertEqual(certificate.read_text(encoding="utf-8"), "public certificate\n")
        self.assertEqual(private_key.read_text(encoding="utf-8"), "private key\n")

    def test_incomplete_certificate_pair_is_rejected(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        (directory / "server.crt").write_text("certificate\n", encoding="utf-8")
        with self.assertRaisesRegex(BootstrapError, "不完整"):
            ensure_certificate(("mc.example.com",), directory)


@unittest.skipUnless(shutil.which("openssl"), "需要 openssl")
class ManagerBootstrapTests(unittest.TestCase):
    def test_ensure_generates_key_and_certificate_from_config_host(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "data").mkdir()
        (root / "data/config.yml").write_text(
            yaml.safe_dump({"host": "mc.example.com"}), encoding="utf-8",
        )
        ensure(root)
        values = (root / "data/.env").read_text(encoding="utf-8")
        self.assertIn("MANAGER_API_KEY=", values)
        self.assertTrue((root / "data/tls/server.crt").is_file())
        self.assertTrue((root / "data/tls/server.key").is_file())
        self.assertTrue((root / "data/tls/ca.crt").is_file())
        before = (root / "data/tls/server.crt").read_bytes()
        ensure(root)
        self.assertEqual((root / "data/tls/server.crt").read_bytes(), before)

    def test_ensure_requires_host(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "data").mkdir()
        (root / "data/config.yml").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(BootstrapError, "host"):
            ensure(root)


if __name__ == "__main__":
    unittest.main()
