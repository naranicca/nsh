import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from nsh.search.fuzzy import gather


class SearchFuzzyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
