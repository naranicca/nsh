import asyncio
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from nsh.app import NshApp
from nsh.explorer.view import ExplorerView
from nsh.network.backend import (
    HostKeyRequired, RemoteBackend, RemoteEntry, SFTPBackend, parse_target)
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
    def test_shift_l_in_network_only_focuses_remote_pane(self):
        app = SimpleNamespace(
            mode="network", two_pane=False,
            focus_network_pane=mock.Mock(),
            open_dir_in_two_pane=mock.Mock())

        NshApp.move_pane_focus(app, 1)

        app.focus_network_pane.assert_called_once_with(1)
        app.open_dir_in_two_pane.assert_not_called()

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

            async def first(cancel):
                events.append("first-start")
                while not cancel.is_set():
                    await asyncio.sleep(0.001)
                events.append("first-cancel")

            async def second(cancel):
                events.append("second")
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
