# Changelog

Notable user-facing changes to the Python rewrite. Newest first.

## Unreleased

### Command-line (shell) mode
- **Word-wrap output.** Long lines now wrap instead of being cut off at the
  right edge, so you can read them in full.
- **Backspace-aware output.** A `\b` (0x08) in command output is resolved the
  way a terminal does — the cursor steps back and later characters overwrite —
  instead of showing a literal `^H`. ANSI colour is preserved across the
  overwrite.
- **Snappy typing with a long scrollback.** Rendering now only materialises the
  visible lines, so input and cursor movement stay fast no matter how much
  output has accumulated.
- **Alt+Up / Alt+Down** scroll the output one line at a time.
- **Exit codes.** A command that exits non-zero prints `[exit code N]`.
- **Correct non-UTF-8 output.** Output is decoded as UTF-8 with a fallback to
  the OS OEM code page (e.g. cp949), so localized tool/`cmd` messages render
  correctly.
- **Interactive credentials work.** Network git commands (`push`/`pull`/
  `fetch`/`clone`) and `sudo` now run on a real terminal, so git can prompt for
  a username/password instead of failing with *"could not read Username"*.
  A one-line result (e.g. `git push: done` / `exit code N`) is left in the
  scrollback afterwards.
- **Quoting fix.** Streamed commands keep their own quotes intact
  (e.g. `python -c "..."`).

### Explorer & Git
- **Git mode** (`Ctrl+G`): a flat, `git status`-style list of the repo's changed
  and untracked files (subdirectory changes show their full path, not a tree),
  with multi-select and a diff in the preview pane. `Tab` opens an action menu
  (stage/unstage the selection, commit, edit, branches). Left/right are inert
  (no hierarchy); moving to another directory leaves git mode.
- **Symlinked directories keep their logical path** (`cd -L`): entering a
  symlinked subdir shows the path you followed (not the link target), and going
  up returns to the directory that holds the link instead of the target's real
  parent.
- **Untracked marker** (`?`) is now gray instead of red.
- **Title-bar branch colour** reflects repo state: red when there are
  uncommitted changes, yellow `↓N` when behind the upstream, yellow `↑N` when
  ahead (committed but not pushed), green when in sync.
- **Git action menu** adapts to the file's status (untracked shows only
  *Git: Add*; unmodified files hide stage/commit/diff) and includes a
  **Branches** submenu: checkout, `+ New Branch`, and delete (local/remote)
  with a confirmation.
- **Edit action** for text files, with a configurable editor
  (`[general] editor` in nshrc, else `$EDITOR`/`$VISUAL`, else notepad/vi).
- **Auto-refresh** when the directory changes; `?` shows the key list.

### UI & dialogs
- Title bar: a single space before `on <branch>`; the action menu lines up
  under the `nsh` label.
- **Tab** activates the selected menu item (like Enter).
- Centered confirm / input dialogs (rename, new file/folder, commit, delete,
  quit-while-running) with arrow/`h`/`l` button navigation.
- Fuzzy search: no yellow background; the selected row is a gray bar that keeps
  the match colour.
- The explorer cursor highlight is hidden while in shell mode; fixed a
  transient blank line after a command.

## 0.2.0

- Python rewrite of the original single-file Bash `nsh` on top of
  `prompt_toolkit`: explorer, command-line and fuzzy-search modes.
