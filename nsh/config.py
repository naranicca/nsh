"""Theme, icons and the file/git -> style mappings used across the UI.

Colours and the explorer action keys can be overridden by the user's
``~/.config/nsh/nshrc`` (see :func:`load_user_config`).
"""
import configparser
import os
from pathlib import Path

from prompt_toolkit.styles import Style

from .search.fuzzy import SKIP_DIRS as _SEARCH_SKIP_DIRS

# the default value for the `search_exclude` setting, single-sourced from the
# fuzzy indexer so the seeded nshrc shows exactly what's skipped by default
_DEFAULT_SEARCH_EXCLUDE = " ".join(sorted(_SEARCH_SKIP_DIRS))

STYLE_DEFAULTS = {
        # chrome
        "titlebar": "bg:#303030 #d0d0d0",
        # the "nsh" label: blends with the bar normally, lights up (menu.title)
        # while a popup menu is open
        "titlebar.name": "bg:#303030 #ffffff bold",
        "titlebar.path": "bg:#303030 #87d7ff bold",
        "titlebar.branch": "bg:#303030 #87ff87 bold",
        "titlebar.branch.dirty": "bg:#303030 #ff5f5f bold",
        "titlebar.branch.behind": "bg:#303030 #ffaf00 bold",
        "titlebar.sel": "bg:#303030 #ffff5f bold",
        "titlebar.clock": "bg:#303030 #d0d0d0 bold",
        "statusbar": "bg:#1c1c1c #999999",
        "statusbar.key": "bg:#1c1c1c #5fafff bold",
        "statusbar.msg": "bg:#1c1c1c #ffff87",
        "statusbar.notes": "bg:#1c1c1c #ffd700 bold",
        # explorer entries
        "explorer.dir": "#5fafff bold",
        "explorer.file": "#d0d0d0",
        "explorer.exec": "#5fff5f bold",
        "explorer.link": "#5fffff",
        "explorer.image": "#ff5fff",
        "explorer.selected": "#ffff5f bold",
        "explorer.size": "#808080",
        # brief blink on a copy / cut, so the action is visible
        "explorer.flash": "bg:#ffd700 #000000 bold",
        # inline rename field (the edited name cell + its block cursor)
        "explorer.rename": "bg:#005f87 #ffffff",
        "explorer.rename.cursor": "bg:#d0d0d0 #000000",
        # git overlay
        "git.modified": "#ffaf00",
        "git.staged": "#5fff5f",
        "git.untracked": "#808080",
        "git.conflict": "#ff005f bold",
        # preview pane
        "preview": "#c0c0c0",
        "preview.header": "#87d7ff bold",
        # the pinned header gets a background while the preview pane is focused
        "preview.header.focus": "bg:#005f87",
        "preview.dim": "#808080 italic",
        "preview.meta": "#5fafff bold",
        "preview.border": "#444444",
        # fuzzy search
        # prompt_toolkit ships a built-in "search" class (its incremental-search
        # highlight) styled bg:ansibrightyellow; because style classes inherit
        # along dots, our search.* classes would pick up that yellow background.
        # Reset it here so the picker uses the normal background.
        "search": "noinherit",
        "search.prompt": "bg:#5f87af #ffffff bold",
        "search.input": "bg:#303030 #ffffff",
        "search.count": "bg:#303030 #999999",
        "search.results": "#d0d0d0",
        "search.match": "#ffaf00 bold",
        # selected row: a background only (no fg) so the match/dir colours show.
        # Deliberately NOT under the "search." namespace: as a child of "search"
        # it would re-apply that class's noinherit and wipe the match colour when
        # combined with search.match on a selected, matched character.
        "search-selected": "bg:#444444 bold",
        # shell
        "shell.output": "#d0d0d0",
        "shell.prompt": "#5fff5f bold",
        # the prompt dimmed while the previous command is still running (typing
        # here opens a new tab rather than running inline)
        "shell.prompt.dim": "#808080",
        # git branch in the prompt, coloured by repo state (like the title bar):
        # green in sync, yellow behind/ahead, red with uncommitted changes
        "shell.branch": "#87ff87",
        "shell.branch.behind": "#ffaf00",
        "shell.branch.dirty": "#ff5f5f",
        "shell.command": "#5fafff bold",
        "shell.option": "#ffaf00",
        "shell.string": "#ffff87",
        "shell.path": "#5fffff",
        "shell.error": "#ff5f5f",
        "shell.elapsed": "#ffaf00",  # running-time counter before the prompt
        # finished-command run time, tinted by exit status (green ok / red fail)
        "shell.elapsed.ok": "bg:#5faf5f #000000 bold",
        "shell.elapsed.err": "bg:#ff5f5f #000000 bold",
        # shell tab bar (multiple sessions)
        "shell.tabbar": "bg:#1c1c1c",
        "shell.tab": "bg:#1c1c1c #999999",
        "shell.tab.active": "bg:#5f87af #ffffff bold",
        # a tab whose command is still running goes orange (matching the busy
        # prompt's #ffaf00): orange text while inactive, an orange fill when it
        # is the active tab so it stays legible.
        "shell.tab.busy": "bg:#1c1c1c #ffaf00 bold",
        "shell.tab.active.busy": "bg:#ffaf00 #000000 bold",
        # a tab whose last command failed goes red (matching the failed prompt
        # badge #ff5f5f): red text while inactive, a red fill when it is active.
        "shell.tab.err": "bg:#1c1c1c #ff5f5f bold",
        "shell.tab.active.err": "bg:#ff5f5f #000000 bold",
        # the "+" button at the right end of the tab bar (click to open a tab)
        "shell.tab.new": "bg:#303030 #87d7ff bold",
        # completion popup
        "completion-menu.completion": "bg:#303030 #d0d0d0",
        "completion-menu.completion.current": "bg:#5fafff #000000 bold",
        "scrollbar.background": "bg:#303030",
        "scrollbar.button": "bg:#5fafff",
        # the preview scrollbar goes grayscale while the pane isn't focused
        "scrollbar.button.inactive": "bg:#808080",
        # commit / input dialog
        "dialog": "bg:#1c1c1c",
        "dialog.label": "bg:#1c1c1c #ffff87 bold",
        "dialog.input": "bg:#303030 #ffffff",
        "dialog.button": "bg:#303030 #d0d0d0",
        "dialog.button.focus": "bg:#5fafff #000000 bold",
        "frame.border": "#5f87af",
        "frame.label": "#87d7ff bold",
        # popup action menu. "menu" is the background; the unselected item rows
        # only set a foreground, so they inherit that background (prompt_toolkit
        # applies the parent "menu" class first) — meaning the "menu" colour
        # controls the menu background, as nshrc advertises.
        "menu": "bg:#1c1c1c",
        "menu.title": "bg:#5f87af #ffffff bold",
        "menu.item": "#d0d0d0",
        "menu.selected": "bg:#5fafff #000000 bold",
        "menu.separator": "#585858",  # divider row (bg inherited from menu)
        # notes mode
        "notes.label": "bg:#5f87af #ffffff bold",
        "notes.label.inactive": "bg:#303030 #d0d0d0 bold",
        "notes.input": "bg:#303030 #ffffff",
        "notes.item": "#d0d0d0",
        "notes.meta": "#5f87af",
        "notes.selected": "bg:#005f87 #ffffff",
        # process manager (system mode)
        "system.header": "bg:#1c1c1c #d0d0d0",
        "system.label": "bg:#1c1c1c #87d7ff bold",
        "system.bar": "bg:#1c1c1c #5fafff",
        "system.dim": "bg:#1c1c1c #808080",
        "system.colhead": "bg:#303030 #d0d0d0 bold",
        "system.sortcol": "bg:#303030 #ffff87 bold",
        "system.row": "#d0d0d0",
        "system.row.sel": "bg:#005f87 #ffffff",
}


