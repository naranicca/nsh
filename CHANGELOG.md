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
- **`source` persists too.** `source FILE` (or `. FILE`) used to be lost — each
  command runs in its own subprocess, so the script's functions / aliases /
  variables vanished before the next command. nsh now remembers each sourced
  file and silently re-sources it ahead of every later command, so its functions
  and variables stay usable for the rest of the session (in any tab). The files
  are re-sourced each command, so a script meant only to define helpers is ideal;
  one with side effects would repeat them (their output is suppressed). (POSIX
  shells / Git Bash.)
- **Completion menu no longer jumps to the top.** Completing a long filename used
  to throw the popup menu to the top-left corner of the screen: the single-line
  prompt scrolls horizontally to keep the cursor visible, pushing the start of
  the completion (where the menu anchors) off-screen, and prompt_toolkit then
  fell back to position (0, 0). The menu keeps its stable anchor (the completion
  start) while that's on screen — so it stays put as you cycle candidates — and
  only re-anchors at the always-visible cursor once the start has scrolled off,
  so it never leaps to the top.
- **Tab-completion menu navigation.** When there's a single candidate, Tab
  applies it directly without a menu; a unique directory is entered and the menu
  of its contents opens without pre-selecting an item (the focus stays on the
  prompt) — a further Tab then steps into that menu instead of closing it, so you
  can keep tabbing straight through nested directories. With several candidates,
  it opens the completion menu with the first item selected; the arrows or `j`/`k`
  move through it. `Tab` and `Space` both accept the highlighted item — a directory
  is reopened so you can keep drilling in (no trailing space), and anything else
  ends with a trailing space.
- **Completions quote names with shell metacharacters.** Tab-completing a file
  or directory whose path contains a space — or parentheses, `&`, `;`, `|`, `$`
  and the like — now wraps it in double quotes so the shell sees one argument
  (`cat "file (1).txt"`); previously only a space triggered quoting, so a name
  with parentheses was left bare and the command failed to run. On POSIX shells
  the characters still special inside double quotes (`$`, `` ` ``, `"`, `\`) are
  backslash-escaped as well. A directory keeps its quote open (`"New folder/`) so
  you can keep drilling into it, and `cd` strips the quotes (and escapes) back
  off. The same quoting applies to filenames dropped into the prompt when you
  enter the shell with files selected in the explorer.
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
  with Ctrl+T, close with Ctrl+W. A tab bar below the prompt lists the
  sessions, and any tab whose command is still running is tinted orange. The tab
  bar also stays visible outside shell mode (just above the status bar) whenever
  there's an open shell — a running command, multiple tabs, or one with
  scrollback — so background shells aren't hidden; clicking a tab there jumps
  straight into it.
- **Selected files seed the prompt.** Opening the command line from the explorer
  while files are selected drops their names (relative, quoted if they contain a
  space) into the empty prompt, ready to use as command arguments.
- **Send a file to the command line.** `Ctrl+J` (or `Ctrl+Down`) in the explorer
  opens the shell and splices the cursor file — or the whole selection — into the
  command line at the cursor (quoted, relative to the cwd), so you can build a
  command like `cat <name>` without retyping the name.
- **Word-wrap output.** Long lines now wrap instead of being cut off at the
  right edge, so you can read them in full.
- **Backspace-aware output.** A `\b` (0x08) in command output is resolved the
  way a terminal does — the cursor steps back and later characters overwrite —
  instead of showing a literal `^H`. ANSI colour is preserved across the
  overwrite.
- **Snappy typing with a long scrollback.** Rendering now only materialises the
  visible lines, so input and cursor movement stay fast no matter how much
  output has accumulated.
- **Commands no longer break the keyboard.** A command's stdin is detached from
  the terminal (it gets `/dev/null`), so it can't leave the terminal in a state
  that swallowed nsh's own input afterwards — arrow keys (Up/Down history) would
  stop registering after running certain commands. Interactive tools still get a
  real terminal via the run-on-terminal path.
- **Alt+Up / Alt+Down** scroll the output one line at a time.
- **Typing jumps back to the bottom.** When you've scrolled up through the output
  (which hides the input line), starting to type a command snaps the view back to
  the newest line so the prompt reappears and you can see what you're typing.
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
- **Commands that run on the terminal echo their prompt first.** When a command
  drops to a real terminal (git asking for a username/password, an editor, …),
  nsh now prints the prompt and the command above its output — in the same
  format as the shell prompt (cwd + git branch + `$`), minus the previous
  command's run-time/exit badge — so the bare output isn't left without context.
- **Quoting fix.** Streamed commands keep their own quotes intact
  (e.g. `python -c "..."`).

### Explorer & Git
- **Preview shows permissions, size and modified date.** Under the filename the
  preview pane has one meta line: the permissions in `ls -l` style (e.g.
  `-rw-r--r--`, `drwxr-xr-x`) · the size / line count / item count · the modified
  date. For a symbolic link it shows the link's mode (`lrwxrwxrwx`) followed by
  `→ <target>` (the path it points to).
- **Long filenames wrap in the preview header.** A name too long for the pane
  used to be cut off; it now wraps across as many header rows as it needs so the
  whole name is visible. The `[+]`/`[-]` zoom button stays pinned to the
  top-right corner of the first row — and stays clickable: it's positioned
  against the pane's true current-frame width, so the scrollbar appearing (which
  a wrapped name often triggers) no longer pushes the button onto a second row,
  and the click is hit-tested by character index (not display column) so it
  works for wide (CJK) filenames too.
