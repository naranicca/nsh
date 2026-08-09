import re
import unittest
from pathlib import Path

import nsh


class VersionTests(unittest.TestCase):
    def test_runtime_and_package_versions_are_fixed_at_1_0(self):
        pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(
            encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertEqual("1.0.0", nsh.__version__)
        self.assertEqual(nsh.__version__, match.group(1))


if __name__ == "__main__":
    unittest.main()
