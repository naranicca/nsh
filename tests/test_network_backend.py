import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from nsh.app import NshApp
from nsh.explorer.view import ExplorerView
from nsh.network.backend import (
    HostKeyRequired, RemoteBackend, RemoteEntry, SFTPBackend, parse_target)
from nsh.network.view import NetworkView
from nsh.search.view import SearchView


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
                class SFTP:
                    def close(self):
                        pass
                return SFTP()

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

    def test_sftp_prompts_then_saves_an_approved_host_key(self):
        class Key:
            def asbytes(self):
                return b"mac-host-key"

            def get_name(self):
                return "ssh-ed25519"

        class HostKeys:
            def __init__(self):
                self.added = None

            def add(self, hostname, key_type, key):
                self.added = (hostname, key_type, key)

        clients = []

        class Client:
            def __init__(self):
                self.host_keys = HostKeys()
                clients.append(self)

            def load_system_host_keys(self):
                pass

            def load_host_keys(self, filename):
                self.loaded = filename

            def set_missing_host_key_policy(self, policy):
                self.policy = policy

            def connect(self, hostname, **kwargs):
                self.policy.missing_host_key(self, hostname, Key())

            def get_host_keys(self):
                return self.host_keys

            def save_host_keys(self, filename):
                Path(filename).write_text("saved", encoding="utf-8")

            def open_sftp(self):
                class SFTP:
                    def close(self):
                        pass
                return SFTP()

            def close(self):
                pass

        with TemporaryDirectory() as temp:
            home = Path(temp)
            with mock.patch("paramiko.SSHClient", Client), mock.patch(
                    "nsh.network.backend.Path.home", return_value=home):
                with self.assertRaises(HostKeyRequired) as raised:
                    SFTPBackend.connect(
                        "192.168.45.75", 22, "naranicca", "secret")
                requested = raised.exception
                backend = SFTPBackend.connect(
                    "192.168.45.75", 22, "naranicca", "secret",
                    accept_host_key=(requested.hostname,
                                     requested.fingerprint))

            self.assertEqual(requested.hostname, "192.168.45.75")
            self.assertTrue(requested.fingerprint.startswith("SHA256:"))
            self.assertTrue((home / ".ssh" / "known_hosts").exists())
            self.assertEqual(clients[-1].host_keys.added[0],
                             "192.168.45.75")
            backend.close()

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

    def test_escape_action_clears_selection_without_disconnect(self):
        class App:
            def invalidate(self):
                self.invalidated = True

        app = App()
        view = NetworkView(app)
        backend = object()
        view.backend = backend
        view.selected = {"/important.txt"}

        view.cancel()

        self.assertEqual(view.selected, set())
        self.assertIs(view.backend, backend)
        self.assertTrue(app.invalidated)

    def test_disconnect_requires_confirmation(self):
        class Backend:
            label = "sftp://example.com"

        class App:
            def confirm(self, label, callback):
                self.confirmation = (label, callback)

            def set_message(self, message):
                self.message = message

        app = App()
        view = NetworkView(app)
        backend = Backend()
        view.backend = backend

        view.disconnect()

        self.assertIs(view.backend, backend)
        self.assertEqual(app.confirmation[0],
                         "Disconnect from sftp://example.com?")
        app.confirmation[1](False)
        self.assertIs(view.backend, backend)

    def test_remote_directory_expands_inline(self):
        class App:
            def invalidate(self):
                pass

            def set_message(self, message):
                self.message = message

        class Backend:
            def listdir(self, path):
                if path == "/docs":
                    return [RemoteEntry("guide.txt", "/docs/guide.txt",
                                        False, 1536)]
                return []

        async def scenario():
            view = NetworkView(App())
            view.backend = Backend()
            view.path = "/"
            view.entries = [RemoteEntry("docs", "/docs", True)]
            view._children = {"/": list(view.entries)}
            view.toggle_expand()
            while view.busy:
                await asyncio.sleep(0.01)
            return view

        view = asyncio.run(scenario())
        self.assertEqual([entry.path for entry in view.entries],
                         ["/docs", "/docs/guide.txt"])
        self.assertEqual(view.entries[1].depth, 1)
        self.assertIn("/docs", view.expanded)

        view.toggle_expand()
        self.assertEqual([entry.path for entry in view.entries], ["/docs"])

    def test_remote_rows_show_size_and_explorer_cursor_style(self):
        class App:
            def invalidate(self):
                pass

        view = NetworkView(App())
        view.entries = [RemoteEntry("archive.bin", "/archive.bin",
                                    False, 1536)]

        fragments = view._text()
        rendered = "".join(text for _style, text in fragments)
        styles = [style for style, _text in fragments]

        self.assertIn("archive.bin", rendered)
        self.assertIn("1.5K", rendered)
        self.assertTrue(any("class:explorer.file" in style and
                            "reverse" in style for style in styles))

    def test_only_focused_network_pane_shows_its_cursor(self):
        class Layout:
            remote_focused = False

            def has_focus(self, control):
                return self.remote_focused

        class Application:
            layout = Layout()

        class App:
            application = Application()
            mode = "network"

            def invalidate(self):
                pass

            def network_local_focused(self):
                return not self.application.layout.remote_focused

        app = App()
        remote_view = NetworkView(app)
        remote_view.entries = [RemoteEntry("file.txt", "/file.txt", False)]
        local_view = object.__new__(ExplorerView)
        local_view.app = app
        app.explorer = local_view
        app.networkview = remote_view
        remote_view.local_view = local_view

        self.assertTrue(local_view._cursor_visible())
        self.assertFalse(any("reverse" in style
                             for style, _text in remote_view._text()))

        app.application.layout.remote_focused = True
        self.assertFalse(local_view._cursor_visible())
        self.assertTrue(any("reverse" in style
                            for style, _text in remote_view._text()))

    def test_successful_sftp_target_is_remembered_and_prefilled(self):
        class Backend:
            label = "sftp://naranicca@192.168.45.75:22"

            def listdir(self, path):
                return []

        class App:
            explorer = object()

            def set_message(self, message):
                self.message = message

            def switch_mode(self, mode):
                self.mode = mode

            def invalidate(self):
                pass

        async def scenario():
            view = NetworkView(App())
            with mock.patch("nsh.network.view.remote.connect",
                            return_value=(Backend(), "/")), mock.patch(
                    "nsh.network.view.state.set") as remember:
                view.connect("sftp", "naranicca@192.168.45.75", "secret")
                while view.busy:
                    await asyncio.sleep(0.01)
                return remember

        remember = asyncio.run(scenario())
        remember.assert_called_once_with(
            "network_sftp_target", "naranicca@192.168.45.75")

        app = object.__new__(NshApp)
        app.open_input_dialog = mock.Mock()
        with mock.patch("nsh.app.state.get",
                        return_value="naranicca@192.168.45.75"):
            app._network_target("sftp")
        self.assertEqual(app.open_input_dialog.call_args.args[1],
                         "naranicca@192.168.45.75")

    def test_remote_listing_has_parent_row_and_navigation_shortcuts(self):
        class App:
            def invalidate(self):
                pass

            def set_message(self, message):
                self.message = message

        class Backend:
            home = "/Users/naranicca"

            def listdir(self, path):
                if path == "/var/log":
                    return [RemoteEntry("a.log", "/var/log/a.log", False)]
                if path == self.home:
                    return [RemoteEntry("Desktop",
                                        "/Users/naranicca/Desktop", True)]
                return []

        async def scenario():
            view = NetworkView(App())
            view.backend = Backend()
            view.path = "/var/log"
            await view._load()
            initial = list(view.entries)
            initial_cursor = view.cursor
            view._move_to(len(view.entries) - 1)
            last_cursor = view.cursor
            view._move_to(0)
            first_cursor = view.cursor
            view.go_home()
            while view.busy:
                await asyncio.sleep(0.01)
            return (view, initial, initial_cursor,
                    first_cursor, last_cursor)

        view, initial, initial_cursor, first_cursor, last_cursor = \
            asyncio.run(scenario())
        self.assertTrue(initial[0].is_parent)
        self.assertEqual(initial[0].name, "..")
        self.assertEqual(initial[0].path, "/var")
        self.assertEqual(initial_cursor, 1)
        self.assertEqual(first_cursor, 0)
        self.assertEqual(last_cursor, 1)
        self.assertEqual(view.path, "/Users/naranicca")
        self.assertTrue(view.entries[0].is_parent)
        self.assertEqual(view.entries[1].name, "Desktop")

    def test_remote_sort_matches_explorer_groups_and_keeps_cursor(self):
        class App:
            settings = {"sort": "name", "sort_reverse": "false"}

            def invalidate(self):
                pass

        view = NetworkView(App())
        view.path = "/"
        view._children = {"/": [
            RemoteEntry("small.txt", "/small.txt", False, 10, 10),
            RemoteEntry("folder", "/folder", True, 0, 30),
            RemoteEntry("large.bin", "/large.bin", False, 100, 20),
        ]}
        view.entries = view._flatten("/")
        view.cursor = 0

        view.set_sort("size", reverse=True)

        self.assertEqual([entry.name for entry in view.entries],
                         ["folder", "large.bin", "small.txt"])
        self.assertEqual(view.current().name, "folder")

    def test_remote_fuzzy_search_indexes_tree_and_opens_result(self):
        class Backend:
            def listdir(self, path):
                return {
                    "/work": [
                        RemoteEntry("docs", "/work/docs", True),
                        RemoteEntry("readme.md", "/work/readme.md", False),
                    ],
                    "/work/docs": [
                        RemoteEntry("guide.txt", "/work/docs/guide.txt", False),
                    ],
                }.get(path, [])

        class Layout:
            def focus(self, control):
                self.focused = control

        class Application:
            layout = Layout()

        class App:
            settings = {}
            application = Application()

            def invalidate(self):
                pass

            def set_message(self, message):
                self.message = message

            def switch_mode(self, mode):
                self.mode = mode

        async def scenario():
            app = App()
            view = NetworkView(app)
            view.backend = Backend()
            view.path = "/work"
            await view._load()
            immediate = view.search_candidates()
            indexed = view.gather_search_candidates()
            view.open_search_result("docs/guide.txt")
            while view.busy:
                await asyncio.sleep(0.01)
            return app, view, immediate, indexed

        app, view, immediate, indexed = asyncio.run(scenario())
        self.assertEqual(immediate, ["docs/", "readme.md"])
        self.assertEqual(indexed,
                         ["docs/", "docs/guide.txt", "readme.md"])
        self.assertEqual(view.path, "/work/docs")
        self.assertEqual(view.current().name, "guide.txt")
        self.assertEqual(app.mode, "network")

    def test_remote_search_accepts_visible_result_while_indexing(self):
        class RemoteView:
            def open_search_result(self, relative):
                self.opened = relative

        class App:
            def invalidate(self):
                pass

        view = SearchView(App())
        remote = RemoteView()
        view.remote_view = remote
        view.loading = True
        view.results = [("docs/", 100, [])]
        view.cursor = 0

        view.accept()

        self.assertEqual(remote.opened, "docs/")


if __name__ == "__main__":
    unittest.main()