def build_style(overrides=None):
    """A prompt_toolkit ``Style`` from the defaults plus user overrides."""
    merged = dict(STYLE_DEFAULTS)
    merged.update(overrides or {})
    return Style.from_dict(merged)


STYLE = build_style()

# Explorer action -> default key. These are the keys the user may remap in the
# ``[keys]`` section of nshrc. Navigation keys (arrows, j/k, enter…) are fixed.
DEFAULT_KEYS = {
    "select": " ",
    "select_pattern": "*",
    "two_pane": "2",
    # F7/F8: move between "siblings" — previous/next shell tab, the active pane
    # in two-pane mode, and list <-> preview focus. Remappable (ctrl combos OK).
    "pane_prev": "f7",
    "pane_next": "f8",
    # enlarge the focused pane to a 9:1 split (the big pane follows the focus)
    "zoom": "z",
    "menu": "tab",
    "copy": "y",
    "cut": "x",
    "paste": "p",
    "delete": "D",
    "rename": "f2",
    "new_dir": "m",
    "new_file": "N",
    "bookmark": "b",
    "home": "~",
    "visited": "-",
    "sort": "s",
    "find": "/",
    "command": ":",
    "preview": "P",
    "hidden": ".",
    "refresh": "r",
    "help": "?",
    "quit": "q",
}

