import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from nsh.network.backend import (
    RemoteBackend, RemoteEntry, SFTPBackend, parse_target)


class FakeBackend(RemoteBackend):
    def __init__(self, names):
        super().__init__("example.com", 22, "user")
        self.names = names

    def listdir(self, path):
        return [RemoteEntry(name, path + "/" + name, False) for name in self.names]


class NetworkBackendTests(unittest.TestCase):
    def test_parse_sftp_target(self):
        self.assertEqual(
            parse_target("sftp", "alice@example.com:2222/home/alice"),
            ("example.com", 2222, "alice", "/home/alice"),
        )

    def test_parse_ftp_defaults(self):
        self.assertEqual(
            parse_target("ftp", "ftp.example.com"),
            ("ftp.example.com", 21, "anonymous", "/"),
        )

    def test_unique_remote_path(self):
        backend = FakeBackend({"report.txt", "report (2).txt"})
        self.assertEqual(
            backend.unique_path("/upload", "report.txt"),
            "/upload/report (3).txt",
        )

    def test_sftp_uses_proxyjump_from_ssh_config(self):
        clients = []
        channel = object()

        class Transport:
            def is_active(self):
                return True

            def open_channel(self, kind, destination, source):
                self.call = (kind, destination, source)
                return channel

        class Client:
            def __init__(self):
                self.transport = Transport()
                self.connect_args = None
                clients.append(self)

            def load_system_host_keys(self):
                pass

            def set_missing_host_key_policy(self, policy):
                pass

            def connect(self, hostname, **kwargs):
                self.connect_args = (hostname, kwargs)

            def get_transport(self):
                return self.transport

            def open_sftp(self):
                return object()

            def close(self):
                pass

        with TemporaryDirectory() as temp:
            home = Path(temp)
            ssh_dir = home / ".ssh"
            ssh_dir.mkdir()
            with open(ssh_dir / "config", "w", encoding="utf-8") as stream:
                stream.write(
                    "Host target-alias\n"
                    "  HostName target.example\n"
                    "  User dest\n"
                    "  ProxyJump jump@bastion.example:2222\n")
            with \
                mock.patch("paramiko.SSHClient", Client), \
                mock.patch("nsh.network.backend.Path.home",
                           return_value=home):
                backend = SFTPBackend.connect(
                    "target-alias", 22, "", "secret")

        self.assertEqual(len(clients), 2)
        self.assertEqual(clients[0].connect_args[0], "bastion.example")
        self.assertEqual(clients[0].connect_args[1]["port"], 2222)
        self.assertEqual(
            clients[0].transport.call,
            ("direct-tcpip", ("target.example", 22), ("127.0.0.1", 0)),
        )
        self.assertIs(clients[1].connect_args[1]["sock"], channel)
        self.assertEqual(len(backend.ssh_clients), 2)


if __name__ == "__main__":
    unittest.main()
