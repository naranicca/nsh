import asyncio
import stat
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from prompt_toolkit.data_structures import Point

from nsh.app import NshApp, PANE_SEPARATOR, _safe_status_message
from nsh.explorer.view import ExplorerView
from nsh.network.backend import (
    HostKeyRequired, RemoteBackend, RemoteEntry, SFTPBackend,
    has_configured_proxy, parse_target)
from nsh.network.view import NetworkView
from nsh.network.shell import RemoteShellView
from nsh.search.view import SearchView
from nsh.shell.view import ShellView


class FakeBackend(RemoteBackend):
    def __init__(self, names):
        super().__init__("example.com", 22, "user")
        self.names = names

    def listdir(self, path):
        return [RemoteEntry(name, path + "/" + name, False) for name in self.names]


class NetworkBackendTests(unittest.TestCase):
    def test_status_message_sanitizes_multiline_terminal_error(self):
        message = "proxy failed\r\n\x1b[31mconnection refused\x1b[0m\t(detail)"

        self.assertEqual(
            _safe_status_message(message),
            "proxy failed connection refused (detail)",
        )

    def test_set_message_stores_only_safe_single_line_text(self):
        app = object.__new__(NshApp)
        app.invalidate = mock.Mock()

        app.set_message("connection failed:\nserver closed\x07")

        self.assertEqual(app.message, "connection failed: server closed")
        app.invalidate.assert_called_once_with()

    def test_escape_clears_local_shell_input_before_leaving(self):
        buffer = SimpleNamespace(text="unfinished", reset=mock.Mock())
        local = SimpleNamespace(
            command_buffer=buffer,
            command_window=SimpleNamespace(horizontal_scroll=20))
        app = object.__new__(NshApp)
        app.shells = SimpleNamespace(current=lambda: local)
        app.invalidate = mock.Mock()
        app.leave_shell = mock.Mock()

        app.shell_escape()

        buffer.reset.assert_called_once_with()
        self.assertEqual(local.command_window.horizontal_scroll, 0)
        app.leave_shell.assert_not_called()

        buffer.text = ""
        app.shell_escape()
        app.leave_shell.assert_called_once_with()

    def test_escape_clears_remote_shell_input_before_returning_to_files(self):
        buffer = SimpleNamespace(text="unfinished", reset=mock.Mock())
        remote = SimpleNamespace(
            command_buffer=buffer,
            command_window=SimpleNamespace(horizontal_scroll=20))
        app = object.__new__(NshApp)
        app.remote_shell = remote
        app.invalidate = mock.Mock()
        app.switch_mode = mock.Mock()

        app.shell_escape(remote=True)

        buffer.reset.assert_called_once_with()
        app.switch_mode.assert_not_called()

        buffer.text = ""
        app.shell_escape(remote=True)
        app.switch_mode.assert_called_once_with("network")

    def test_two_pane_and_network_use_single_cell_double_separator(self):
        self.assertEqual(PANE_SEPARATOR, "║")
        self.assertEqual(len(PANE_SEPARATOR), 1)

    def test_network_title_uses_blank_at_pane_split(self):
        app = object.__new__(NshApp)
        session = SimpleNamespace(mode="network")
        app.shells = SimpleNamespace(current=lambda: session)
        app.networkview = SimpleNamespace(
            local_view=SimpleNamespace(cwd=Path("C:/local")),
            location="sftp://host/remote")
        clock = [("class:titlebar.clock", " 12:34:56 ")]

        fragments = app._network_title("class:titlebar.name", clock, 81)
        rendered = "".join(text for _style, text in fragments)

        self.assertNotIn("│", rendered)
        self.assertEqual(rendered[81 // 2], " ")
        self.assertEqual(sum(len(text) for _style, text in fragments), 81)

    def test_two_pane_title_colors_both_paths_cyan(self):
        app = object.__new__(NshApp)
        session = SimpleNamespace(mode="explorer", active_pane=0)
        app.shells = SimpleNamespace(current=lambda: session)
        session.explorers = [
            SimpleNamespace(cwd=Path("C:/left"), git_status=None, selected=set()),
            SimpleNamespace(cwd=Path("C:/right"), git_status=None, selected=set()),
        ]
        clock = [("class:titlebar.clock", " 12:34:56 ")]
        output = SimpleNamespace(get_size=lambda: SimpleNamespace(columns=100))

        with mock.patch("nsh.app.get_app",
                        return_value=SimpleNamespace(output=output)):
            fragments = app._two_pane_title("class:titlebar.name", clock)

        path_texts = [text for style, text in fragments
                      if style == "class:titlebar.path"]
        self.assertTrue(any("left" in text for text in path_texts))
        self.assertTrue(any("right" in text for text in path_texts))
        self.assertNotIn("▸", "".join(text for _style, text in fragments))

    def test_f10_network_item_becomes_disconnect_while_connected(self):
        app = object.__new__(NshApp)
        disconnect = mock.Mock()
        app.networkview = SimpleNamespace(connected=True, disconnect=disconnect)
        app.open_menu = mock.Mock()

        app.open_nsh_menu()

        items = app.open_menu.call_args.args[1]
        labels = [label for label, _callback in items]
        self.assertNotIn("Network", labels)
        self.assertIn("Network: Disconnect", labels)
        callback = next(callback for label, callback in items
                        if label == "Network: Disconnect")
        callback()
        disconnect.assert_called_once_with()

    def test_f10_shows_network_item_while_disconnected(self):
        app = object.__new__(NshApp)
        app.networkview = SimpleNamespace(connected=False)
        app.open_network_menu = mock.Mock()
        app.open_menu = mock.Mock()

        app.open_nsh_menu()

        items = app.open_menu.call_args.args[1]
        labels = [label for label, _callback in items]
        self.assertIn("Network", labels)
        self.assertNotIn("Network: Disconnect", labels)

    def test_shift_l_in_network_only_focuses_remote_pane(self):
        app = SimpleNamespace(
            mode="network", two_pane=False,
            focus_network_pane=mock.Mock(),
            open_dir_in_two_pane=mock.Mock())

        NshApp.move_pane_focus(app, 1)

        app.focus_network_pane.assert_called_once_with(1)
        app.open_dir_in_two_pane.assert_not_called()

    def test_shift_h_in_network_makes_local_cursor_visible(self):
        local_control = object()

        class Layout:
            focused = None

            def focus(self, control):
                self.focused = control

            def has_focus(self, control):
                return self.focused is control

        app = object.__new__(NshApp)
        session = SimpleNamespace(mode="network")
        app.shells = SimpleNamespace(current=lambda: session)
        app.mode = "network"
        app.application = SimpleNamespace(layout=Layout())
        app.networkview = SimpleNamespace(
            local_view=SimpleNamespace(control=local_control),
            control=object())
        app.invalidate = mock.Mock()

        app.focus_network_pane(-1)

        self.assertTrue(app.network_local_focused())
        self.assertEqual(app._network_pane_direction(), -1)
        app.invalidate.assert_called_once_with()

    def test_shift_l_in_existing_two_pane_only_focuses_right_pane(self):
        app = SimpleNamespace(
            mode="explorer", two_pane=True, active_pane=0,
            switch_to_pane=mock.Mock(), open_dir_in_two_pane=mock.Mock())

        NshApp.move_pane_focus(app, 1)

        app.switch_to_pane.assert_called_once_with(1)
        app.open_dir_in_two_pane.assert_not_called()

    def test_shift_l_opens_cursor_directory_only_in_single_pane(self):
        entry = SimpleNamespace(path=Path("folder"), is_dir=True,
                                is_parent=False)
        app = SimpleNamespace(
            mode="explorer", two_pane=False,
            preview_focused=mock.Mock(return_value=False),
            explorer=SimpleNamespace(current=lambda: entry),
            open_dir_in_two_pane=mock.Mock())

        NshApp.move_pane_focus(app, 1)

        app.open_dir_in_two_pane.assert_called_once_with(entry.path)

    def test_local_search_from_network_restores_network_left_pane(self):
        app = SimpleNamespace(
            mode="network", picker=False, search_result=None,
            switch_mode=mock.Mock(), focus_network_pane=mock.Mock(),
            set_cwd=mock.Mock(),
            explorer=SimpleNamespace(refresh_listing=mock.Mock()))

        NshApp.enter_search(app, "photo")
        self.assertEqual(app._search_return, "network")
        self.assertFalse(app._search_remote)

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "photo.jpg"
            path.write_bytes(b"")
            NshApp.search_select(app, path)

        app.switch_mode.assert_called_with("network")
        app.focus_network_pane.assert_called_once_with(-1)
        app.explorer.refresh_listing.assert_called_once_with(
            select_name="photo.jpg")

    def test_cancel_remote_search_restores_network_remote_pane(self):
        app = SimpleNamespace(
            picker=False, _search_return="network", _search_remote=True,
            switch_mode=mock.Mock(), focus_network_pane=mock.Mock())

        NshApp.cancel_search(app)

        app.switch_mode.assert_called_once_with("network")
        app.focus_network_pane.assert_called_once_with(1)

    def test_notes_and_system_restore_originating_network_pane(self):
        notes_app = SimpleNamespace(
            mode="network", _network_pane_direction=lambda: -1,
            switch_mode=mock.Mock(), focus_network_pane=mock.Mock(),
            notesview=SimpleNamespace(
                input=SimpleNamespace(text=""), save_note=mock.Mock()))
        NshApp.open_notes(notes_app)
        NshApp.leave_notes(notes_app)
        self.assertEqual(notes_app._notes_return, "network")
        notes_app.switch_mode.assert_called_with("network")
        notes_app.focus_network_pane.assert_called_once_with(-1)

        system_app = SimpleNamespace(
            mode="network", _network_pane_direction=lambda: 1,
            switch_mode=mock.Mock(), focus_network_pane=mock.Mock())
        NshApp.open_system(system_app)
        NshApp.close_system(system_app)
        self.assertEqual(system_app._system_return, "network")
        system_app.focus_network_pane.assert_called_once_with(1)

    def test_preferences_and_grep_shell_restore_network_pane(self):
        preferences_app = SimpleNamespace(
            mode="network", _network_pane_direction=lambda: -1,
            switch_mode=mock.Mock(), focus_network_pane=mock.Mock())
        with mock.patch("nsh.app.config.ensure_default_config"):
            NshApp.open_preferences(preferences_app)
        NshApp.close_preferences(preferences_app)
        self.assertEqual(preferences_app._preferences_return, "network")
        preferences_app.focus_network_pane.assert_called_once_with(-1)

        grep_app = SimpleNamespace(
            _find_return="network", _find_return_pane=-1,
            switch_mode=mock.Mock(), run_in_shell=mock.Mock(), shell=object())
        NshApp._run_grep(grep_app, "needle", False, False)
        self.assertEqual(grep_app._shell_return, "network")
        self.assertEqual(grep_app._shell_return_pane, -1)
        grep_app.run_in_shell.assert_called_once()

    def test_git_and_log_restore_originating_network_pane(self):
        git_app = SimpleNamespace(
            mode="network",
            git_status=SimpleNamespace(is_repo=True),
            _network_pane_direction=lambda: -1,
            switch_mode=mock.Mock(), focus_network_pane=mock.Mock())
        NshApp.toggle_git_mode(git_app)
        self.assertEqual(git_app._git_return, "network")
        git_app.mode = "git"
        NshApp.close_git(git_app)
        git_app.focus_network_pane.assert_called_once_with(-1)

        log_app = SimpleNamespace(
            mode="network",
            git_status=SimpleNamespace(is_repo=True),
            logview=SimpleNamespace(path_filters=()),
            _network_pane_direction=lambda: 1,
            switch_mode=mock.Mock(), focus_network_pane=mock.Mock())
        NshApp.open_log(log_app)
        self.assertEqual(log_app._log_return, "network")
        NshApp.close_log(log_app)
        log_app.focus_network_pane.assert_called_once_with(1)

    def test_dialog_close_restores_exact_originating_pane_control(self):
        local_control = object()
        layout = SimpleNamespace(
            current_control=local_control, focus=mock.Mock())
        app = SimpleNamespace(
            application=SimpleNamespace(layout=layout),
            _dialog_return_focus=None,
            _restore_focus=mock.Mock(), invalidate=mock.Mock())

        NshApp._capture_dialog_focus(app)
        layout.current_control = object()
        NshApp._dialog_closed(app)

        layout.focus.assert_called_once_with(local_control)
        app._restore_focus.assert_not_called()

    def test_sftp_directory_symlink_is_navigable_and_broken_link_is_marked(self):
        backend = SFTPBackend("host", 22, "user")

        class Client:
            def listdir_attr(self, path):
                return [
                    SimpleNamespace(filename="docs", st_mode=stat.S_IFLNK,
                                    st_size=4, st_mtime=10),
                    SimpleNamespace(filename="missing", st_mode=stat.S_IFLNK,
                                    st_size=7, st_mtime=20),
                ]

            def readlink(self, path):
                return "shared/docs" if path.endswith("docs") else "gone"

            def stat(self, path):
                if path.endswith("missing"):
                    raise IOError("not found")
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_size=4096)

        backend.client = Client()
        entries = backend.listdir("/home/user")

        docs = next(entry for entry in entries if entry.name == "docs")
        missing = next(entry for entry in entries if entry.name == "missing")
        self.assertTrue(docs.is_symlink)
        self.assertTrue(docs.is_dir)
        self.assertEqual(docs.link_target, "shared/docs")
        self.assertFalse(docs.is_broken)
        self.assertTrue(missing.is_symlink)
        self.assertFalse(missing.is_dir)
        self.assertTrue(missing.is_broken)

    def test_recursive_delete_unlinks_directory_symlink_without_following_it(self):
        class Backend(RemoteBackend):
            def __init__(self):
                super().__init__("host", 22, "user")
                self.calls = []

            def listdir(self, path):
                self.calls.append(("list", path))
                return [RemoteEntry("linked", path + "/linked", True,
                                    is_symlink=True)]

            def remove(self, path):
                self.calls.append(("remove", path))

            def rmdir(self, path):
                self.calls.append(("rmdir", path))

        backend = Backend()
        backend.remove_tree("/root")

        self.assertEqual(backend.calls, [
            ("list", "/root"), ("remove", "/root/linked"),
            ("rmdir", "/root")])

    def test_remote_search_lists_but_does_not_follow_directory_symlink(self):
        class Backend:
            def __init__(self):
                self.visited = []

            def listdir(self, path):
                self.visited.append(path)
                return [RemoteEntry("loop", path + "/loop", True,
                                    is_symlink=True)]

        backend = Backend()
        view = object.__new__(NetworkView)
        view.path = "/root"
        view.backend = backend
        view._backend_call = lambda fn, *args: fn(*args)

        results = view.gather_search_candidates()

        self.assertEqual(results, ["loop/"])
        self.assertEqual(backend.visited, ["/root"])

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

    def test_sftp_uses_proxycommand_from_ssh_config(self):
        clients = []
        proxies = []

        class Proxy:
            def __init__(self, command):
                self.command = command
                self.closed = False
                proxies.append(self)

            def close(self):
                self.closed = True

        class Client:
            def __init__(self):
                clients.append(self)

            def load_system_host_keys(self):
                pass

            def set_missing_host_key_policy(self, policy):
                pass

            def connect(self, hostname, **kwargs):
                self.connect_args = (hostname, kwargs)

            def open_sftp(self):
                class SFTP:
                    def normalize(self, path):
                        return "/home/proxy-user"

                    def close(self):
                        pass
                return SFTP()

            def close(self):
                pass

        with TemporaryDirectory() as temp:
            home = Path(temp)
            ssh_dir = home / ".ssh"
            ssh_dir.mkdir()
            (ssh_dir / "config").write_text(
                "Host proxied\n"
                "  HostName internal.example\n"
                "  User proxy-user\n"
                "  Port 2222\n"
                "  ProxyCommand ssh -W %h:%p gateway-%r\n",
                encoding="utf-8")
            with mock.patch("paramiko.SSHClient", Client), mock.patch(
                    "paramiko.ProxyCommand", Proxy), mock.patch(
                    "nsh.network.backend.Path.home", return_value=home):
                backend = SFTPBackend.connect("proxied", 22, "", "secret")

        self.assertEqual(proxies[0].command,
                         "ssh -W internal.example:2222 gateway-proxy-user")
        self.assertIs(clients[0].connect_args[1]["sock"], proxies[0])
        self.assertEqual(clients[0].connect_args[0], "internal.example")
        backend.close()
        self.assertTrue(proxies[0].closed)

    def test_sftp_finds_proxycommand_alias_by_configured_hostname(self):
        clients = []
        proxies = []

        class Proxy:
            def __init__(self, command):
                self.command = command
                proxies.append(self)

            def close(self):
                pass

        class Client:
            def __init__(self):
                clients.append(self)

            def load_system_host_keys(self):
                pass

            def set_missing_host_key_policy(self, policy):
                pass

            def connect(self, hostname, **kwargs):
                self.connect_args = (hostname, kwargs)

            def open_sftp(self):
                return SimpleNamespace(normalize=lambda path: "/home/user",
                                       close=lambda: None)

            def close(self):
                pass

        with TemporaryDirectory() as temp:
            home = Path(temp)
            ssh_dir = home / ".ssh"
            ssh_dir.mkdir()
            (ssh_dir / "config").write_text(
                "Host office-mac\n"
                "  HostName 192.168.45.75\n"
                "  User naranicca\n"
                "  ProxyCommand ssh -W %h:%p gateway\n",
                encoding="utf-8")
            with mock.patch("paramiko.SSHClient", Client), mock.patch(
                    "paramiko.ProxyCommand", Proxy), mock.patch(
                    "nsh.network.backend.Path.home", return_value=home):
                backend = SFTPBackend.connect(
                    "192.168.45.75", 22, "naranicca", "secret")

        self.assertEqual(proxies[0].command,
                         "ssh -W 192.168.45.75:22 gateway")
        self.assertIs(clients[0].connect_args[1]["sock"], proxies[0])
        backend.close()

    def test_configured_proxy_is_detected_for_alias_and_hostname(self):
        with TemporaryDirectory() as temp:
            home = Path(temp)
            ssh_dir = home / ".ssh"
            ssh_dir.mkdir()
            (ssh_dir / "config").write_text(
                "Host office-mac\n"
                "  HostName 192.168.45.75\n"
                "  ProxyCommand ssh -W %h:%p gateway\n"
                "Host direct\n"
                "  HostName direct.example\n",
                encoding="utf-8")
            with mock.patch("nsh.network.backend.Path.home", return_value=home):
                self.assertTrue(has_configured_proxy("office-mac"))
                self.assertTrue(has_configured_proxy("192.168.45.75"))
                self.assertFalse(has_configured_proxy("direct"))

    def test_configured_proxy_skips_jump_host_dialog(self):
        app = object.__new__(NshApp)
        app._network_password = mock.Mock()
        app._network_jump = mock.Mock()

        with mock.patch("nsh.app.remote.has_configured_proxy",
                        return_value=True):
            app._network_sftp_route("office-mac")

        app._network_password.assert_called_once_with("sftp", "office-mac")
        app._network_jump.assert_not_called()

    def test_direct_sftp_target_still_opens_jump_host_dialog(self):
        app = object.__new__(NshApp)
        app._network_password = mock.Mock()
        app._network_jump = mock.Mock()

        with mock.patch("nsh.app.remote.has_configured_proxy",
                        return_value=False):
            app._network_sftp_route("direct")

        app._network_jump.assert_called_once_with("direct")
        app._network_password.assert_not_called()

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

    def test_remote_c_downloads_p_is_unbound_and_q_quits(self):
        class App:
            settings = {}

            def exit(self):
                self.exited = True

        app = App()
        view = NetworkView(app)
        view.download = mock.Mock()
        bindings = view._keys()

        bindings.get_bindings_for_keys(("c",))[0].handler(None)
        bindings.get_bindings_for_keys(("q",))[0].handler(None)

        view.download.assert_called_once_with()
        self.assertTrue(app.exited)
        self.assertEqual(bindings.get_bindings_for_keys(("p",)), [])

    def test_remote_colon_opens_ssh_shell(self):
        class App:
            settings = {}

            def open_remote_shell(self):
                self.opened = True

        app = App()
        view = NetworkView(app)
        view._keys().get_bindings_for_keys((":",))[0].handler(None)
        self.assertTrue(app.opened)

    def test_remote_shell_can_open_during_background_indexing(self):
        class Backend:
            def execute(self):
                pass

        class Network:
            backend = Backend()
            busy = False
            indexing = True

        app = object.__new__(NshApp)
        app.networkview = Network()
        app.switch_mode = mock.Mock()
        app.set_message = mock.Mock()

        app.open_remote_shell()

        app.switch_mode.assert_called_once_with("remote-shell")
        app.set_message.assert_not_called()

    def test_local_shell_queues_immediately_while_command_is_running(self):
        class Session:
            pending = ["already queued"]

            @staticmethod
            def busy():
                return True

        app = object.__new__(NshApp)
        app.invalidate = mock.Mock()
        app._dispatch_command = mock.Mock()
        session = Session()

        app.run_in_shell(session, "next command")

        self.assertEqual(session.pending, ["already queued", "next command"])
        app._dispatch_command.assert_not_called()
        app.invalidate.assert_called_once_with()

    def test_remote_shell_executes_in_file_view_directory(self):
        class Backend:
            label = "sftp://user@example:22"
            home = "/home/user"

            def execute(self, directory, command):
                self.executed = (directory, command)
                return "hello\n", "", 0

            def listdir(self, path):
                return []

        class App:
            settings = {}

            def invalidate(self):
                pass

            def set_message(self, message):
                self.message = message

            def switch_mode(self, mode):
                self.mode = mode

        async def scenario():
            app = App()
            view = NetworkView(app)
            backend = Backend()
            view.backend = backend
            view.path = "/srv/project"
            app.networkview = view
            shell = RemoteShellView(app)
            shell.run("pwd")
            while shell.busy:
                await asyncio.sleep(0.01)
            return shell, backend

        shell, backend = asyncio.run(scenario())
        self.assertEqual(backend.executed, ("/srv/project", "pwd"))
        rendered = "\n".join(
            "".join(text for _style, text in line) for line in shell.lines)
        self.assertIn("hello", rendered)

        shell.command_buffer.text = "x" * 200
        prompt_fragments = shell._prompt_text()
        prompt = "".join(text for _style, text in prompt_fragments)
        self.assertTrue(prompt.endswith("$"))
        self.assertEqual(prompt_fragments[-1][0], "")
        self.assertEqual(
            ShellView._right_fit_fragments(
                [("class:explorer.dir", "/very/long/path "), ("", "$")], 1),
            [("", "$")],
        )
        shell.command_window.horizontal_scroll = 80
        shell.command_buffer.text = "short"
        shell.command_buffer.cursor_position = len("short")
        shell._reset_stale_input_scroll(shell.command_buffer)
        self.assertEqual(shell.command_window.horizontal_scroll, 0)

    def test_remote_shell_queues_commands_and_shows_elapsed_time(self):
        class Backend:
            label = "sftp://user@example:22"

            def __init__(self):
                self.commands = []

            def execute(self, directory, command):
                self.commands.append(command)
                return command + "\n", "", 0

        class App:
            settings = {}

            def invalidate(self):
                pass

            def set_message(self, message):
                self.message = message

            def switch_mode(self, mode):
                self.mode = mode

        async def scenario():
            app = App()
            view = NetworkView(app)
            backend = Backend()
            view.backend = backend
            view.path = "/srv"
            app.networkview = view
            shell = RemoteShellView(app)
            shell.run("first")
            shell.run("second")
            shell.run("third")
            queue_fragments = shell._queue_text()
            queued = "".join(text for _style, text in queue_fragments)
            running_prompt = shell._prompt_text()
            while shell.busy or shell.pending:
                await asyncio.sleep(0.01)
            return shell, backend, queued, queue_fragments, running_prompt

        shell, backend, queued, queue_fragments, running_prompt = \
            asyncio.run(scenario())
        self.assertEqual(backend.commands, ["first", "second", "third"])
        self.assertIn("$ second", queued)
        self.assertIn("$ third", queued)
        self.assertTrue(any(style == "class:shell.elapsed"
                            for style, _text in queue_fragments))
        self.assertTrue(any(style == "class:shell.prompt.dim"
                            for style, _text in running_prompt))
        self.assertEqual(shell.pending, [])
        self.assertEqual(shell._last_result[1], 0)
        finished_prompt = shell._prompt_text()
        self.assertTrue(any(style == "class:shell.elapsed.ok"
                            for style, _text in finished_prompt))

    def test_sftp_download_is_enqueued_in_remote_shell(self):
        remote_shell = SimpleNamespace(enqueue_transfer=mock.Mock())
        local = SimpleNamespace(cwd=Path("local"), refresh=mock.Mock())
        app = SimpleNamespace(
            explorer=local, remote_shell=remote_shell,
            open_remote_shell=mock.Mock(), invalidate=mock.Mock())
        view = NetworkView(app)
        view.backend = SimpleNamespace(protocol="sftp")
        view.local_view = local
        view.entries = [RemoteEntry("photo.jpg", "/photo.jpg", False)]
        view.cursor = 0

        view.download()

        remote_shell.enqueue_transfer.assert_called_once()
        self.assertIn("download /photo.jpg", remote_shell.enqueue_transfer.call_args.args[0])
        app.open_remote_shell.assert_called_once_with()

    def test_enter_on_sftp_file_opens_preview_instead_of_downloading(self):
        entry = RemoteEntry("notes.txt", "/notes.txt", False, size=12)
        view = object.__new__(NetworkView)
        view.backend = SimpleNamespace(protocol="sftp", read_preview=mock.Mock())
        view.current = lambda: entry
        view.preview_file = mock.Mock()
        view.download = mock.Mock()

        view.open()

        view.preview_file.assert_called_once_with(entry)
        view.download.assert_not_called()

    def test_enter_on_ftp_file_keeps_download_behavior(self):
        entry = RemoteEntry("notes.txt", "/notes.txt", False, size=12)
        view = object.__new__(NetworkView)
        view.backend = SimpleNamespace(protocol="ftp")
        view.current = lambda: entry
        view.preview_file = mock.Mock()
        view.download = mock.Mock()

        view.open()

        view.download.assert_called_once_with()
        view.preview_file.assert_not_called()

    def test_remote_preview_renders_text_and_escape_returns_to_listing(self):
        entry = RemoteEntry("notes.txt", "/notes.txt", False, size=11)
        app = SimpleNamespace(invalidate=mock.Mock())
        view = object.__new__(NetworkView)
        view.app = app
        view.window = SimpleNamespace(render_info=None)
        view._preview_entry = entry
        view._preview_data = b"hello\nworld"
        view._preview_error = None
        view._preview_loading = False
        view._preview_scroll = 0
        view._preview_token = object()

        rendered = "".join(text for _style, text in view._preview_text())
        self.assertIn("notes.txt", rendered)
        self.assertIn("hello", rendered)
        self.assertIn("world", rendered)
        view.cursor = 500
        view._top = 0
        self.assertEqual(view._cursor_position(), Point(0, 0))

        view.cancel()
        self.assertIsNone(view._preview_entry)
        app.invalidate.assert_called_once_with()

    def test_binary_remote_preview_keeps_cursor_within_content(self):
        entry = RemoteEntry("photo.jpg", "/photo.jpg", False, size=1024)
        view = object.__new__(NetworkView)
        view.window = SimpleNamespace(render_info=None)
        view._preview_entry = entry
        view._preview_data = b"\xff\xd8\x00\x10JFIF"
        view._preview_error = None
        view._preview_loading = False
        view._preview_scroll = 0
        view.cursor = 100
        view._top = 0

        rendered = "".join(text for _style, text in view._preview_text())

        self.assertIn("binary file", rendered)
        self.assertEqual(view._cursor_position(), Point(0, 0))
        pdf = RemoteEntry("manual.pdf", "/manual.pdf", False, size=20)
        self.assertTrue(NetworkView._is_binary_preview(
            pdf, b"%PDF-1.7 mostly ascii"))

    def test_remote_preview_reads_bounded_data_in_worker(self):
        class Backend:
            protocol = "sftp"

            def read_preview(self, path, limit):
                self.request = (path, limit)
                return b"remote text"

        class App:
            settings = {}

            def invalidate(self):
                pass

        async def scenario():
            view = NetworkView(App())
            backend = Backend()
            view.backend = backend
            entry = RemoteEntry("notes.txt", "/notes.txt", False, size=11)
            view.preview_file(entry)
            while view.busy:
                await asyncio.sleep(0.001)
            return view, backend

        view, backend = asyncio.run(scenario())
        self.assertEqual(backend.request,
                         ("/notes.txt", 256 * 1024))
        self.assertEqual(view._preview_data, b"remote text")

    def test_binary_preview_downloads_modally_opens_and_cleans_private_temp(self):
        class Backend:
            protocol = "sftp"

            def read_preview(self, _path, _limit):
                return b"\xff\xd8\x00JFIF"

            def download(self, _remote, local, callback=None):
                if callback:
                    callback(8, 8)
                Path(local).write_bytes(b"jpegdata")

        class App:
            settings = {}

            def __init__(self):
                self.opened = None
                self.progress = []

            def invalidate(self):
                pass

            def open_progress_dialog(self, title, label, on_cancel):
                self.progress.append(("open", title, label))
                self.cancel = on_cancel

            def update_progress_dialog(self, done, total):
                self.progress.append(("update", done, total))

            def close_progress_dialog(self):
                self.progress.append(("close",))

            def open_file(self, path):
                self.opened = Path(path)

            def set_message(self, message):
                self.message = message

        async def scenario():
            app = App()
            view = NetworkView(app)
            view.backend = Backend()
            view.preview_file(RemoteEntry(
                "photo.jpg", "/photo.jpg", False, size=8))
            while view.busy:
                await asyncio.sleep(0.001)
            await asyncio.sleep(0)
            return app, view

        app, view = asyncio.run(scenario())
        target = app.opened
        self.assertIsNotNone(target)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"jpegdata")
        self.assertEqual(app.progress[0][0], "open")
        self.assertIn(("update", 8, 8), app.progress)
        self.assertEqual(app.progress[-1], ("close",))
        temp_root = target.parent
        view._cleanup_temp()
        self.assertFalse(temp_root.exists())

    def test_binary_preview_cancel_does_not_open_file(self):
        class Backend:
            protocol = "sftp"

            def read_preview(self, _path, _limit):
                return b"\x00binary"

            def download(self, _remote, _local, callback=None):
                callback(1, 10)

        class App:
            settings = {}

            def invalidate(self):
                pass

            def open_progress_dialog(self, _title, _label, on_cancel):
                on_cancel()

            def update_progress_dialog(self, _done, _total):
                pass

            def close_progress_dialog(self):
                self.closed = True

            def open_file(self, _path):
                self.opened = True

            def set_message(self, message):
                self.message = message

        async def scenario():
            app = App()
            view = NetworkView(app)
            view.backend = Backend()
            view.preview_file(RemoteEntry(
                "data.bin", "/data.bin", False, size=10))
            while view.busy:
                await asyncio.sleep(0.001)
            return app, view

        app, view = asyncio.run(scenario())
        self.assertFalse(hasattr(app, "opened"))
        self.assertEqual(app.message, "preview download cancelled")
        self.assertTrue(app.closed)
        view._cleanup_temp()

    def test_preview_temp_cleanup_is_isolated_per_nsh_instance(self):
        app = SimpleNamespace(settings={}, invalidate=lambda: None)
        first, second = NetworkView(app), NetworkView(app)
        first._temp_dir = TemporaryDirectory(
            prefix="nsh-remote-preview-", ignore_cleanup_errors=True)
        second._temp_dir = TemporaryDirectory(
            prefix="nsh-remote-preview-", ignore_cleanup_errors=True)
        first_file = Path(first._temp_dir.name) / "first.bin"
        second_file = Path(second._temp_dir.name) / "second.bin"
        first_file.write_bytes(b"first")
        second_file.write_bytes(b"second")

        first._cleanup_temp()

        self.assertFalse(first_file.exists())
        self.assertTrue(second_file.exists())
        second._cleanup_temp()

    def test_local_shell_removes_only_last_queued_command(self):
        app = SimpleNamespace(invalidate=mock.Mock())
        shell = object.__new__(ShellView)
        shell.app = app
        shell.pending = ["first", "last"]

        removed = shell.remove_last_pending()

        self.assertEqual(removed, "last")
        self.assertEqual(shell.pending, ["first"])
        app.invalidate.assert_called_once_with()

    def test_remote_shell_removes_last_queued_transfer_without_interrupting(self):
        app = SimpleNamespace(invalidate=mock.Mock())
        shell = object.__new__(RemoteShellView)
        shell.app = app
        transfer = SimpleNamespace(__str__=lambda _self: "download last")
        shell.pending = ["first", transfer]
        shell.busy = True

        removed = shell.remove_last_pending()

        self.assertIs(removed, transfer)
        self.assertEqual(shell.pending, ["first"])
        self.assertTrue(shell.busy)
        app.invalidate.assert_called_once_with()

    def test_remove_last_queued_dispatches_to_active_shell_type(self):
        local = SimpleNamespace(remove_last_pending=mock.Mock(return_value="local"))
        remote = SimpleNamespace(remove_last_pending=mock.Mock(return_value="transfer"))
        app = object.__new__(NshApp)
        app.shells = SimpleNamespace(current=lambda: local)
        app.remote_shell = remote
        app.set_message = mock.Mock()

        app.mode = "shell"
        app.remove_last_queued()
        local.remove_last_pending.assert_called_once_with()
        remote.remove_last_pending.assert_not_called()

        app.mode = "remote-shell"
        app.remove_last_queued()
        remote.remove_last_pending.assert_called_once_with()
        app.set_message.assert_called_with("removed from queue: transfer")

    def test_remote_transfer_jobs_queue_and_ctrl_c_cancels_current_job(self):
        events = []

        class App:
            networkview = SimpleNamespace(location="sftp://host/dir")

            def invalidate(self):
                pass

        async def scenario():
            shell = RemoteShellView(App())

            async def first(cancel, report):
                events.append("first-start")
                report(25, 100)
                while not cancel.is_set():
                    await asyncio.sleep(0.001)
                events.append("first-cancel")

            async def second(cancel, report):
                events.append("second")
                report(100, 100)
                return "second complete"

            shell.enqueue_transfer("download first", first)
            shell.enqueue_transfer("download second", second)
            self.assertEqual([str(job) for job in shell.pending],
                             ["download second"])
            self.assertTrue(shell.interrupt())
            while shell.busy or shell.pending:
                await asyncio.sleep(0.001)
            return shell

        shell = asyncio.run(scenario())
        self.assertEqual(events, ["first-start", "first-cancel", "second"])
        self.assertEqual(shell._last_result[1], 0)

    def test_remote_transfer_progress_renders_percentage_size_and_speed(self):
        app = SimpleNamespace(invalidate=mock.Mock())
        shell = object.__new__(RemoteShellView)
        shell.app = app
        shell._transfer_token = token = object()
        shell._transfer_progress = (0, 0)
        shell._transfer_progress_started = time.monotonic() - 2

        shell._set_transfer_progress(token, 512 * 1024, 1024 * 1024)
        text = "".join(value for _style, value in shell._progress_text())

        self.assertIn("50.0%", text)
        self.assertIn("512.0 KiB", text)
        self.assertIn("1.0 MiB", text)
        self.assertIn("/s", text)
        app.invalidate.assert_called_once_with()

    def test_remote_shell_starts_split_and_maximizes_only_after_output_cap(self):
        shell = object.__new__(RemoteShellView)
        shell.lines = [[("", "short")], [("", "x" * 20)]]
        app = SimpleNamespace(
            remote_shell=shell,
            _shell_cap=lambda: 3,
            _term_cols=lambda: 10)

        self.assertEqual(shell.display_rows(10), 3)
        self.assertFalse(NshApp.remote_shell_fullscreen(app))
        self.assertEqual(NshApp.remote_shell_split_output_rows(app), 3)

        shell.lines.append([("", "one more row")])
        self.assertTrue(NshApp.remote_shell_fullscreen(app))

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

    def test_network_menu_restores_the_originating_local_pane(self):
        local_control = object()
        menu_control = object()

        class Layout:
            current_control = local_control

            def focus(self, control):
                self.current_control = control

        class Application:
            layout = Layout()

        class Menu:
            control = menu_control

            def open(self, title, items, on_close):
                self.opened = (title, items, on_close)

        app = object.__new__(NshApp)
        app.application = Application()
        app.menu = Menu()
        app._menu_float = mock.Mock()
        app._menu_return_focus = None
        app.invalidate = mock.Mock()

        app.open_menu("Sort by", [("Name", lambda: None)])
        self.assertIs(app.application.layout.current_control, menu_control)
        app._menu_closed()

        self.assertIs(app.application.layout.current_control, local_control)

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