# Single-cell glyphs (width 1) chosen for broad terminal support; colour carries
# most of the meaning, the icon is a secondary cue.
ICONS = {
    "dir": "▸",
    "file": " ",
    "exec": "*",
    "link": "↪",
    "image": "▦",
}

# git porcelain code -> (symbol, style)
GIT_SYMBOL = {"M": "M", "S": "S", "?": "?", "C": "!"}
GIT_STYLE = {
    "M": "class:git.modified",
    "S": "class:git.staged",
    "?": "class:git.untracked",
    "C": "class:git.conflict",
}


def entry_style(entry) -> str:
    if entry.is_link:
        return "class:explorer.link"
    if entry.is_dir:
        return "class:explorer.dir"
    if entry.is_image:
        return "class:explorer.image"
    if entry.is_exec:
        return "class:explorer.exec"
    return "class:explorer.file"


def entry_icon(entry) -> str:
    if entry.is_dir:
        return ICONS["dir"]
    if entry.is_link:
        return ICONS["link"]
    if entry.is_image:
        return ICONS["image"]
    if entry.is_exec:
        return ICONS["exec"]
    return ICONS["file"]


# -- user configuration (~/.config/nsh/nshrc) --------------------------------
# Default settings (overridable by nshrc's [general] section).
DEFAULT_SETTINGS = {
    # editor for the "Edit" action; empty -> $EDITOR/$VISUAL, then a platform default
    "editor": "",
    # explorer sort order: name | size | date | type, and whether to reverse it
    "sort": "name",
    "sort_reverse": "false",
    # start in two-pane view (two explorers side by side; F7/F8 switch panes)
    "two_pane": "false",
    # directory names skipped in fuzzy file search (comma/space separated). This
    # is the full list (not additive), so it can be edited to add or remove names
    "search_exclude": _DEFAULT_SEARCH_EXCLUDE,
}

