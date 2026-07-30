import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from nsh.network.backend import (
    RemoteBackend, RemoteEntry, SFTPBackend, parse_target)
from nsh.network.view import NetworkView


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

    def test_download_targets_displayed_local_pane(self):
        class App:
            cwd = Path("wrong-directory")

            def set_message(self, message):
                self.message = message

            def invalidate(self):
                pass

        class LocalPane:
            def __init__(self, cwd):
                self.cwd = cwd
                self.refreshed = False

            def refresh(self):
                self.refreshed = True

        class DownloadBackend:
            def download(self, remote, local):
                with open(local, "wb") as stream:
                    stream.write(b"remote data")

        async def scenario(directory):
            app = App()
            view = NetworkView(app)
            local = LocalPane(Path(directory))
            view.local_view = local
            view.backend = DownloadBackend()
            view.entries = [RemoteEntry("report.txt", "/report.txt", False)]
            view.download()
            while view.busy:
                await asyncio.sleep(0.01)
            return local

        with TemporaryDirectory() as temp:
            local = asyncio.run(scenario(temp))
            self.assertEqual(
                (Path(temp) / "report.txt").read_bytes(), b"remote data")
            self.assertTrue(local.refreshed)

    def test_upload_reads_displayed_local_pane_selection(self):
        class App:
            class WrongPane:
                def _targets(self):
                    raise AssertionError("hidden app explorer must not be used")

            explorer = WrongPane()

            def set_message(self, message):
                self.message = message

            def invalidate(self):
                pass

        class LocalPane:
            def __init__(self, path):
                self.path = path

            def _targets(self):
                return [self.path]

        class UploadBackend:
            def __init__(self):
                self.uploaded = None

            def unique_path(self, directory, name):
                return directory + "/" + name

            def upload(self, local, remote):
                self.uploaded = (Path(local), remote)

            def listdir(self, path):
                return []

        async def scenario(local_file):
            app = App()
            view = NetworkView(app)
            backend = UploadBackend()
            view.local_view = LocalPane(local_file)
            view.backend = backend
            view.path = "/incoming"
            view.upload()
            while view.busy:
                await asyncio.sleep(0.01)
            return backend

        with TemporaryDirectory() as temp:
            local_file = Path(temp) / "upload.txt"
            local_file.write_text("local data", encoding="utf-8")
            backend = asyncio.run(scenario(local_file))
            self.assertEqual(
                backend.uploaded, (local_file, "/incoming/upload.txt"))


if __name__ == "__main__":
    unittest.main()
