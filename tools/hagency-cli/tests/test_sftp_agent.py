from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import paramiko

from hagency_cli.files.sync.sftp import _connect_agent


class SFTPAgentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    @unittest.skipIf(os.name == "nt", "Unix agent socket test")
    def test_missing_socket_preserves_authentication_fallback(self):
        agent = _connect_agent(str(self.root / "absent"), 0.1)
        self.addCleanup(agent.close)
        self.assertEqual(agent.get_keys(), ())

    @unittest.skipIf(os.name == "nt", "Unix agent socket test")
    def test_unresponsive_agent_is_bounded_and_connection_is_closed(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            path = str(self.root / "hung")
            listener.bind(path)
            listener.listen()
            listener.settimeout(3)
            closed = threading.Event()

            def serve():
                with listener.accept()[0] as connection:
                    connection.settimeout(3)
                    # Read the request but deliberately withhold the response.
                    while connection.recv(4096):
                        pass
                    closed.set()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            with self.assertRaises(TimeoutError):
                _connect_agent(path, 0.05)
            self.assertTrue(closed.wait(3), "timed-out agent connection leaked")
            thread.join(timeout=3)

    def test_native_windows_agent_discovery_is_preserved(self):
        with (
            mock.patch("hagency_cli.files.sync.sftp.sys.platform", "win32"),
            mock.patch("paramiko.Agent") as native,
        ):
            self.assertIs(
                _connect_agent("configured-but-unused", 1), native.return_value
            )

    @unittest.skipUnless(
        os.name != "nt" and shutil.which("ssh-agent") and shutil.which("ssh-add"),
        "requires a local OpenSSH agent",
    )
    def test_real_agent_keys_sign_and_work_with_paramiko_client_authentication(self):
        path = str(self.root / "agent")
        process = subprocess.Popen(
            ["ssh-agent", "-D", "-a", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def stop():
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

        self.addCleanup(stop)
        deadline = time.monotonic() + 3
        while not Path(path).exists() and time.monotonic() < deadline:
            self.assertIsNone(process.poll(), "test agent exited before listening")
            time.sleep(0.01)
        self.assertTrue(Path(path).exists())
        private_key = paramiko.RSAKey.generate(2048)
        key_path = self.root / "key"
        private_key.write_private_key_file(str(key_path))
        subprocess.run(
            ["ssh-add", str(key_path)],
            env={**os.environ, "SSH_AUTH_SOCK": path},
            check=True,
            capture_output=True,
            timeout=3,
        )
        agent = _connect_agent(path, 2)
        self.addCleanup(agent.close)
        self.assertEqual(len(agent.get_keys()), 1)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        client._agent = agent
        client._transport = mock.Mock()

        def authenticate(username, key):
            self.assertEqual(username, "fixture")
            self.assertIs(key, agent.get_keys()[0])
            signature = key.sign_ssh_data(b"test challenge", algorithm="rsa-sha2-512")
            self.assertTrue(
                private_key.verify_ssh_sig(
                    b"test challenge", paramiko.Message(signature)
                )
            )
            return []

        client._transport.auth_publickey.side_effect = authenticate
        with mock.patch(
            "paramiko.client.Agent",
            side_effect=AssertionError("must reuse explicit agent"),
        ):
            client._auth(
                username="fixture",
                password=None,
                pkey=None,
                key_filenames=[],
                allow_agent=True,
                look_for_keys=False,
                gss_auth=False,
                gss_kex=False,
                gss_deleg_creds=False,
                gss_host=None,
                passphrase=None,
            )
        client._transport.auth_publickey.assert_called_once()


if __name__ == "__main__":
    unittest.main()
