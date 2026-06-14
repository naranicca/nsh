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

### Explorer mode
The action keys (everything below the navigation block) are remappable in
`nshrc` — see [Configuration](#configuration).

| Key | Action |
| --- | --- |
| `↑`/`↓`, `k`/`j` | move cursor |
| `↵`, `l`, `→` | open file / enter directory |
| `⌫`, `h`, `←` | go to parent directory |
| `Space` | select / deselect the entry (multi-select) |
| `Tab` | open the **action menu** (copy, rename, delete, git…) |
| `y` / `x` / `p` | copy / cut / paste (the selection, or the entry under the cursor) |
| `R` | rename |
| `m` / `N` | new folder / new file |
| `D` | delete (asks to confirm) |
| `b` | bookmarks — add/remove this directory, or jump to a saved one |
| `/` | fuzzy-find a file |
| `:` | switch to command-line mode |
| `P` | toggle the preview pane |
| `.` | toggle hidden files |
| `r` | refresh (the listing also auto-refreshes when the directory changes) |
| `?` | show this key list |
| `ESC` | clear the selection |
| `q` | quit |

Git actions (stage / unstage, commit, diff, and a **Branches** submenu that
lists branches to check out plus a `+ New Branch` entry) live in the `Tab`
action menu when the directory is a repository.

### Command-line mode
| Key | Action |
| --- | --- |
| typing | live syntax highlighting |
| `Tab` | completion popup (`↑`/`↓` to navigate, `↵` to pick) |
| `↑`/`↓` | command history (when no popup is open) |
| `↵` | run the command |
| `PgUp`/`PgDn`, wheel, `Ctrl+End` | scroll the output (the prompt hides while scrolled up) |
| `ESC` | switch back to explorer mode |

Built-ins handled internally: `cd`, `clear`/`cls`, `exit`/`quit`. The output pane
grows with its content and goes full-screen once it fills up.

### Fuzzy search mode
Type to filter, `↑`/`↓` to move, `↵` to select, `ESC` to cancel. Launched with
`nsh search [WORD]` (prints the selection to stdout, e.g. `cd "$(nsh search)"`)
or with `/` from the explorer.

## Configuration

On first run nsh seeds a commented template at `~/.config/nsh/nshrc`
(`$XDG_CONFIG_HOME` is honoured). It is a simple INI file:

```ini
[general]
# editor for the "Edit" action; unset -> $EDITOR/$VISUAL, then notepad/vi
editor = code -w

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

`[general]` sets the **Edit** editor (Tab menu, text files only); `[colors]`
overrides any UI style class; `[keys]` remaps the explorer action keys.
Invalid entries are ignored, never fatal.

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
    fileops.py        copy / move / delete / rename / mkdir (threaded)
    preview.py        side preview pane (text / dir / image dims / hexdump)
    view.py           file-list rendering, navigation, multi-select, action menu
  search/
    fuzzy.py          fzf-style scorer + directory indexer
    view.py           the fuzzy picker
  shell/
    runner.py         host-shell wrap via asyncio subprocesses
    completer.py      interactive path + command Tab-completion
    lexer.py          command-line syntax highlighting
    view.py           scrollback (ANSI / CR-aware) + prompt
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
  stdout isn't a TTY). Interactive programs (editors, pagers, `top`…) are detected
  and run with the full-screen UI temporarily suspended.
- **Cross-platform paths.** All path handling uses `pathlib`, so Windows `\` and
  POSIX `/` are handled uniformly.
