from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from lib.deploy import AccountError, AccountProvisioner, read_public_key
from lib.remote import CommandError, target


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def ssh(self, destination, arguments, **options):
        self.calls.append((destination, arguments, options))
        stdout = b""
        if arguments[:2] == ["getent", "passwd"]:
            username = arguments[2]
            stdout = (
                f"{username}:x:1001:1001::/home/{username}:/bin/bash\n"
            ).encode()
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout)


class AccountTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = self.root / "id_ed25519.pub"
        self.key.write_text("ssh-ed25519 AAAATEST terminal@test\n")
        self.key.with_suffix("").write_text("private-test-placeholder\n")
        self.transport = FakeTransport()
        self.accounts = AccountProvisioner(self.transport)

    def test_create_uses_root_ssh_and_idempotent_locked_user_script(self) -> None:
        self.accounts.create("manager.example", "minecraft", "root", self.key)
        destination, arguments, options = self.transport.calls[0]
        self.assertEqual(destination, "root@manager.example")
        self.assertEqual(arguments[:4], ["bash", "-s", "--", "minecraft"])
        self.assertEqual(arguments[4], "ssh-ed25519 AAAATEST terminal@test")
        self.assertEqual(options["identity"], self.key.with_suffix(""))
        script = options["input_text"]
        self.assertIn('passwd --lock "$user"', script)
        self.assertLess(
            script.index('passwd --lock "$user"'),
            script.index("fi\nhome=$(getent passwd"),
        )
        self.assertIn("NOPASSWD: ALL", script)
        self.assertIn("getent group docker", script)
        self.assertNotIn("useradd -p", script)
        self.assertEqual(
            self.transport.calls[1][1], ["getent", "passwd", "minecraft"],
        )

    def test_non_root_administrator_uses_noninteractive_sudo(self) -> None:
        self.accounts.create("manager.example", "minecraft", "operator", self.key)
        destination, arguments, _ = self.transport.calls[0]
        self.assertEqual(destination, "operator@manager.example")
        self.assertEqual(arguments[:6], [
            "sudo", "--non-interactive", "bash", "-s", "--", "minecraft",
        ])

    def test_login_uses_private_half_of_reported_public_key(self) -> None:
        source = self.accounts.prepare_key(self.key)
        self.accounts.test_login("manager.example", "root", source)
        destination, arguments, options = self.transport.calls[0]
        self.assertEqual(destination, "root@manager.example")
        self.assertEqual(arguments, ["true"])
        self.assertEqual(options["identity"], self.key.with_suffix(""))

    def test_username_and_host_reject_injection(self) -> None:
        for username in ("root", "BadUser", "bad;user", ""):
            with self.subTest(username=username), self.assertRaises(AccountError):
                self.accounts.create("manager.example", username, "root", self.key)
        for administrator in ("BadUser", "bad;user", ""):
            with self.subTest(administrator=administrator), self.assertRaises(AccountError):
                self.accounts.create(
                    "manager.example", "minecraft", administrator, self.key,
                )
        for host in ("-oProxyCommand=bad", "bad host", "host;bad"):
            with self.subTest(host=host), self.assertRaises(CommandError):
                target("root", host)

    def test_public_key_must_be_one_supported_line(self) -> None:
        self.key.write_text("not-a-key\n")
        with self.assertRaisesRegex(AccountError, "格式无效"):
            read_public_key(self.key)


if __name__ == "__main__":
    unittest.main()