- **Scroll the preview pane.** In single-pane explorer mode and in the git /
  git-log views, `F7`/`F8` move focus between the list and the preview; while the
  preview is focused the arrows (plus `PgUp`/`PgDn` and `g`/`G`) scroll it, and
  the list keeps its cursor visible. The header (filename) stays pinned and gains
  a background to show which pane has the focus, and a scrollbar appears whenever
  the content overflows. `F7`/`F8` or `Esc` returns to the list.
- **The `F7`/`F8` pane keys are remappable.** The one pair that switches shell
  tabs, moves between the two panes and toggles list ↔ preview focus is now
  configurable via `[keys] pane_prev` / `pane_next` in nshrc, like the other
  action keys — and a `Ctrl` combo (e.g. `c-o`) works as the value. The status
  bar hints follow whatever you set.
- **Zoom the focused pane** with `z` (remappable via `[keys] zoom`). It enlarges
  whichever pane has the focus from an even 5:5 split to 9:1 — the list or the
  preview in single-pane explorer / git / git-log, or the active pane in
  two-pane view. The big pane follows the focus, so the pane keys hand the space
  to the newly focused pane; press `z` again — or `Esc` — to go back to 5:5.
  While zoomed, `Esc` backs out the zoom first (a second `Esc` then does its
  usual job: clear a selection, return from the preview, or leave the mode).
  Clicking a *different* pane with the mouse cancels the zoom (back to 5:5)
  instead of handing the big share over — use the preview header's `[+]`/`[-]`
  button to zoom with the mouse.
- **Two-pane view**: toggle it with the `2` key (remappable), or
  `[general] two_pane = true` in nshrc, to show two explorer panes side by
  side, each with its own directory, selection and cursor. The preview pane is
  hidden in this view, and the status bar shows the `2`/`F7`·`F8` hints.
  `F7`/`F8` move the cursor between the panes — like switching shell tabs — and
  only the active pane shows its cursor row; the other keeps its place. The
  title bar mirrors the split: the left pane's path sits in the left half and
  the right pane's path is aligned to the start of the right half, each clipped
  to its half (keeping the tail with a leading `…`) so the left can't bleed into
  the right and the right never covers the clock. The active path is marked
  (`▸`) and brightly coloured; the cwd, git branch and command line / file
  operations all follow whichever pane is active. The copy / cut keys cross
  straight over to the other pane here: `y` copies the cursor row or selection
  into the other pane's directory and `x` moves it there — each after a confirm
  dialog, since it's easy to hit the wrong key. In single-pane the keys keep
  their usual copy/cut-to-clipboard behaviour.
- **`..` parent row.** The listing starts with a `..` entry whenever the
  current directory has a parent (so not at a drive / filesystem root); opening
  it (Enter or double-click) steps up, landing the cursor on the directory you
  left. It's navigation-only — never selected, renamed, deleted or expanded —
  and entering a directory skips past it onto the first real row.
- **Expand folders inline**: on a directory the Right arrow / `l` expands its
  contents as an indented tree under it (and again to collapse); the caret turns
  from `▸` to `▾`. Enter opens a file or enters a directory. Inside an expanded
  tree, Left/`h` moves the cursor up to the parent directory (leaving it
  expanded); pressing it again on that expanded directory folds it. Expansions
  reset when you change directory. Symlinked directories can be expanded too.
