import asyncio
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh.shell.runner import CommandRunner


class ShellRunnerTests(unittest.TestCase):
    def test_terminal_command_records_duration_and_exit_code(self):
        app = SimpleNamespace(
            cwd=Path.cwd(), shell_vars={}, sourced_files=[],
            invalidate=mock.Mock(),
        )
        runner = CommandRunner(app)

        async def run_callback(callback, in_executor=False):
            self.assertTrue(in_executor)
            callback()

        with (
            mock.patch("nsh.shell.runner.run_in_terminal", run_callback),
            mock.patch("nsh.shell.runner.subprocess.run",
                       return_value=SimpleNamespace(returncode=7)),
            mock.patch("nsh.shell.runner.prompt_fragments", return_value=[]),
            mock.patch("nsh.shell.runner._wait_for_key"),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            rc = asyncio.run(runner.run_in_term("example --interactive"))

        self.assertEqual(rc, 7)
        duration, result_rc = runner.last_result()
        self.assertGreaterEqual(duration, 0)
        self.assertEqual(result_rc, 7)
        self.assertIsNone(runner._started_at)
        app.invalidate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
