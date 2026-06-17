# Changelog

Notable user-facing changes to the Python rewrite. Newest first.

## Unreleased

### Command-line (shell) mode
- **Prompt shows the path, git branch and `$`.** The directory uses the
  explorer's directory colour, the current git branch follows as ` (branch)`
  tinted by repo state like the title bar (green in sync, yellow behind/ahead,
  red with uncommitted changes) with a `+N`/`-N` count when ahead/behind the
  upstream, and the `$` is the default text colour.
- **Windows drive change.** A bare `D:` (or a drive path like `D:\work`) typed
  on its own changes directory like in cmd — `D:` returns to the last place you
  were on that drive, or its root the first time. A drive path that points at a
  file (`D:\tool.exe`) still runs normally.
- **Variable assignments persist.** A line that only sets variables (`a=10`,
  `export PATH=…`) is now evaluated once and kept in nsh's environment for the
  rest of the session, so later commands — in any tab — inherit it (`echo $a`
  prints `10`). Quoting, `$other` expansion and `$(…)` all work. (POSIX shells /
  Git Bash.)
- **Tab-completion menu navigation.** Tab opens the completion menu with the
  first item selected; the arrows or `j`/`k` move through it, `Tab` accepts the
  highlighted item (no trailing space) and `Space` accepts it and adds a space.
- **Fuzzy path completion.** After the exact (prefix) path matches, Tab also
  offers pseudo-fuzzy matches — the typed characters need only appear in order
  (`abc` matches `a…b…c`). Every path component is matched this way, not just the
  last, so `sou/re/n` completes to `source/repos/nsh` (and `…/nnn`, …). A leading
  `~` or `/` stays anchored. (Commands stay prefix-only.)
- **Running-time in the prompt.** While a tab's command is still running, the
  prompt is prefixed with the live elapsed time (e.g. `[9s]`), ticking each
  second. After it finishes the time stays, tinted green (exit 0) or red
  (failure) — shown even for sub-second commands — until the next command.
- **Shell tabs / multiple sessions.** Entering a command while the current one
  is still running opens it in a new shell tab instead of interleaving output;
  each tab has its own scrollback and process. Switch with Alt+←/→, open a tab
  with Ctrl+T, close with Ctrl+W. A tab bar (shown once there is more than one)
  marks which sessions are still running.
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
- **Correct non-UTF-8 output.** Output is decoded as UTF-8 with a fallback to
  the OS OEM code page (e.g. cp949), so localized tool/`cmd` messages render
  correctly.
- **Network git runs in the shell when credentials are cached.** `push`/`pull`/
  `fetch`/`clone` are now tried in the shell first (with prompting disabled), so
  when the credentials are already stored they stream their output into the
  scrollback like any other command. Only when git actually needs to ask for a
  username/password does nsh fall back to a real terminal (leaving a one-line
  `git push: done` / `exit code N` note); a plain failure such as a rejected
  push or an unreachable host stays in the shell with its real error. `sudo` and
  bare interactive tools still go straight to the terminal.
- **Quoting fix.** Streamed commands keep their own quotes intact
  (e.g. `python -c "..."`).

### Explorer & Git
- **Expand folders inline**: on a directory the Right arrow / `l` expands its
  contents as an indented tree under it (and again to collapse); the caret turns
  from `▸` to `▾`, while `e` enters the directory. Inside an expanded tree,
  Left/`h` collapses the directory (jumping to it) instead of leaving for the
  parent. Expansions reset when you change directory. Symlinked directories can
  be expanded too. Set `[general] right_expand = false` in nshrc to swap the
  two — then the Right arrow enters a directory and `e` expands it (Enter always
  opens / enters).
- **Jump home / recent directories**: `~` changes to your home directory, and
  `-` opens a menu of recently visited directories to jump back to one. Both
  keys are remappable in the `[keys]` section of nshrc.
- **Inline rename** (`F2`): the cursor row's name becomes editable in place —
  no dialog — with the cursor placed before the extension. Type to edit, Enter
  commits, Esc cancels. (The rename key moved from `R` to `F2`.)