- **Select by pattern** (`*`): opens a *Select pattern* dialog whose matches
  highlight live as you type — a plain word is a substring match (`txt` grabs
  every name containing it), a pattern with a glob metacharacter is matched as a
  shell glob (`*.py`, `test?`). Matching is case-insensitive and adds on top of
  any existing selection; Enter keeps it, Esc restores what you had. The scan is
  debounced so typing stays snappy even on very long listings.
- **Sort order** (`s`): a *Sort by* menu switches the listing between name, size,
  date and type, each offered ascending (`↑`) and descending (`↓`); directories
  always stay first. The default is configurable in nshrc (`[general] sort` /
  `sort_reverse`).
- **Jump home / recent directories**: `~` changes to your home directory, and
  `-` opens a menu of recently visited directories to jump back to one. Both
  keys are remappable in the `[keys]` section of nshrc.
- **Inline rename** (`F2`, or `i`): the cursor row's name becomes editable in
  place — no dialog — with the cursor placed before the extension. Type to edit,
  Enter commits, Esc cancels. (The rename key moved from `R` to `F2`.)
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
- **Resolve a merge/rebase in progress.** When a merge, rebase, cherry-pick or
  revert stops on a conflict, the title bar flags it (`⚠ rebase`) and the action
  menu adds *Git: Continue* (commit / `--continue`, on a real terminal) and
  *Git: Abort*. Edit the conflicted files (shown in git mode), stage them, then
  continue.
- **Action menu in an empty directory.** `Tab` now opens the menu even with no
  files, offering *New folder*, *New file* (and *Paste* / repo-wide git actions
  when they apply) instead of doing nothing.
- **Action menu force-selects the cursor file.** Opening the action menu (`Tab`)
  with nothing selected now marks the file under the cursor as selected, so the
  menu's actions have an explicit target. *Git: Commit* therefore commits that
  file; a new **Git: Commit all** entry commits the whole current directory
  (`git commit .`) — it appears whenever the repo has tracked changes, even on
  an unmodified or no file. The forced mark is temporary: it's cleared once the
  menu closes (whether an action ran or it was cancelled), while a real
  selection you made yourself is left untouched.
- **chmod** (action menu → *chmod…*, POSIX only): a permission editor for the
  selected file(s). A 3×3 grid of rwx toggles for owner / group / other with a
  live `rwxr-xr-x (755)` readout; arrow keys / Tab / Space move and toggle, a
  digit 0-7 sets a whole row at once, and every cell and button is clickable.
  The grid is seeded from the first target's current mode and the chosen mode is
  applied to all selected items.
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
- **Keys work with the Korean IME on.** A console app can't intercept keys
  before the IME composes them, so with the Korean IME switched on, pressing `j`
  to scroll used to deliver the jamo `ㅓ` and do nothing — you had to flip to
  English first. The navigation / action keys now also bind the two-beolsik
  (두벌식) jamo each key produces, so `j`/`k`/`g`/… keep working whatever the IME
  state, in the explorer, git, git-log, process and notes lists and in popup
  menus. Text-entry areas (the shell, search, rename, the note editbox) are
  untouched, so Korean input there stays Korean. Only lowercase letters are
  recovered — a Shift'd letter composes to the same jamo as its lowercase form,
  so keys like `G` / `D` still need the English IME.
- **Mouse clicks.** On top of the existing wheel scrolling, the mouse now
  clicks. In the explorer, git and git-log lists a click moves the cursor to the
  row (and activates that pane in two-pane view); a double-click opens it — a
  file/dir, a changed file, or a commit's action menu. Ctrl+click toggles a
  row's multi-selection in the explorer and git views (like Space). Clicking a directory's
  ▸/▾ caret in the explorer expands or collapses it inline. The same applies to
  the notes list (double-click edits a note; clicking the editbox above moves
  focus there to start writing) and the System process list (a
  click selects the row; clicking the `CPU%` / `MEM%` / `PROC` column header
  sorts by it). Clicking the preview pane (or the other pane in
  two-pane view) moves the focus there. Clicking the explorer or preview while
  the shell is focused closes the shell and moves focus to the clicked pane.
  Header corner buttons: the preview header's `[+]` / `[-]` (top-right) zooms the
  pane in and out, and the process and notes headers carry an `[x]` that closes
  that view. A
  click in any popup menu
  invokes that row directly (and a click anywhere outside an open menu dismisses
  it, like Esc), and clicking a shell tab switches to it while a double-click
  closes it — with a confirm first if that tab's command is still running (the
  wheel cycles tabs). Clicking the `nsh` label at the left of the title bar opens the nsh
  (F10) menu. Dialog buttons (OK / Cancel) and the find dialog's *case
  sensitive* / *whole word* checkboxes are clickable too. The status bar's
  shortcut hints are clickable — clicking a hint (its key or its label) runs that
  action, e.g. `Tab actions`, `/ find`, `2 2-pane`, `q quit` (the directional /
  typing hints like the arrows stay inert). Clicking the yellow notes square (■)
  at the far left opens notes, like Ctrl+N.
