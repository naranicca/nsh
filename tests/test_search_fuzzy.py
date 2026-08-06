import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from nsh.search.fuzzy import gather, gather_query_path, match
from nsh.search.view import SearchView


class SearchFuzzyTests(unittest.TestCase):
    def test_path_separators_match_across_windows_and_posix_forms(self):
        self.assertIsNotNone(match("src/mo", r"src\model.py"))
        self.assertIsNotNone(match(r"src\mo", "src/model.py"))

    def test_gather_keeps_breadth_first_order_with_deque(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a" / "deep").mkdir(parents=True)
            (root / "b").mkdir()
            (root / "a" / "deep" / "last.txt").write_text("x")
            (root / "b" / "near.txt").write_text("x")

            items = gather(root, limit=20, skip=set())

        expected = [
            "a" + os.sep,
            "b" + os.sep,
            os.path.join("a", "deep") + os.sep,
            os.path.join("b", "near.txt"),
            os.path.join("a", "deep", "last.txt"),
        ]
        self.assertEqual(items, expected)

    def test_slash_query_lists_matching_subdirectory_before_full_index(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "source" / "models").mkdir(parents=True)
            (root / "source" / "models" / "user.py").write_text("x")
            (root / "source" / "main.py").write_text("x")

            items = gather_query_path(root, "sou/ma", skip=set())

        self.assertIn(os.path.join("source", "main.py"), items)
        self.assertIn(os.path.join("source", "models") + os.sep, items)

    def test_search_view_merges_path_lookup_while_full_index_is_loading(self):
        async def scenario(root):
            app = SimpleNamespace(invalidate=mock.Mock())
            view = SearchView(app)
            view.remote_view = None
            view.loading = True
            view._immediate_candidates = ["source" + os.sep]
            view.candidates = list(view._immediate_candidates)
            view._search_root = root
            view._search_show_hidden = False
            view._search_skip = set()
            view.query_buffer.text = "sou/ma"
            task = view._path_task
            await task
            first_candidates = list(view.candidates)

            # Editing only the final search term reuses the same directory
            # listing instead of scheduling another filesystem scan.
            view.query_buffer.text = "sou/mod"
            return view, task, first_candidates

        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "source" / "models").mkdir(parents=True)
            (root / "source" / "main.py").write_text("x")
            view, first_task, candidates = asyncio.run(scenario(root))

        self.assertIn(os.path.join("source", "main.py"), candidates)
        self.assertIs(view._path_task, first_task)


if __name__ == "__main__":
    unittest.main()
