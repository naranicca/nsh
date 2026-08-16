# nsh development guide

## Product direction

- nsh is a cross-platform terminal file explorer with integrated local and
  remote command shells. Keep Windows, Linux, and macOS behavior aligned.
- Explorer, shell, Git, search, Preferences, notes, system, and network views
  should feel like parts of one interface: reuse navigation, cursor, selection,
  sorting, search, prompt, elapsed-time, and queue conventions.
- Prefer clear, predictable behavior over hidden context. When an action can
  target either the current directory or selected files, show both scopes in
  the menu explicitly.
- Keep the UI responsive. Filesystem traversal, indexing, network operations,
  Git commands, previews, and long-running shell commands must not block the
  prompt_toolkit event loop.

## Current behavior to preserve

- Tabs own their explorer pane(s), shell session, Git view, log view, cwd,
  selection, cursor, queue, and scrollback.
- Commands entered while a local or remote command is running are queued
  immediately and run in order.
- Interactive commands, commands prefixed with `!`, and commands configured in
  `external_commands` run on the real terminal and still record duration and
  exit status.
- Local and SSH shells share prompt colors, horizontal scrolling, elapsed-time
  badges, and queue presentation.
- Fuzzy search shows current-directory or already-visible results immediately,
  then adds background-indexed results.
- FTP/SFTP browsing uses a local/remote two-pane layout. `c` uploads from the
  local pane and downloads from the remote pane. SSH config, IdentityFile,
  ProxyJump, ProxyCommand, host-key verification, and remembered targets are
  supported.
- Git Commit, Revert, and Log menus expose `.` and selected-file scopes as
  separate entries. Directory Git markers aggregate tracked descendant changes
  only while collapsed; expanded directories, untracked parent directories,
  and the synthetic `..` row do not show aggregate markers.
- Preferences is a searchable full-screen editor for general variables,
  colors, and shortcuts. Modified values are marked and can be reset; a blank
  shortcut means unbound rather than default.

## Implementation guidelines

- Use `pathlib` and existing path helpers. Account for both Windows and POSIX
  separators, quoting rules, home expansion, case normalization, and drive
  letters.
- Use existing reusable UI components in `nsh/util`, especially menus, dialogs,
  width helpers, and scrolling controls.
- Preserve CJK display correctness by measuring terminal cell width with the
  helpers in `nsh/util/width.py`, not `len()`.
- Keep per-tab and per-pane state on its owning object; do not accidentally use
  whichever tab or pane happens to be active when an asynchronous task finishes.
- Capture menu action targets when the menu is built if its temporary selection
  may be cleared when the menu closes.
- Do not overwrite existing files during copy, upload, download, or branch
  browse extraction. Follow the existing unique-name behavior.
- Do not add required external executables or Python dependencies when an
  optional, gracefully degrading implementation is practical.
- Update README.md when user-visible behavior, configuration, shortcuts,
  installation, architecture, or network capabilities change.

## Verification

- Add or update regression tests for every behavior change.
- Run the focused test module first, then the full suite:

  ```powershell
  python -m unittest tests.test_relevant_module -v
  python -m unittest discover -s tests
  ```

- Preserve unrelated working-tree changes and untracked user files.
- Do not commit or push unless the user explicitly requests it.
