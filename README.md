# nsh

## nsh is Not a SHell!

A cross-platform (Windows / Linux / macOS) interactive **shell + file explorer**,
rewritten in Python on top of [`prompt_toolkit`](https://python-prompt-toolkit.readthedocs.io/).

It runs in two modes, toggled with **`ESC`**:

1. **Interactive mode (file explorer)** — an `mc`/`lf`-style directory pane with
   type-coloured entries, icons, and live Git integration.
2. **Command-line mode** — a normal shell prompt that drives your host shell,
   with syntax highlighting and an interactive Tab-completion popup.

## Install / run

```sh
pip install -e .        # installs prompt_toolkit + wcwidth
nsh                     # or:  python -m nsh
```

Requires Python 3.8+.

## Keys

### Interactive (explorer) mode
| Key | Action |
| --- | --- |
| `↑`/`↓`, `k`/`j` | move cursor |
| `↵`, `l`, `→` | open file / enter directory |
| `⌫`, `h`, `←` | go to parent directory |
| `Space` | stage / unstage the file under the cursor (Git) |
| `c` | commit (prompts for a message) |
| `d` | show the file's diff |
| `.` | toggle hidden files |
| `r` | refresh |
| `ESC` | switch to command-line mode |
| `q` | quit |

### Command-line mode
| Key | Action |
| --- | --- |
| typing | live syntax highlighting |
| `Tab` | open the completion popup (`↑`/`↓` to navigate, `↵` to pick) |
| `↑`/`↓` | command history (when no popup is open) |
| `↵` | run the command |
| `ESC` | switch back to explorer mode |

Built-ins handled internally: `cd`, `clear`/`cls`, `exit`/`quit`.

## Architecture

```
nsh/
  app.py              two modes, layout, central key dispatch, cwd/git state
  config.py           Style, icons, file/git -> colour mappings
  util/
    width.py          wcwidth-based truncate/pad (CJK-correct columns)
    paths.py          pathlib helpers, normalised compare keys
  explorer/
    model.py          os.scandir directory listing
    git.py            async git status/branch/stage/commit/diff
    view.py           file-list rendering + navigation + lazygit-style keys
  shell/
    runner.py         host-shell wrap via asyncio.create_subprocess_exec
    completer.py      interactive path + command Tab-completion
    lexer.py          command-line syntax highlighting
    view.py           scrollback + prompt
```

### Design notes
- **CJK widths.** Every column in the file list is padded/truncated with
  `util/width.py`, which measures rendered cell width via `wcwidth` instead of
  `len()`, so Korean/Chinese/Japanese names never break the layout. Git output
  is read with `core.quotepath=false` to keep unicode filenames intact.
- **Non-blocking Git.** Entering a directory schedules an `asyncio` task that
  runs `git` in a subprocess and calls `invalidate()` when done; the UI thread
  never waits on it. Stale results (for a directory you already left) are dropped.
- **Host shell.** Commands run through your platform's default shell
  (`$SHELL -c …` on Unix; `cmd /c …` or `powershell -Command …` on Windows),
  streamed line-by-line. Interactive programs (editors, pagers, `git`, `top`…)
  are detected and run with the full-screen UI temporarily suspended.
- **Cross-platform paths.** All path handling uses `pathlib`, so Windows `\` and
  POSIX `/` are handled uniformly.
