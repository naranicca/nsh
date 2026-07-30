# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What nsh is

**nsh is Not a SHell** — a console (TUI) file manager. Its design goals are constraints, not slogans, and they shape every implementation choice:

- **No install, no dependencies, no sudo.** It must "just work" by running a single file. Don't introduce hard third-party dependencies; treat external tools (`bat`, `htop`, image viewers) as optional and degrade gracefully when absent.
- **Cross-platform between Linux and macOS.** The code branches on `$OSTYPE` (`darwin*`) and probes for GNU-vs-BSD differences in core utilities at startup (e.g. `ls --color` vs `-G`, `stat --printf` vs `-f`, `date +%s%N` vs the macOS fallback, `--time-style`). Any new use of `ls`/`stat`/`date`/`sed` must keep both variants working.

## Repository state — mid-rewrite

This repo is being rewritten from BASH to Python. The current branch is `python`, whose tip commit ("removed previous implementations") deleted the original source, so the working tree is effectively empty.

**The canonical design reference is `nsh.sh` on the `main` / `dev` branches** (~5000 lines, single file). When implementing the Python port, read the corresponding BASH behavior first:

```bash
git show main:nsh.sh            # view the full original implementation
git show main:nsh.sh | sed -n '1631,4972p'   # the main nsh() function
```

Other branches (`nshgit`, `new-ui`, `dev`) are feature branches of the same single-file BASH program; diff them against `main` to see in-progress features.

## Running / developing (BASH version)

There is **no build system, no linter config, and no test suite** — it is a single executable script.

```bash
bash nsh.sh                 # run the file manager (or ./nsh.sh)
source nsh.sh; nsh          # sourcing defines the `nsh` function instead of auto-running
nsh search [WORD]           # start in fuzzy file-search mode (prints selection to stdout)
nsh shell                   # start directly in the command-line subshell
nsh -h | -v                # help / version
```

The `(return 0 2>/dev/null) || nsh "$@"` guard at the end of the file is what makes the script both sourceable and directly runnable. Preserve that dual behavior in any rewrite.

State lives outside the repo: config at `~/.config/nsh/nshrc` (seeded from the `NSH_DEFAULT_CONFIG` heredoc at the top of the file), `~/.config/nsh/bookmarks`, and a cache in `~/.cache/nsh/` (`lastdir`, `history`, plus the `2048` game state).

## Architecture (from `nsh.sh`)

The program is a hand-rolled terminal UI built directly on ANSI escape codes — there is no curses/ncurses. Everything is layered bottom-up in one file:

1. **Preferences / theming** — the `NSH_*` variables (colors, prompt, preview commands, items-to-hide). User config is `source`d over these defaults at startup.
2. **Terminal primitives** — cursor/screen control (`hide_cursor`, `open_screen`, `move_cursor`, `get_cursor_pos`), raw keypress reading (`get_key`, which decodes escape sequences into key names), and line-editing input (`read_string`, with fuzzy completion via `fuzzy_word` / `get_common_string`).
3. **`menu()`** — a reusable interactive selectable-list / popup widget used pervasively (process pickers, confirmations, git views). Learn this before touching anything that shows a list.
4. **Rendering** — file-list rendering (`nshls`, `print_filename`, `put_file_color`), the side info / preview pane, scrollbar, and title bar.
5. **Built-in reimplementations** — file ops with progress (`nshcp`, `nshmv`), `nshgrep`, a git front-end (`nshgit` plus `git_log`, `git_branch`, `git_commit_preview`, `git_fix_conflicts` — note `GIT_COMMANDS` near the top lists the git subcommands it recognizes), system monitors (`cpu`, `mem`, `gpu`, `disk`), and the `play2048` easter egg.
6. **`subshell` / `nsheval`** — an interactive command line with live fuzzy suggestion. `nsheval` rewrites input before executing (e.g. bare directory → `cd`, `~` expansion, `..`/`...` → parent dirs, persisting `lastdir`). This is where shell-like behavior is emulated; it is **not** a real shell.
7. **`nsh_main_loop`** — the modal event loop and central key dispatcher (big `case $KEY in` block). Modes are held in `nsh_mode`: empty = normal file-manager, `search` = pick-a-file-and-print, `shell` = subshell. Most actions delegate to a `subshell --oneshot <builtin>` invocation. `NEXT_KEY` lets handlers queue a synthetic keypress to drive the next iteration.

When adding a feature, identify which layer it belongs to and reuse the layer below it (e.g. any new chooser should go through `menu()`, any new input prompt through `read_string`) rather than re-implementing terminal handling.