DEFAULT_NSHRC = """\
# nsh configuration file.
#
# [general] sets miscellaneous options (see below).
# [colors] overrides any UI style; values use prompt_toolkit syntax, e.g.
#   "#ff8700 bold", "bg:#1c1c1c #ffffff", "italic underline".
# [keys] remaps an explorer action to a key. A key is a single character, or a
#   name: space, tab, escape, enter, f1..f12, or a modifier form like c-r
#   (Ctrl-R) or s-tab (Shift-Tab). A few ctrl combos can't be remapped because
#   the terminal sends them as another key: c-i is Tab, c-m is Enter, c-h is
#   Backspace, c-[ is Esc. Navigation keys (arrows, j/k, …) are fixed.

[general]
# editor used by the "Edit" action (Tab menu). When unset, nsh falls back to
# $EDITOR / $VISUAL, then to notepad (Windows) or vi (Linux/macOS).
# editor = code -w
# editor = vim

# Explorer sort order (change live with the 's' key). sort: name|size|date|type
# sort = name
# sort_reverse = false

# Start in two-pane view: two explorer panes side by side (no preview), each
# with its own directory. F7/F8 move the cursor between them. Toggle any time
# from the F10 menu.
# two_pane = false

# Directories skipped by fuzzy file search (/ key), comma or space separated.
# This is the full list — edit it to add names (e.g. a noisy build/) or remove
# names (to search them); clear it to search everything. build/ and dist/ are
# searched by default. The default is:
# search_exclude = {search_defaults}

[colors]
# explorer.dir = #5fafff bold
# explorer.selected = #ffff5f bold
# explorer.image = #ff5fff
# titlebar.name = bg:#303030 #ffffff bold
# titlebar.clock = bg:#303030 #d0d0d0 bold
# shell.command = #5fafff bold
# shell.string = #ffff87
# menu = bg:#1c1c1c            (popup action-menu background)
# menu.title = bg:#5f87af #ffffff bold
# menu.item = #d0d0d0          (unselected rows; bg inherited from menu)
# menu.selected = bg:#5fafff #000000 bold

[keys]
# select = space
# select_pattern = *   (select files by glob/substring pattern)
# two_pane = 2         (toggle the two-pane view)
# pane_prev = f7       (previous tab)
# pane_next = f8       (next tab)
# zoom = z             (enlarge the focused pane to a 9:1 split)
# menu = tab
# copy = y
# cut = x
# paste = p
# delete = D
# rename = f2
# new_dir = m
# new_file = N
# bookmark = b
# home = ~          (jump to your home directory)
# visited = -       (menu of recently visited directories)
# sort = s          (change the file sort order)
# find = /
# command = :
# preview = P
# hidden = .
# refresh = r
# help = ?
# quit = q
"""


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "nsh"
    return Path.home() / ".config" / "nsh"


def config_path() -> Path:
    return config_dir() / "nshrc"


def cache_dir() -> Path:
    """Where transient, machine-written state lives (UI toggles, lastdir, …) —
    ``$XDG_CACHE_HOME/nsh`` or ``~/.cache/nsh``, separate from the user's config."""
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "nsh"
    return Path.home() / ".cache" / "nsh"


def ensure_default_config() -> None:
    """Seed a commented template nshrc on first run (best-effort)."""
    path = config_path()
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            text = DEFAULT_NSHRC.format(search_defaults=_DEFAULT_SEARCH_EXCLUDE)
            path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _norm_key(value: str) -> str:
    # A key spec is a single token (no spaces), so keep only the first word: this
    # drops any trailing "(...)" annotation the user left in place after
    # uncommenting a template line (e.g. "f8   (next tab / switch pane)").
    value = value.strip().split()[0] if value.strip() else ""
    return " " if value.lower() == "space" else value


def load_user_config():
    """Return ``(color_overrides, key_overrides, settings, warning)`` from nshrc."""
    colors, keys, settings = {}, {}, dict(DEFAULT_SETTINGS)
    try:
        path = config_path()
    except RuntimeError:  # no home directory
        return colors, keys, settings, None
    if not path.exists():
        return colors, keys, settings, None
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # preserve case (style classes, key values)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        return colors, keys, settings, f"nshrc not loaded (not valid INI): {path} - using defaults"
    if parser.has_section("colors"):
        colors = {k.strip(): v.strip() for k, v in parser.items("colors")}
    if parser.has_section("keys"):
        keys = {k.strip(): _norm_key(v) for k, v in parser.items("keys") if v.strip()}
    if parser.has_section("general"):
        for k, v in parser.items("general"):
            if k.strip() in settings:
                settings[k.strip()] = v.strip()
    return colors, keys, settings, None
