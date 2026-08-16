import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nsh.explorer.preview import PreviewView
from nsh.explorer.git import GitStatus


class PreviewKeyTests(unittest.TestCase):
    def test_h_returns_focus_to_active_list(self):
        view = object.__new__(PreviewView)
        view.app = SimpleNamespace(
            keys={}, focus_active_list=mock.Mock(), invalidate=mock.Mock())
        bindings = view._kb().get_bindings_for_keys(("h",))

        self.assertEqual(len(bindings), 1)
        bindings[0].handler(SimpleNamespace())

        view.app.focus_active_list.assert_called_once_with()

    def test_tracked_changed_explorer_file_uses_git_preview(self):
        cwd = Path("repo").resolve()
        path = cwd / "src" / "app.py"
        status = GitStatus(is_repo=True, root=cwd)
        status.add_file(path, "M")
        entry = SimpleNamespace(path=path, name="app.py", is_dir=False)
        view = object.__new__(PreviewView)
        view.app = SimpleNamespace(
            cwd=cwd, explorer=SimpleNamespace(git_status=status))

        adapted = view._explorer_git_entry(entry)

        self.assertEqual(adapted.path, path)
        self.assertEqual(adapted.code, "M")
        self.assertEqual(adapted.rel, "src/app.py")

    def test_clean_untracked_and_directory_entries_keep_normal_preview(self):
        cwd = Path("repo").resolve()
        status = GitStatus(is_repo=True, root=cwd)
        untracked = cwd / "draft.txt"
        status.add_file(untracked, "?")
        view = object.__new__(PreviewView)
        view.app = SimpleNamespace(
            cwd=cwd, explorer=SimpleNamespace(git_status=status))

        self.assertIsNone(view._explorer_git_entry(SimpleNamespace(
            path=untracked, name="draft.txt", is_dir=False)))
        self.assertIsNone(view._explorer_git_entry(SimpleNamespace(
            path=cwd / "src", name="src", is_dir=True)))

    def test_changed_file_in_expanded_child_repo_uses_git_preview(self):
        cwd = Path("work").resolve()
        repo = cwd / "project"
        path = repo / "src" / "app.py"
        child = GitStatus(is_repo=True, root=repo)
        child.add_file(path, "M")
        outer = GitStatus(
            child_repos={str(repo).lower(): "RD"},
            child_statuses={str(repo).lower(): child})
        view = object.__new__(PreviewView)
        view.app = SimpleNamespace(
            cwd=cwd, explorer=SimpleNamespace(git_status=outer),
            invalidate=mock.Mock())
        view._cache = {}
        view._inflight = set()
        view._diff_hunks = {}
        view._hunk_selection = {}
        entry = SimpleNamespace(path=path, name="app.py", is_dir=False)

        adapted = view._explorer_git_entry(entry)

        self.assertEqual(adapted.code, "M")
        self.assertEqual(adapted.rel, "src/app.py")
        self.assertEqual(adapted.git_cwd, repo)
        key = ("test",)
        with mock.patch("nsh.explorer.preview.git.diff_parts",
                        new=mock.AsyncMock(return_value=("", ""))) as diff:
            asyncio.run(view._load_git(adapted, key))
        self.assertEqual(diff.await_count, 2)
        diff.assert_any_await(path, repo)
        diff.assert_any_await(path, repo, unified=0)

    def test_diff_arrows_jump_between_hunks(self):
        view = object.__new__(PreviewView)
        view._scroll = 0
        view._current_diff_key = mock.Mock(return_value=("git",))
        view._diff_hunks = {
            ("git",): [{"line": 4}, {"line": 12}, {"line": 25}]}
        view._hunk_selection = {}
        view.app = SimpleNamespace(invalidate=mock.Mock())

        self.assertTrue(view.jump_hunk(1))
        self.assertEqual(view._scroll, 12)
        view.jump_hunk(1)
        self.assertEqual(view._scroll, 25)
        view.jump_hunk(-1)
        self.assertEqual(view._scroll, 12)

    def test_j_and_k_use_the_same_change_navigation_as_arrows(self):
        view = object.__new__(PreviewView)
        view.app = SimpleNamespace(keys={})
        view.jump_hunk = mock.Mock(return_value=True)
        view.scroll = mock.Mock()
        bindings = view._kb()

        bindings.get_bindings_for_keys(("j",))[0].handler(SimpleNamespace())
        bindings.get_bindings_for_keys(("k",))[0].handler(SimpleNamespace())

        self.assertEqual(view.jump_hunk.call_args_list,
                         [mock.call(1), mock.call(-1)])
        view.scroll.assert_not_called()

    def test_lowercase_u_reverts_current_change(self):
        view = object.__new__(PreviewView)
        view.app = SimpleNamespace(keys={})
        view.confirm_revert_hunk = mock.Mock()
        bindings = view._kb()

        self.assertEqual(len(bindings.get_bindings_for_keys(("U",))), 0)
        bindings.get_bindings_for_keys(("u",))[0].handler(SimpleNamespace())

        view.confirm_revert_hunk.assert_called_once_with()

    def test_s_stages_current_change(self):
        view = object.__new__(PreviewView)
        view.app = SimpleNamespace(keys={})
        view.stage_current_hunk = mock.Mock()
        bindings = view._kb()

        bindings.get_bindings_for_keys(("s",))[0].handler(SimpleNamespace())

        view.stage_current_hunk.assert_called_once_with()

    def test_revert_confirmation_keeps_focus_in_preview(self):
        view = object.__new__(PreviewView)
        view.focus = mock.Mock()
        view.app = SimpleNamespace(set_message=mock.Mock())

        view._do_revert_hunk({}, False)

        view.focus.assert_called_once_with()
        view.app.set_message.assert_called_once_with("change revert cancelled")

    def test_parse_hunks_keeps_each_patch_independent(self):
        text = ("diff --git a/a.txt b/a.txt\nindex 111..222 100644\n"
                "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"
                "@@ -5 +5 @@\n-before\n+after\n")
        entry = SimpleNamespace(rel="a.txt")

        hunks = PreviewView._parse_hunks(text, "", entry, Path("repo"))

        self.assertEqual(len(hunks), 2)
        self.assertIn("@@ -1 +1 @@", hunks[0]["patch"])
        self.assertNotIn("@@ -5 +5 @@", hunks[0]["patch"])
        self.assertTrue(hunks[1]["patch"].startswith("diff --git"))
        self.assertFalse(hunks[0]["staged"])

    def test_selection_is_one_contiguous_change_block_not_whole_hunk(self):
        display = ("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
                   "@@ -1,5 +1,5 @@\n same\n-old one\n+new one\n middle\n"
                   "-old two\n+new two\n")
        zero = ("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
                "@@ -2 +2 @@\n-old one\n+new one\n"
                "@@ -4 +4 @@\n-old two\n+new two\n")

        hunks = PreviewView._parse_hunks(
            display, "", SimpleNamespace(rel="a.txt"), Path("repo"), zero, "")

        self.assertEqual(len(hunks), 2)
        self.assertEqual((hunks[0]["line"], hunks[0]["end"]), (7, 9))
        self.assertEqual((hunks[1]["line"], hunks[1]["end"]), (10, 12))
        self.assertNotIn("old two", hunks[0]["patch"])

    def test_mixed_diff_has_separate_unstaged_and_staged_sections(self):
        view = object.__new__(PreviewView)
        entry = SimpleNamespace(rel="a.txt", code="M")
        unstaged = "@@ -1 +1 @@\n-old\n+working\n"
        staged = "@@ -3 +3 @@\n-before\n+indexed\n"

        frags = view._build_diff(entry, unstaged, staged)
        rendered = "".join(text for _style, text in frags)
        styles = {text.strip(): style for style, text in frags}

        self.assertLess(rendered.index("Unstaged changes"),
                        rendered.index("Staged changes"))
        self.assertIn("class:git.modified", styles["── Unstaged changes ──"])
        self.assertIn("class:git.staged", styles["── Staged changes ──"])

    def test_staged_and_unstaged_selections_use_different_backgrounds(self):
        self.assertEqual(
            PreviewView._selected_hunk_style({"staged": False}),
            "class:preview-hunk-selected")
        self.assertEqual(
            PreviewView._selected_hunk_style({"staged": True}),
            "class:preview-hunk-staged-selected")

    def test_conflict_markers_are_selectable_blocks(self):
        content = (b"before\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> topic\n"
                   b"middle\n<<<<<<< HEAD\na\n=======\nb\n>>>>>>> topic\nafter\n")
        entry = SimpleNamespace(path=Path("a.txt"), rel="a.txt")

        hunks, lines = PreviewView._parse_conflicts(content, entry, Path("repo"))

        self.assertEqual(len(hunks), 2)
        self.assertEqual(hunks[0]["ours"], b"ours\n")
        self.assertEqual(hunks[0]["theirs"], b"theirs\n")
        self.assertEqual(hunks[0]["both"], b"ours\ntheirs\n")
        self.assertEqual(lines[0], "before")

    def test_marker_text_inside_a_line_does_count_as_a_conflict(self):
        # a source file may legitimately mention the markers; only a line that
        # *starts* one is a block, which is what _parse_conflicts matches
        quoted = b'    sep = b"<<<<<<< HEAD"\n    other = b">>>>>>> topic"\n'

        self.assertFalse(PreviewView._has_conflict_block(quoted))
        self.assertEqual(
            [], PreviewView._parse_conflicts(
                quoted, SimpleNamespace(path=Path("a.py"), rel="a.py"),
                Path("repo"))[0])

    def test_a_real_block_still_counts_as_a_conflict(self):
        content = b"<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> topic\n"

        self.assertTrue(PreviewView._has_conflict_block(content))

    def test_s_opens_resolution_menu_for_conflict_block(self):
        view = object.__new__(PreviewView)
        hunk = {"kind": "conflict"}
        view._current_hunk = mock.Mock(return_value=hunk)
        view._open_conflict_menu = mock.Mock()
        view.app = SimpleNamespace(set_message=mock.Mock())

        view.stage_current_hunk()

        view._open_conflict_menu.assert_called_once_with(hunk)

    def test_enter_and_tab_open_the_resolution_menu_for_a_conflict(self):
        for name, key in (("enter", "c-m"), ("tab", "c-i")):
            with self.subTest(key=name):
                view = object.__new__(PreviewView)
                hunk = {"kind": "conflict"}
                view._current_hunk = mock.Mock(return_value=hunk)
                view._open_conflict_menu = mock.Mock()
                view.app = SimpleNamespace(keys={})
                bindings = view._kb().get_bindings_for_keys((key,))

                self.assertEqual(len(bindings), 1)
                # calls the no-argument entry point, not resolve conflict(hunk,
                # choice) - which the menu's own items invoke with a side
                bindings[0].handler(SimpleNamespace())

                view._open_conflict_menu.assert_called_once_with(hunk)
                self.assertTrue(view.has_conflict_hunk())

    def test_enter_is_inert_on_an_ordinary_diff_hunk(self):
        view = object.__new__(PreviewView)
        # a staged/unstaged block has 's' and 'u'; there is no side to pick
        view._current_hunk = mock.Mock(return_value={"staged": False})
        view._open_conflict_menu = mock.Mock()

        view.resolve_current_conflict()

        view._open_conflict_menu.assert_not_called()
        self.assertFalse(view.has_conflict_hunk())

    def test_enter_is_inert_without_any_selected_block(self):
        view = object.__new__(PreviewView)
        view._current_hunk = mock.Mock(return_value=None)
        view._open_conflict_menu = mock.Mock()

        view.resolve_current_conflict()

        view._open_conflict_menu.assert_not_called()
        self.assertFalse(view.has_conflict_hunk())

    @staticmethod
    def _finishing_view():
        view = object.__new__(PreviewView)
        view._conflict_undo = {"a.txt": [{"any": "record"}]}
        return view

    def _finish(self, view, unmerged, operation):
        with mock.patch("nsh.explorer.preview.git.has_unmerged",
                        new=mock.AsyncMock(return_value=unmerged)), \
             mock.patch("nsh.explorer.preview.git.operation_in_progress",
                        return_value=operation), \
             mock.patch("nsh.explorer.preview.git.commit_pending_operation",
                        new=mock.AsyncMock(return_value=(0, ""))) as commit:
            message = asyncio.run(view._finish_operation(Path("repo")))
        return message, commit

    def test_last_resolved_conflict_commits_the_merge(self):
        view = self._finishing_view()

        message, commit = self._finish(view, unmerged=False, operation="merge")

        commit.assert_awaited_once_with(Path("repo"))
        self.assertIn("merge committed", message)
        # the blocks are committed now; 'u' must not rewrite a finished merge
        self.assertEqual({}, view._conflict_undo)

    def test_other_conflicted_files_postpone_the_commit(self):
        view = self._finishing_view()

        message, commit = self._finish(view, unmerged=True, operation="merge")

        commit.assert_not_awaited()
        self.assertNotIn("committed", message)
        self.assertTrue(view._conflict_undo)

    def test_rebase_is_left_to_its_own_sequencer(self):
        view = self._finishing_view()

        message, commit = self._finish(view, unmerged=False, operation="rebase")

        commit.assert_not_awaited()
        self.assertIn("continue the rebase", message)

    def test_a_lone_conflicted_file_outside_an_operation_is_not_committed(self):
        view = self._finishing_view()

        message, commit = self._finish(view, unmerged=False, operation=None)

        commit.assert_not_awaited()
        self.assertIn("file staged", message)

    def test_a_failed_commit_is_reported_and_keeps_the_undo_history(self):
        view = self._finishing_view()

        with mock.patch("nsh.explorer.preview.git.has_unmerged",
                        new=mock.AsyncMock(return_value=False)), \
             mock.patch("nsh.explorer.preview.git.operation_in_progress",
                        return_value="merge"), \
             mock.patch("nsh.explorer.preview.git.commit_pending_operation",
                        new=mock.AsyncMock(
                            return_value=(1, "hook refused\nnothing added"))):
            message = asyncio.run(view._finish_operation(Path("repo")))

        self.assertIn("commit failed: nothing added", message)
        self.assertTrue(view._conflict_undo)


if __name__ == "__main__":
    unittest.main()