- **Process manager** (*System* in the F10 menu): a small task manager. A header
  shows overall CPU, memory and disk usage as bars; below it a scrolling,
  cursor-selectable list of the running processes with their CPU% / MEM% / RSS
  and full command line (with arguments — falling back to the process name when
  the command line can't be read); `v` toggles that column between the full
  command and just the short process name (the default), and the choice is
  remembered for next time. `c` sorts by CPU, `m` by memory, `n`
  by name (A→Z) — the sort order is remembered across runs too; `/` filters the
  list by command line or PID (Esc clears it); `x`
  terminates the selected process and `K`
  force-kills it (each with a confirm); `r` refreshes and the list re-samples
  every couple of seconds on its own. `Ctrl+N` opens Notes from here too. No
  third-party deps — it reads ctypes +
  PowerShell on Windows, `/proc` on Linux and `ps`/`sysctl` on macOS, degrading
  to *n/a* for anything it can't read. Esc returns to the explorer.
- **Notes** (`Ctrl+N`, or *Notes* in the F10 menu): a scratch pad of multi-line
  notes. A new-note editbox sits at the top (Enter adds a line, `Ctrl+S` saves
  it at the top of the list); pressing `↓` from the editbox steps into the saved
  notes, where `j`/`k` (or the arrows) move between them with line-level
  scrolling (a scrollbar shows once the list overflows), `/` filters the list
  to notes containing a query (Esc clears it), `Enter` loads the
  selected note back into the editbox to edit it
  (`Ctrl+S` saves the change in place, `Esc` cancels), `d`/`x` delete the
  selected note, and `u` restores the last delete. `y` (or `Ctrl+C`) in the list
  copies the selected note to the system clipboard, and `Ctrl+V` in the editbox
  pastes the clipboard at the cursor (no third-party dependency — the Win32 API
  on Windows, `pbcopy`/`pbpaste` or `wl-clipboard` / `xclip` / `xsel` elsewhere;
  it's a no-op when none is available). `y` is the reliable copy key — some
  terminals (Git Bash / MSYS, or Windows Terminal with a selection) never hand
  `Ctrl+C` to the app. The title bar above the
  editbox is blue while the editbox is active (typing / editing) and grey once
  the cursor moves down into the list; the list's selection highlight only
  shows while the list is focused (it's hidden while you edit a note above).
  Notes persist in `~/.config/nsh/notes.json`. Esc returns to the explorer,
  auto-saving any unsaved draft still in the editbox first (no prompt).
- **Find** (`Ctrl+F`, or *Find* in the F10 menu): asks whether to search file
  *contents* or file *names*. **Text** opens a small form — the phrase plus
  *case sensitive* and *whole word* checkboxes (Tab/↑↓ between fields, Space
  toggles, Enter runs) — and streams a coloured `grep -rnI` (recursive,
  line-numbered, `--color=always`, `-i`/`-w` per the toggles) of the pattern
  into the shell. **File** starts the existing fuzzy file finder.
- **nsh menu** (`F10`): opens a small menu from any mode with *Find*, *Notes*,
  *System*, *Preferences* — which opens your `nshrc` in the editor
  (seeding the default first if it doesn't exist) — and *About*, a centered
  dialog showing the version and GitHub URL.
- **Menu colours in the config template.** The seeded `nshrc` now lists the
  popup action-menu styles (`menu` for its background, plus `menu.title` /
  `menu.item` / `menu.selected`) among the `[colors]` examples, so they're easy
  to find and override — the override itself already worked for any style class.
- **Live config reload.** Saving an edit to `nshrc` — remapped keys, colours —
  now applies within a second without restarting nsh (a *config reloaded*
  message confirms it), whether you edited it from *Preferences* or in another
  window.
- **Fast with long lists.** The explorer, git mode and git log now render only
  the on-screen rows instead of the whole list every frame, and the git log
  parses each line's colours once (cached) rather than on every render — so
  moving the cursor stays snappy with thousands of files or commits.
- **Status messages lead and stay put.** A status-bar message shows in front of
  the shortcut hints and remains there until something explicitly clears it — a
  directory change, a mode change, or ESC — instead of auto-dismissing or
  sliding away on a timer.
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
