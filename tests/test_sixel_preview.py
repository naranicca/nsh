import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh.explorer.preview import (
    PreviewView, _sixel_run, encode_sixel, sixel_supported)


class SixelPreviewTests(unittest.TestCase):
    def test_windows_terminal_is_detected(self):
        with mock.patch.dict(os.environ, {"WT_SESSION": "session"}, clear=True):
            self.assertTrue(sixel_supported())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(sixel_supported())

    def test_sixel_runs_are_compacted(self):
        self.assertEqual("!5?@@", _sixel_run([0, 0, 0, 0, 0, 1, 1]))

    def test_encoder_emits_raster_palette_and_dcs_terminator(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("optional Pillow dependency is not installed")
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sample.png"
            image = Image.new("RGB", (2, 2), "red")
            image.putpixel((1, 1), (0, 255, 0))
            image.save(path)

            sixel = encode_sixel(path, 10, 5)

        self.assertTrue(sixel.startswith("\x1bP0;1;0q\"1;1;2;2"))
        self.assertIn(";2;", sixel)
        self.assertTrue(sixel.endswith("\x1b\\"))

    def test_image_builder_reserves_cells_then_draws_with_zero_width_escape(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "image.png"
            path.write_bytes(b"image")
            entry = SimpleNamespace(
                path=path, name="image.png", size=5, is_link=False)
            view = PreviewView.__new__(PreviewView)
            view._content_width = lambda: 12
            view._visible_height = lambda: 8
            with mock.patch("nsh.explorer.preview.sixel_supported", return_value=True), \
                    mock.patch("nsh.explorer.preview.encode_sixel",
                               return_value="<SIXEL>"):
                fragments = view._build_image(entry)

        raw = [text for style, text in fragments
               if style == "[ZeroWidthEscape]"]
        self.assertEqual(1, len(raw))
        self.assertIn("<SIXEL>", raw[0])
        self.assertTrue(raw[0].startswith("\x1b7\x1b[9D\x1b[3A"))
        self.assertTrue(raw[0].endswith("\x1b8"))
        escape_index = next(i for i, fragment in enumerate(fragments)
                            if fragment[0] == "[ZeroWidthEscape]")
        anchor_style, anchor_text = fragments[escape_index + 1]
        self.assertTrue(anchor_style.startswith("class:preview.sixel-cell-"))
        self.assertEqual(" ", anchor_text)
        image_cells = [(style, text) for style, text in fragments
                       if style.startswith("class:preview.sixel-cell-")]
        self.assertEqual(5, len(image_cells))
        self.assertEqual(40, sum(len(text) for _style, text in image_cells))

    def test_each_image_uses_distinct_cells_to_replace_previous_sixel(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [Path(root) / "first.png", Path(root) / "second.png"]
            for path in paths:
                path.write_bytes(b"image")
            view = PreviewView.__new__(PreviewView)
            view._content_width = lambda: 12
            view._visible_height = lambda: 8
            with mock.patch("nsh.explorer.preview.sixel_supported", return_value=True), \
                    mock.patch("nsh.explorer.preview.encode_sixel",
                               return_value="<SIXEL>"):
                fragments = [view._build_image(SimpleNamespace(
                    path=path, name=path.name, size=5, is_link=False))
                    for path in paths]

        styles = [{style for style, _text in image
                   if style.startswith("class:preview.sixel-cell-")}
                  for image in fragments]
        self.assertEqual(1, len(styles[0]))
        self.assertEqual(1, len(styles[1]))
        self.assertNotEqual(styles[0], styles[1])


if __name__ == "__main__":
    unittest.main()
