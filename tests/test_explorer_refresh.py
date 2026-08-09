import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh.explorer.model import Entry
from nsh.explorer.view import ExplorerView


def _entry(path, name="new.txt"):
    return Entry(Path(path) / name, name, False, False, False, False, 1, 1)


class ExplorerRefreshTests(unittest.TestCase):
    @staticmethod
    def _view(cwd):
        view = object.__new__(ExplorerView)
        view.app = SimpleNamespace(
            invalidate=mock.Mock(), refresh_git=mock.AsyncMock())
        view.cwd = Path(cwd)
        view.show_hidden = False
        view.sort = "name"
        view.reverse = False
        view.expanded = set()
        view.entries = []
        view.selected = set()
        view.cursor = 0
        view._signature = ()
        view._git_signature = ()
        view._watch_scan_running = False
        return view

    def test_external_refresh_runs_listing_in_worker_and_applies_change(self):
        async def scenario():
            view = self._view("one")
            listing = [_entry("one")]
            with mock.patch("nsh.explorer.view.run_in_thread",
                            mock.AsyncMock(return_value=(listing, ()))) as worker:
                changed = await view.check_external_change()
                await asyncio.sleep(0)
            return view, worker, changed

        view, worker, changed = asyncio.run(scenario())

        self.assertTrue(changed)
        worker.assert_awaited_once()
        self.assertEqual(view.entries[0].name, "new.txt")
        view.app.invalidate.assert_called_once_with()
        view.app.refresh_git.assert_awaited_once_with()

    def test_external_refresh_discards_result_after_cwd_changes(self):
        async def scan(_func, *_args):
            await asyncio.sleep(0)
            return [_entry("old")], ()

        async def scenario():
            view = self._view("old")
            with mock.patch("nsh.explorer.view.run_in_thread", scan):
                task = asyncio.create_task(view.check_external_change())
                await asyncio.sleep(0)
                view.cwd = Path("new")
                changed = await task
            return view, changed

        view, changed = asyncio.run(scenario())

        self.assertFalse(changed)
        self.assertEqual(view.entries, [])
        view.app.invalidate.assert_not_called()

    def test_external_refresh_skips_overlapping_scan(self):
        release = asyncio.Event()
        calls = 0

        async def scan(_func, *_args):
            nonlocal calls
            calls += 1
            await release.wait()
            return [], ()

        async def scenario():
            view = self._view("one")
            with mock.patch("nsh.explorer.view.run_in_thread", scan):
                first = asyncio.create_task(view.check_external_change())
                await asyncio.sleep(0)
                second = await view.check_external_change()
                release.set()
                await first
            return second

        second = asyncio.run(scenario())

        self.assertFalse(second)
        self.assertEqual(calls, 1)

    def test_git_metadata_change_refreshes_status_without_listing_change(self):
        async def scenario():
            view = self._view("one")
            listing = [_entry("one")]
            view.entries = listing
            view._signature = view._sig(listing)
            view._git_signature = ("old",)
            with mock.patch("nsh.explorer.view.run_in_thread",
                            mock.AsyncMock(return_value=(listing, ("new",)))):
                changed = await view.check_external_change()
                await asyncio.sleep(0)
            return view, changed

        view, changed = asyncio.run(scenario())

        self.assertTrue(changed)
        self.assertEqual(("new",), view._git_signature)
        view.app.invalidate.assert_not_called()
        view.app.refresh_git.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
