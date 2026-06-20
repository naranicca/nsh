"""Notes mode.

A multi-line editbox at the top for a new note, and the saved notes below.
``Ctrl+S`` saves the editbox as a new note (added at the top). ``Down`` from the
editbox steps into the list; there ``j``/``k`` (or arrows) move between notes
with line-level scrolling, ``d``/``x`` delete the selected note, and ``u`` undoes
the last delete. Notes are multi-line; the list renders and scrolls by line.
"""
from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.margins import Margin

from ..util.notes import Notes
from ..util.width import text_width

INPUT_HEIGHT = 5  # rows reserved for the new-note editbox


class _NotesScrollbar(Margin):
    """A scrollbar for the notes list. The list windows its own content (only
    the visible lines are rendered), so the built-in ScrollbarMargin can't see
    the true length — this reads the view's line-level scroll state instead."""

    def __init__(self, view):
        self.view = view

    def get_width(self, get_ui_content):
        # only claim a column when the content actually overflows
        total = self.view._total_lines()
        return 1 if total > self.view._visible_height() else 0

    def create_margin(self, window_render_info, width, height):
        total = self.view._total_lines()
        if height <= 0 or total <= height:
            return []
        scroll = max(0, min(self.view.scroll, total - height))
        thumb = max(1, min(height, height * height // total))
        max_scroll = total - height
        top = (height - thumb) * scroll // max_scroll
        top = max(0, min(top, height - thumb))
        frags = []
        for row in range(height):
            inside = top <= row < top + thumb
            frags.append(("class:scrollbar.button" if inside
                          else "class:scrollbar.background", " "))
            if row < height - 1:
                frags.append(("", "\n"))
        return frags


class NotesView:
    def __init__(self, app):
        self.app = app
        self.notes = Notes()
        self.cursor = 0      # selected note index
        self.scroll = 0      # line-level scroll offset into the flattened list
        self._undo = []      # stack of (index, text) for restoring deletes
        self._editing = None  # index of the note being edited, else None

        self.input = Buffer(multiline=True)
        self.input_control = BufferControl(
            self.input, key_bindings=self._input_kb())
        self.list_control = FormattedTextControl(
            self._list_text, focusable=True, show_cursor=False,
            key_bindings=self._list_kb())
        self.list_window = Window(
            self.list_control, wrap_lines=True, style="class:notes.item",
            right_margins=[_NotesScrollbar(self)])

        self.container = HSplit([
            Window(
                FormattedTextControl(self._label_text),
                height=1, style="class:notes.label"),
            Window(self.input_control, height=INPUT_HEIGHT, wrap_lines=True,
                   style="class:notes.input"),
            Window(height=1, char="─", style="class:preview.border"),
            self.list_window,
        ])

    def _label_text(self):
        if self._editing is not None:
            return [("class:notes.label", " Editing note  —  Ctrl+S save · Esc cancel ")]
        return [("class:notes.label",
                 " Notes  —  Ctrl+S save · ↓ browse · ↵ edit · d/x delete · u undo ")]

    # -- lifecycle ------------------------------------------------------------
    def load(self):
        """(Re)enter notes mode: reload from disk, reset the view."""
        self.notes.load()
        self.cursor = 0
        self.scroll = 0
        self._editing = None
        self.input.reset()

    def focus_input(self):
        self.app.application.layout.focus(self.input_control)
        self.app.invalidate()

    def focus_list(self, index=None):
        if len(self.notes) == 0:
            return
        if index is not None:
            self.cursor = max(0, min(len(self.notes) - 1, index))
        self.app.application.layout.focus(self.list_control)
        self.app.invalidate()

    # -- actions --------------------------------------------------------------
    def save_note(self):
        text = self.input.text.strip()
        if not text:
            self.app.set_message("empty note — nothing saved")
            return
        if self._editing is not None:
            # editing an existing note: replace it in place, keep it selected
            idx = self._editing
            self.notes.replace(idx, text)
            self._editing = None
            self.input.reset()
            self.cursor = min(idx, max(0, len(self.notes) - 1))
            self.focus_list(self.cursor)
            self.app.set_message("note updated")
        else:
            self.notes.add(text)    # newest first (index 0)
            self.input.reset()
            self.cursor = 0
            self.scroll = 0
            self.app.set_message("note saved")
        self.app.invalidate()

    def edit_note(self):
        """Load the selected note into the editbox for editing (Enter in list)."""
        if len(self.notes) == 0:
            return
        self._editing = self.cursor
        self.input.text = self.notes.list()[self.cursor]
        self.input.cursor_position = len(self.input.text)
        self.focus_input()

    def cancel_edit(self):
        self._editing = None
        self.input.reset()
        self.focus_list(self.cursor)
        self.app.set_message("edit cancelled")

    def move(self, delta):
        n = len(self.notes)
        if n == 0:
            return
        self.cursor = max(0, min(n - 1, self.cursor + delta))
        self.app.invalidate()

    def delete_note(self):
        if len(self.notes) == 0:
            return
        idx = self.cursor
        removed = self.notes.delete(idx)
        if removed is None:
            return
        self._undo.append((idx, removed))
        if self.cursor >= len(self.notes):
            self.cursor = max(0, len(self.notes) - 1)
        # keep focus on the list (even when now empty) so `u` can restore
        self.app.set_message("note deleted  (u to undo)")
        self.app.invalidate()

    def undo_delete(self):
        if not self._undo:
            self.app.set_message("nothing to undo")
            return
        idx, text = self._undo.pop()
        self.notes.insert(idx, text)
        self.cursor = idx
        self.focus_list(idx)
        self.app.set_message("note restored")
        self.app.invalidate()

    # -- rendering ------------------------------------------------------------
    def _visible_height(self):
        ri = getattr(self.list_window, "render_info", None)
        if ri is not None and ri.window_height:
            return ri.window_height
        return 20

    def _display_lines(self):
        """Flatten notes into display lines: ``(note_index|None, is_first, text)``;
        a ``None`` index is a blank separator between notes."""
        lines = []
        notes = self.notes.list()
        for ni, note in enumerate(notes):
            for li, ln in enumerate(note.split("\n")):
                lines.append((ni, li == 0, ln))
            if ni != len(notes) - 1:
                lines.append((None, False, ""))  # separator
        return lines

    def _total_lines(self):
        """Total display-line count (for the scrollbar)."""
        notes = self.notes.list()
        if not notes:
            return 0
        # one line per note line, plus a blank separator between notes
        return sum(n.count("\n") + 1 for n in notes) + (len(notes) - 1)

    def _list_text(self):
        if len(self.notes) == 0:
            return [("class:preview.dim", "  (no notes yet — type above, Ctrl+S to save)")]
        try:
            width = get_app().output.get_size().columns
        except Exception:
            width = 80
        height = self._visible_height()
        lines = self._display_lines()
        # the scrollbar claims the rightmost column when the list overflows;
        # pad selected rows to just before it so the highlight doesn't run under
        content_w = max(1, width - (1 if len(lines) > height else 0))

        # keep the selected note's lines within the visible window
        sel = [k for k, (ni, _, _) in enumerate(lines) if ni == self.cursor]
        if sel:
            if sel[0] < self.scroll:
                self.scroll = sel[0]
            elif sel[-1] >= self.scroll + height:
                self.scroll = sel[-1] - height + 1
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - height)))

        frags = []
        for k in range(self.scroll, min(len(lines), self.scroll + height)):
            ni, is_first, ln = lines[k]
            if ni is None:  # separator
                frags.append(("", "\n"))
                continue
            on = ni == self.cursor
            marker = ("▸ " if on else "● ") if is_first else "  "
            cell = " " + marker + ln
            style = "class:notes.selected" if on else "class:notes.item"
            frags.append((style, cell))
            if on:  # pad the selected note's row so its highlight spans the width
                pad = content_w - text_width(cell)
                if pad > 0:
                    frags.append(("class:notes.selected", " " * pad))
            frags.append(("", "\n"))
        return frags

    # -- key bindings ---------------------------------------------------------
    def _input_kb(self):
        kb = KeyBindings()

        @kb.add("c-s")
        def _(event):
            self.save_note()

        @kb.add("down")
        def _(event):
            buff = event.current_buffer
            # at the bottom of the editbox, step into the notes list — but while
            # editing an existing note, Down just moves within its text
            if (self._editing is None and buff.document.on_last_line
                    and len(self.notes)):
                self.focus_list(0)
            else:
                buff.cursor_down()

        # Esc cancels an in-progress edit (without this, the global Esc would
        # leave notes mode); when not editing the filter is off, so Esc falls
        # through to the global handler.
        @kb.add("escape", filter=Condition(lambda: self._editing is not None))
        def _(event):
            self.cancel_edit()

        return kb

    def _list_kb(self):
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("k")
        def _(event):
            if self.cursor <= 0:
                self.focus_input()  # back up into the editbox
            else:
                self.move(-1)

        @kb.add("down")
        @kb.add("j")
        def _(event):
            self.move(1)

        @kb.add("enter")
        def _(event):
            self.edit_note()

        @kb.add("pageup")
        def _(event):
            self.move(-(max(1, self._visible_height() // 2)))

        @kb.add("pagedown")
        def _(event):
            self.move(max(1, self._visible_height() // 2))

        @kb.add("g")
        def _(event):
            self.cursor = 0
            self.app.invalidate()

        @kb.add("G")
        def _(event):
            self.cursor = max(0, len(self.notes) - 1)
            self.app.invalidate()

        @kb.add("d")
        @kb.add("x")
        @kb.add("delete")
        def _(event):
            self.delete_note()

        @kb.add("u")
        def _(event):
            self.undo_delete()

        return kb
