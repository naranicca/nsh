import unittest
from types import SimpleNamespace

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.history import InMemoryHistory

from nsh.app import NshApp
from nsh.network.shell import RemoteShellView


class ShellPasteTests(unittest.TestCase):
    def make_app(self, shell):
        app = NshApp.__new__(NshApp)
        app.shells = SimpleNamespace(current=lambda: shell)
        app.remote_shell = shell
        app.invalidate = lambda: None
        return app

    def make_shell(self):
        return SimpleNamespace(
            command_buffer=Buffer(history=InMemoryHistory()),
            command_window=SimpleNamespace(horizontal_scroll=9),
            pending=[],
            busy=lambda: False,
        )

    def test_single_line_paste_only_inserts_text(self):
        shell = self.make_shell()
        app = self.make_app(shell)
        app.run_shell_batch = lambda *_args: self.fail("single line was run")

        app.paste_shell_text("echo one")

        self.assertEqual("echo one", shell.command_buffer.text)

    def test_multiline_local_paste_combines_input_and_normalizes_crlf(self):
        shell = self.make_shell()
        shell.command_buffer.text = "echo prepost"
        shell.command_buffer.cursor_position = len("echo pre")
        app = self.make_app(shell)
        batches = []
        app.run_shell_batch = lambda _shell, commands: batches.append(commands)

        app.paste_shell_text("fix\r\necho two\r\n")

        self.assertEqual([["echo prefix", "echo two", "post"]], batches)
        self.assertEqual("", shell.command_buffer.text)
        self.assertEqual(0, shell.command_window.horizontal_scroll)

    def test_local_batch_queues_all_commands_when_busy(self):
        shell = self.make_shell()
        shell.busy = lambda: True
        shell.pending.append("waiting")
        app = self.make_app(shell)

        app.run_shell_batch(shell, ["one", "", "two"])

        self.assertEqual(["waiting", "one", "two"], shell.pending)

    def test_local_batch_queues_tail_before_starting_first(self):
        shell = self.make_shell()
        app = self.make_app(shell)
        observed = []
        app._dispatch_command = lambda _shell, command: observed.append(
            (command, list(shell.pending)))

        app.run_shell_batch(shell, ["one", "two", "three"])

        self.assertEqual([("one", ["two", "three"])], observed)

    def test_remote_batch_queues_tail_before_starting_first(self):
        remote = RemoteShellView.__new__(RemoteShellView)
        remote.busy = False
        remote.pending = []
        remote.app = SimpleNamespace(invalidate=lambda: None)
        observed = []
        remote.run = lambda command: observed.append(
            (command, list(remote.pending)))

        remote.run_batch(["one", "two", "three"])

        self.assertEqual([("one", ["two", "three"])], observed)

    def test_remote_batch_queues_all_commands_when_busy(self):
        remote = RemoteShellView.__new__(RemoteShellView)
        remote.busy = True
        remote.pending = ["waiting"]
        remote.app = SimpleNamespace(invalidate=lambda: None)

        remote.run_batch(["one", "", "two"])

        self.assertEqual(["waiting", "one", "two"], remote.pending)


if __name__ == "__main__":
    unittest.main()