- **Git mode** (`Ctrl+G`): a flat, `git status`-style list of the repo's changed
  and untracked files (subdirectory changes show their full path, not a tree),
  with multi-select and a diff in the preview pane. Tracked changes are listed
  first, untracked files last. `Tab` opens an action menu (stage/unstage the
  selection, commit, edit, branches, revert) — and with no changed files it
  still offers the repo-wide actions (log, pull, push, branches). Left/right are
  inert (no hierarchy); moving to another directory leaves git mode.
- **Git log** (action menu → *Git: Log*): a graph + one-line history; Up/Down
  move between commits (graph-only lines are skipped) and the preview pane shows
  the selected commit's detail and diff. `/` searches the log (by hash, subject
  or author) and `n`/`N` jump to the next/previous match. Enter opens an action
  menu — **check
  out** the commit (detached HEAD), **revert to** it (roll the branch back with
  `reset --hard`), **amend its message** (reword), **squash** it together with
  the commits after it into one, or **interactively edit** from it
  (`git rebase -i` on a real terminal, so you edit the todo list and resolve
  conflicts in your own editor). Reword/squash/rebase rewrite history, so they
  need a clean working tree. Esc/`q` leaves.
- **Git stash** (action menu): *Git: Stash* shelves your changes; *Git: Stashes…*
  lists the stash stack, and picking one offers Pop (apply + drop), Apply (keep)
  or Drop. Handy with the reword/squash/rebase actions, which need a clean tree.
- **Symlinked directories keep their logical path** (`cd -L`): entering a
  symlinked subdir shows the path you followed (not the link target), and going
  up returns to the directory that holds the link instead of the target's real
  parent.
- **Untracked marker** (`?`) is now gray instead of red.
- **Title-bar branch colour** reflects repo state: red when there are
  uncommitted changes, yellow `↓N` when behind the upstream, yellow `↑N` when
  ahead (committed but not pushed), green when in sync.
- **Git action menu** adapts to the file's status (untracked shows only
  *Git: Add*; unmodified files hide stage/diff) — but *Git: Commit* is offered
  whenever the repo has tracked changes, even on a clean file or directory,
  since it commits the whole directory. **Git: Push** appears when there are
  commits to push — ahead of the upstream, or an unpushed branch on a repo that
  has a remote (in which case the push sets the upstream, `git push -u`).
  **Git: Pull** appears when the branch has an upstream. Both run on a real
  terminal so they can prompt for credentials (and pull can resolve a merge).
  It also includes a
  **Branches** submenu: checkout, `+ New Branch`, and delete (local/remote)
  with a confirmation. Deleting a *remote* branch now runs on a real terminal so
  git can prompt for a username/password where credentials are required (it used
  to hang/fail). A changed file also offers **Git: Revert** — discards its
  staged and unstaged changes back to HEAD (with a confirmation).
- **Commit works without staging first**: Git: Commit now commits by pathspec
  like the original nsh — the selected files, or the whole current directory
  (`git commit .`) when nothing is selected — instead of only the staged index,
  so it no longer fails with *no changes added to commit*. Selected untracked
  files are staged first so they commit too; with nothing selected, untracked
  files are left alone.
- **Git commit shows why it failed**: a failed commit now puts git's real reason
  in the status bar (e.g. *nothing to commit*, *unable to auto-detect email
  address*) instead of a bare *commit failed* — the full output still goes to the
  shell scrollback.
- **Edit action** for text files, with a configurable editor
  (`[general] editor` in nshrc, else `$EDITOR`/`$VISUAL`, else notepad/vi).
- **Auto-refresh** when the directory changes; `?` shows the key list.
- **Cleaner cursor-row highlight**: the git mark no longer tints the cell after
  it, and the file size now blends into the row highlight instead of showing a
  grey block at the right edge. The leading marker cell (and an unmarked file's
  mark cell) now take the row colour too, so the cursor highlight is a single
  solid colour instead of a near-white cell at the front — in both the explorer
  and git mode.

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
