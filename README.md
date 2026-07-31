# nsh

## nsh is Not a SHell!

A cross-platform (Windows / Linux / macOS) interactive **file explorer + shell**,
rewritten in Python on top of [`prompt_toolkit`](https://python-prompt-toolkit.readthedocs.io/).
It is CJK-aware throughout and includes FTP/SFTP remote browsing.

Its primary modes are:

1. **Explorer** (default) — an `mc`/`lf`-style directory pane with type-coloured
   entries, icons, a live Git status overlay, multi-select, file operations, and
   a side preview pane. Press **`:`** to drop into the shell, **`/`** to fuzzy-find.
2. **Command-line** — drives your host shell with syntax highlighting, an
   interactive Tab-completion popup, ANSI-coloured / progress-bar-aware output,
   and a scrollback you can page through. Press **`ESC`** to return to the explorer.
3. **Fuzzy search** — an fzf-style file picker (`/` from the explorer, or
   `nsh search` from the command line).
4. **Network** — an FTP/SFTP remote browser with recursive transfers and file
   operations, opened from **F10 → Network**.

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
**`Shift+H`** / **`Shift+L`** move focus left / right across the on-screen
columns — the two panes, or (in single-pane view) the list and its preview — or
click a pane to focus it.

### Explorer mode
The action keys (everything below the navigation block) are remappable in
`nshrc` — see [Configuration](#configuration).

| Key | Action |
| --- | --- |
| `↑`/`↓`, `k`/`j` | move cursor |
| `↵` | open the file / enter the directory |
| `l`, `→` | expand/collapse a directory inline; **on a file, focus the preview** |
| `⌫`, `h`, `←` | collapse the directory, else go to the parent |
| `Shift+H` / `Shift+L` | move focus left / right across columns — the two panes (two-pane view), or the list and its preview |
| `Space` | select / deselect the entry (multi-select) |
| `Tab` | open the **action menu** (copy, rename, delete, git…) — drops from the cursor row, beside the filename |
| `y` / `x` / `p` | copy / cut / paste — the picked rows briefly flash; **paste lands in the directory at the cursor**. The clipboard is shared across tabs, so you can copy in one tab and paste in another |
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
exactly where you're pointing. Paste is stricter: it only drops *inside* a
directory you've already **expanded**; on a collapsed one it pastes into the
container beside it, not into a folder you can't see into.

Git actions (stage / unstage, commit, diff, and a **Branches** submenu that
lists branches to check out plus a `+ New Branch` entry) live in the `Tab`
action menu when the directory is a repository. An untracked file can be staged
— including the files inside a brand-new directory, which carry the untracked
marker and so can be added too.

Picking a branch opens a per-branch menu (Checkout, **Browse**, Delete). **Browse**
pops a small centered dialog listing that branch's files without checking it out:
`↑`/`↓` (or `j`/`k`) move, `↵` / `l` / `→` step into a directory and `⌫` / `h` /
`←` back out, and **`y`** copies the highlighted file — or a whole directory —
out of the branch into the current explorer directory (never clobbering an
existing name). `Esc` / `q` closes it.

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
current one is still running pops a centered dialog box — queue it (it runs in
this tab once the running command, and anything queued ahead of it, finishes) or
run it now in a new tab (rather than mixing the output); several commands can wait in line and
run in order. Queued commands are listed in grey above the prompt, with a `⋯ N
queued` count in the status bar. A tab bar marks each session's state — orange
while a command is still running, red once one finishes with a non-zero exit
(cleared when the next command runs).

Built-ins handled internally: `cd`, `clear`/`cls`, `exit`/`quit`. The output pane
grows with its content and goes full-screen once it fills up. Long lines wrap,
the prompt shows each command's run time tinted by its exit status (and dims
itself while a command is still running, since entering one then prompts to queue
it or open a new tab), and
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

### Network mode (FTP / SSH-SFTP)

Network mode places the active local explorer pane on the left and an FTP or
SSH-SFTP directory tree on the right. Open **F10 → Network** from the local pane
you want to use as the transfer endpoint, then choose a connection type. Both
directories remain visible while files are transferred between them.

Connection targets use `[user@]host[:port][/initial/path]`:

```text
alice@example.com:22/home/alice       # SFTP
files@example.com:21/incoming         # FTP
internal-server/projects              # SSH alias + initial path
```

SFTP defaults to port 22 and the current operating-system user; FTP defaults
to port 21 and anonymous login. The password dialog is masked and cleared as
soon as it closes. Leave the SFTP password blank to use `IdentityFile` entries,
your SSH agent, or Paramiko's normal private-key discovery. Leave an anonymous
FTP password blank to use `anonymous@`.

After a successful SFTP connection, its target (never its password) is saved in
the UI state and prefilled the next time the SFTP connection dialog opens.

#### SSH config and jump hosts

SFTP resolves aliases through `~/.ssh/config`, including `HostName`, `User`,
`Port`, `IdentityFile`, `ProxyJump`, and `ProxyCommand`. For example:

```sshconfig
Host bastion
    HostName bastion.example.com
    User jumpuser
    IdentityFile ~/.ssh/bastion_ed25519

Host internal
    HostName 10.20.0.15
    User deploy
    IdentityFile ~/.ssh/internal_ed25519
    ProxyJump bastion
```

Choose SFTP and enter `internal/var/www` as the target. At the next prompt,
leave **Jump host** empty: nsh reads `ProxyJump bastion` from the config and
opens the final SFTP session through an SSH `direct-tcpip` channel.

A jump host can also be entered explicitly as
`jumpuser@bastion.example.com:2222`. Comma-separated chains such as
`edge@edge-host,core@core-host` are supported. An explicit value overrides the
destination's `ProxyJump` setting. Each hop resolves its own SSH config and is
closed in reverse order when the connection ends or fails.

When no jump host or `ProxyJump` is active, nsh also opens the expanded
`ProxyCommand` as the SSH transport socket. Standard tokens such as `%h`, `%p`,
and `%r` are expanded by the SSH config parser. The proxy process is closed when
the connection ends or fails.

Every SSH host key is checked independently against the user's system
`known_hosts`. On first connection nsh shows the key type and SHA256 fingerprint
for approval; verify it with the server owner. An approved key is saved to
`~/.ssh/known_hosts` and the connection is retried. A key that conflicts with
an existing entry is still rejected without an approval prompt. A password
entered in nsh is available as an authentication fallback for the destination
and every jump host, while per-host keys and the SSH agent remain preferred
alternatives.

#### Browsing and file operations

| Key | Action |
| --- | --- |
| `↑`/`↓`, `j`/`k` | move cursor |
| `g`/`Home`, `G`/`End` | first / last row |
| `~` | open the remote login home directory |
| `s` | sort by name, size, date, or type in either direction |
| `/` | fuzzy-find files and directories below the remote directory |
| `:` | open a command shell over the current SSH connection |
| `↵` | enter a directory; download a file |
| `l`, `→` | expand / fold a remote directory inline |
| `⌫`, `h`, `←` | fold, move to the tree parent, or open the parent directory |
| `Space` | select / deselect |
| `Shift+H` / `Shift+L` | focus the local / remote pane |
| `c` | local focus: upload; remote focus: download |
| `n` | create remote directory |
| `i` | rename remote item |
| `D` | permanently delete selected remote items |
| `Tab` | remote actions menu |
| `r` | refresh |
| `Esc` | clear the focused pane's selection |
| `q` | quit nsh |

With the remote pane focused, `c` downloads into the displayed local pane's
current directory. With the local pane focused, `c` uploads its marked
selection—or its cursor item when nothing is marked—into the displayed remote
directory. Files and whole directory trees are supported in both directions.
`Enter` on a remote file is a download shortcut; `Enter` on a directory opens
it. A transfer refreshes only its destination pane.

The remote pane uses the same tree presentation as the local file pane: file
sizes are right-aligned, directories use `▸`/`▾` carets, expanded children are
indented, and the cursor highlights the complete row with the explorer colour
scheme. A `..` row is shown outside the remote root. Directory contents are
fetched only when first expanded.

On an SFTP connection, `:` opens a remote command shell that reuses the active
authenticated SSH session. Commands run in the directory displayed by the
remote file pane. `cd` updates that pane's directory, `clear` clears remote
shell output, and `exit`/`quit` or `Esc` returns to the remote files. Commands
are executed one at a time without an interactive PTY, so full-screen programs
such as `vim` and `top` are not supported in this view.

Transfers and recursive delete operations run in worker threads so the TUI can
continue repainting. Existing names are never overwritten: uploads and
downloads use `name (2)`, `name (3)`, and so on. Disconnect is blocked while an
operation is active, and connection/transfer errors remain visible in the
status bar.

Disconnect deliberately has no single-key shortcut. Choose `Disconnect` from
the `Tab` remote-actions menu (or `F10` network menu) and approve the
confirmation prompt. The `2` key is disabled while this fixed local/remote
layout is open, so it cannot alter the hidden local explorer split.

Current limitations: transfers cannot yet be resumed, there is no remote-to-
remote copy/move or chmod action, credentials are not saved, and only plain FTP
(not FTPS) is implemented. Plain FTP sends credentials and data without
encryption and should only be used with trusted legacy servers; prefer SFTP for
normal use.

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
  app.py              application modes, layout, central key dispatch, cwd/git state
  network/
    backend.py         FTP/SFTP connection and remote filesystem operations
    view.py            remote browser, transfers and action bindings
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
