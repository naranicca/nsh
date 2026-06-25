# nsh

## nsh is Not a SHell!

A cross-platform (Windows / Linux / macOS) interactive **file explorer + shell**,
rewritten in Python on top of [`prompt_toolkit`](https://python-prompt-toolkit.readthedocs.io/).
No install needed beyond two pure-Python packages, and it's CJK-aware throughout.

It has three modes:

1. **Explorer** (default) — an `mc`/`lf`-style directory pane with type-coloured
   entries, icons, a live Git status overlay, multi-select, file operations, and
   a side preview pane. Press **`:`** to drop into the shell, **`/`** to fuzzy-find.
2. **Command-line** — drives your host shell with syntax highlighting, an
   interactive Tab-completion popup, ANSI-coloured / progress-bar-aware output,
   and a scrollback you can page through. Press **`ESC`** to return to the explorer.
3. **Fuzzy search** — an fzf-style file picker (`/` from the explorer, or
   `nsh search` from the command line).

Everything runs in **tabs**: each tab is its own explorer + shell pair, so you
can keep several working directories — each with its own command-line session —
open at once and switch between them with `F7`/`F8`.

Recent changes are listed in [CHANGELOG.md](CHANGELOG.md).

## Install / run

Requires Python 3.7+.

```sh
pip install -e .            # installs prompt_toolkit + wcwidth, and an `nsh` command
```

The `-e` (editable) install puts an **`nsh` command on your PATH** that runs from
any directory; because it's editable, later code edits take effect with no
re-install. Then, from anywhere:

```sh
nsh                         # explorer mode
nsh shell                   # start in command-line mode
nsh search [WORD]           # fuzzy-pick a file; the choice is printed to stdout
nsh -h | -v
```

If the `nsh` command isn't found after installing, your Python **user-scripts
directory isn't on PATH**. Print it and add it to PATH:

```sh
python -c "import sysconfig,os; print(sysconfig.get_path('scripts', os.name=='nt' and 'nt_user' or 'posix_user'))"
```

- **Linux / macOS** — this is usually `~/.local/bin`; add it to PATH in your shell rc.
- **Windows** — with the Microsoft Store Python it's a
  `…\LocalCache\local-packages\Python3x\Scripts` folder that isn't on PATH by
  default; add it under *Settings → Edit environment variables → Path*, then open
  a new terminal.

As a fallback that needs no PATH change, run it as a module: `python -m nsh`.

## Keys

### Tabs
Every explorer is paired with a shell in a **tab** — its own directory,
selection, preview and command-line scrollback — so several places stay open at
once and the process working directory follows the active tab.

| Key | Action |
| --- | --- |
| `Ctrl+T` | new tab (a fresh explorer at the current directory) |
| `Ctrl+W` | close the current tab |
| `F7` / `F8`, `Alt+←` / `Alt+→` | previous / next tab |

These work from both the explorer and the command line. The tab bar is also
clickable: click a tab to switch to it, double-click it to close it, and click
the **`+`** button at its right end to open a new tab. The two-pane view (`2`) is
per-tab, so one tab can be split while another shows a single pane.

### Explorer mode
The action keys (everything below the navigation block) are remappable in
`nshrc` — see [Configuration](#configuration).

| Key | Action |
| --- | --- |
| `↑`/`↓`, `k`/`j` | move cursor |
| `↵` | open the file / enter the directory |
| `l`, `→` | expand/collapse a directory inline; **on a file, focus the preview** |
| `⌫`, `h`, `←` | collapse the directory, else go to the parent |
| `Space` | select / deselect the entry (multi-select) |
| `Tab` | open the **action menu** (copy, rename, delete, git…) |
| `y` / `x` / `p` | copy / cut / paste — the picked rows briefly flash; **paste lands in the directory at the cursor** |
| `F2` / `i` | rename (inline) |
| `m` / `N` | new folder / new file — **created in the directory at the cursor** |
| `D` | delete (asks to confirm) |
| `b` | bookmarks — add/remove this directory, or jump to a saved one |
| `/` | fuzzy-find a file |
| `Ctrl+G` | **git mode** — the repository's changed files (see below) |
| `:` | switch to command-line mode |
| `P` | toggle the preview pane |
| `.` | toggle hidden files |
| `r` | refresh (the listing also auto-refreshes when the directory changes) |
| `?` | show this key list |
| `ESC` | clear the selection |
| `q` | quit |

Paste, new file and new folder follow the cursor: a directory under the cursor
is the target (the item lands *inside* it and it expands to show the result),
while a file targets its containing directory — so in the tree view you act
exactly where you're pointing.

Git actions (stage / unstage, commit, diff, and a **Branches** submenu that
lists branches to check out plus a `+ New Branch` entry) live in the `Tab`
action menu when the directory is a repository. An untracked file can be staged
— including the files inside a brand-new directory, which carry the untracked
marker and so can be added too.

### Git mode

`Ctrl+G` opens a flat, `git status`-style list of the repository's changed and
untracked files — a change in a subdirectory shows as its full path (not a
tree). `↑`/`↓` move, `Space` multi-selects, and the preview pane shows the file's
diff (untracked files show their new content). `Tab` opens an action menu
(stage / unstage — applied to the whole selection — commit, edit, branches).
There is no directory hierarchy, so `→`/`l` steps into the diff preview to
scroll it (`Esc` returns to the list) while the other left/right keys are inert;
`Ctrl+G` or `ESC` returns to the explorer, and jumping elsewhere (e.g. via a
bookmark) leaves git mode automatically. The git log (from the `Tab` action
menu) works the same way — `→`/`l` focuses the commit-detail/diff preview.

Git mode and the git log are per-tab — each tab keeps its own changed-file list
and history (cursor, selection and search) — so `F7`/`F8` (or `Alt+←`/`Alt+→`)
switch tabs from here too, swapping the view along with the directory it belongs
to, and `Ctrl+T`/`Ctrl+W` open and close tabs without leaving the mode. Which
mode a tab is in is itself per-tab: leaving git/log mode in one tab doesn't pull
the others out of it, and switching tabs shows each one in the mode you last left
it in (a new tab opens in your current mode, so `Ctrl+T` keeps your workflow).

### Command-line mode
| Key | Action |
| --- | --- |
| typing | live syntax highlighting |
| `Tab` | completion popup; `↑`/`↓` or `j`/`k` navigate, `Tab` picks (no space), `Space` picks and adds a space |
| `↑`/`↓` | command history (when no popup is open) |
| `↵` | run the command |
| `PgUp`/`PgDn`, `Alt+↑`/`Alt+↓`, wheel, `Ctrl+End` | scroll the output (the prompt hides while scrolled up) |
| `Ctrl+T` / `Ctrl+W` | open / close a tab |
| `Alt+←` / `Alt+→` (or `F7` / `F8`) | previous / next tab |
| `ESC` | switch back to explorer mode |

Each **tab** pairs this shell session with its own explorer (see [Tabs](#tabs)),
so switching tabs swaps the whole working context. Entering a command while the
current one is still running opens it in a new tab (rather than mixing the
output); a tab bar marks each session's state — orange while a command is still
running, red once one finishes with a non-zero exit (cleared when the next
command runs).

Built-ins handled internally: `cd`, `clear`/`cls`, `exit`/`quit`. The output pane
grows with its content and goes full-screen once it fills up. Long lines wrap,
the prompt shows each command's run time tinted by its exit status (and dims
itself while a command is still running, since typing then opens a new tab), and
interactive commands that need a real terminal — editors/pagers, plus network
git (`push`/`pull`/`fetch`/`clone`) and `sudo` that may prompt for credentials
— run with the UI briefly suspended. nsh echoes the prompt + command above their
output and, when they finish, waits for a keypress (`press any key to
continue …`) so it stays visible. Prefix any command with **`!`** to force it
onto the real terminal this way — an escape hatch for a TUI nsh doesn't
recognise on its own (e.g. `!htop`).

### Fuzzy search mode
Type to filter, `↑`/`↓` to move, `↵` to select, `ESC` to cancel. Launched with
`nsh search [WORD]` (prints the selection to stdout, e.g. `cd "$(nsh search)"`)
or with `/` from the explorer. Build outputs (`build/`, `dist/`) are indexed too,
so a built executable is findable; the skipped directories are configurable via
`search_exclude` (see [Configuration](#configuration)).

## Configuration

On first run nsh seeds a commented template at `~/.config/nsh/nshrc`
(`$XDG_CONFIG_HOME` is honoured). It is a simple INI file:

```ini
[general]
# editor for the "Edit" action; unset -> $EDITOR/$VISUAL, then notepad/vi
editor = code -w
two_pane = false                     # start with two explorer panes side by side
search_exclude = .git node_modules   # directories fuzzy search skips

[colors]
# <style-class> = <prompt_toolkit style>
explorer.dir      = #5fafff bold
explorer.selected = #ffff5f bold
shell.command     = #5fafff bold

[keys]
# <action> = <key>   (a char, or: space, tab, escape, f5, c-r, s-tab, …)
copy   = y
delete = D
menu   = tab
quit   = q
```

`[general]` sets the **Edit** editor (Tab menu, text files only), whether to
start in `two_pane` view, and `search_exclude` — the directories fuzzy search
skips (seeded with the defaults, so edit it to add a noisy `build/` or remove a
name to search it). `[colors]` overrides any UI style class; `[keys]` remaps the
explorer action keys. Invalid entries are ignored, never fatal.

Bookmarks (the `b` key) are saved one path per line in `~/.config/nsh/bookmarks`.

## Architecture

```
nsh/
  app.py              the three modes, layout, central key dispatch, cwd/git state
  config.py           styles, icons, key map, nshrc loading
  util/
    width.py          wcwidth-based truncate/pad (CJK-correct columns)
    paths.py          pathlib helpers, normalised compare keys
    aio.py            run_in_thread (asyncio.to_thread backport for 3.7/3.8)
    widgets.py        mouse-wheel-aware control
    menu.py           reusable popup action menu
  explorer/
    model.py          os.scandir directory listing
    git.py            async git status / branch / stage / commit / diff
    gitview.py        git mode: flat changed-file list (Ctrl+G)
    fileops.py        copy / move / delete / rename / mkdir (threaded)
    preview.py        side preview pane (text / dir / image dims / hexdump / diff)
    view.py           file-list rendering, navigation, multi-select, action menu
  search/
    fuzzy.py          fzf-style scorer + directory indexer
    view.py           the fuzzy picker
  shell/
    runner.py         host-shell wrap via asyncio subprocesses (per session)
    tabs.py           tabs — each bundles an explorer pair + a shell session
    completer.py      interactive path + command Tab-completion
    lexer.py          command-line syntax highlighting
    view.py           one session: scrollback (ANSI / CR / BS-aware, wrapped) + prompt
```

### Design notes
- **CJK widths.** Every column in the file list is padded/truncated with
  `util/width.py`, which measures rendered cell width via `wcwidth` instead of
  `len()`, so Korean/Chinese/Japanese names never break the layout. Git output
  is read with `core.quotepath=false` to keep unicode filenames intact.
- **Non-blocking everything.** Directory listing, Git status, file copies, the
  preview, and the search index all run off the event loop (an `asyncio` task or
  a worker thread) and `invalidate()` when done; the UI never waits on them.
  Stale results (for a directory you already left) are dropped.
- **Host shell.** Commands run through your platform's default shell
  (`$SHELL -c …` on Unix; `cmd /c …` or `powershell -Command …` on Windows),
  streamed in chunks so a `\r`-only progress bar updates in place. ANSI colour
  codes are interpreted (and colour is forced on via the child environment, since
  stdout isn't a TTY); for the same reason `PYTHONUNBUFFERED=1` is set so a
  Python script's `print`s stream live instead of block-buffering until it exits.
  Interactive programs (editors, pagers, `top`…) — and
  commands that may prompt for credentials, like network git and `sudo` — are
  detected and run with the full-screen UI temporarily suspended so they get a
  real terminal.
- **Cross-platform paths.** All path handling uses `pathlib`, so Windows `\` and
  POSIX `/` are handled uniformly.
