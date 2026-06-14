"""Theme, icons and the file/git -> style mappings used across the UI.

Colours and the explorer action keys can be overridden by the user's
``~/.config/nsh/nshrc`` (see :func:`load_user_config`).
"""
import configparser
import os
from pathlib import Path

from prompt_toolkit.styles import Style

STYLE_DEFAULTS = {
        # chrome
        "titlebar": "bg:#303030 #d0d0d0",
        # the "nsh" label: blends with the bar normally, lights up (menu.title)
        # while a popup menu is open
        "titlebar.name": "bg:#303030 #ffffff bold",
        "titlebar.path": "bg:#303030 #87d7ff bold",
        "titlebar.branch": "bg:#303030 #87ff87 bold",
        "titlebar.sel": "bg:#303030 #ffff5f bold",
        "titlebar.clock": "bg:#303030 #d0d0d0 bold",
        "statusbar": "bg:#1c1c1c #999999",
        "statusbar.key": "bg:#1c1c1c #5fafff bold",
        "statusbar.msg": "bg:#1c1c1c #ffff87",
        # explorer entries
        "explorer.dir": "#5fafff bold",
        "explorer.file": "#d0d0d0",
        "explorer.exec": "#5fff5f bold",
        "explorer.link": "#5fffff",
        "explorer.image": "#ff5fff",
        "explorer.selected": "#ffff5f bold",
        "explorer.size": "#808080",
        # git overlay
        "git.modified": "#ffaf00",
        "git.staged": "#5fff5f",
        "git.untracked": "#ff5f5f",
        "git.conflict": "#ff005f bold",
        # preview pane
        "preview": "#c0c0c0",
        "preview.header": "#87d7ff bold",
        "preview.dim": "#808080 italic",
        "preview.meta": "#5fafff bold",
        "preview.border": "#444444",
        # fuzzy search
        "search.prompt": "bg:#5f87af #ffffff bold",
        "search.input": "bg:#303030 #ffffff",
        "search.count": "bg:#303030 #999999",
        "search.results": "#d0d0d0",
        "search.match": "#ffaf00 bold",
        # shell
        "shell.output": "#d0d0d0",
        "shell.prompt": "#5fff5f bold",
        "shell.command": "#5fafff bold",
        "shell.option": "#ffaf00",
        "shell.string": "#ffff87",
        "shell.path": "#5fffff",
        "shell.error": "#ff5f5f",
        # completion popup
        "completion-menu.completion": "bg:#303030 #d0d0d0",
        "completion-menu.completion.current": "bg:#5fafff #000000 bold",
        "scrollbar.background": "bg:#303030",
        "scrollbar.button": "bg:#5fafff",
        # commit / input dialog
        "dialog": "bg:#1c1c1c",
        "dialog.label": "bg:#1c1c1c #ffff87 bold",
        "dialog.input": "bg:#303030 #ffffff",
        "dialog.button": "bg:#303030 #d0d0d0",
        "dialog.button.focus": "bg:#5fafff #000000 bold",
        "frame.border": "#5f87af",
        "frame.label": "#87d7ff bold",
        # popup action menu
        "menu": "bg:#1c1c1c",
        "menu.title": "bg:#5f87af #ffffff bold",
        "menu.item": "bg:#1c1c1c #d0d0d0",
        "menu.selected": "bg:#5fafff #000000 bold",
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
    "menu": "tab",
    "copy": "y",
    "cut": "x",
    "paste": "p",
    "delete": "D",
    "rename": "R",
    "new_dir": "m",
    "new_file": "N",
    "bookmark": "b",
    "find": "/",
    "command": ":",
    "preview": "P",
    "hidden": ".",
    "refresh": "r",
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
DEFAULT_NSHRC = """\
# nsh configuration file.
#
# [colors] overrides any UI style; values use prompt_toolkit syntax, e.g.
#   "#ff8700 bold", "bg:#1c1c1c #ffffff", "italic underline".
# [keys] remaps an explorer action to a key. A key is a single character, or a
#   name: space, tab, escape, enter, f1..f12, or a modifier form like c-r
#   (Ctrl-R) or s-tab (Shift-Tab). Navigation keys (arrows, j/k, …) are fixed.

[colors]
# explorer.dir = #5fafff bold
# explorer.selected = #ffff5f bold
# explorer.image = #ff5fff
# titlebar.name = bg:#303030 #ffffff bold
# titlebar.clock = bg:#303030 #d0d0d0 bold
# shell.command = #5fafff bold
# shell.string = #ffff87
# menu.selected = bg:#5fafff #000000 bold

[keys]
# select = space
# menu = tab
# copy = y
# cut = x
# paste = p
# delete = D
# rename = R
# new_dir = m
# new_file = N
# bookmark = b
# find = /
# command = :
# preview = P
# hidden = .
# refresh = r
# quit = q
"""


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "nsh"
    return Path.home() / ".config" / "nsh"


def config_path() -> Path:
    return config_dir() / "nshrc"


def ensure_default_config() -> None:
    """Seed a commented template nshrc on first run (best-effort)."""
    path = config_path()
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_NSHRC, encoding="utf-8")
    except OSError:
        pass


def _norm_key(value: str) -> str:
    value = value.strip()
    return " " if value.lower() == "space" else value


def load_user_config():
    """Return ``(color_overrides, key_overrides, warning)`` from nshrc."""
    colors, keys = {}, {}
    try:
        path = config_path()
    except RuntimeError:  # no home directory
        return colors, keys, None
    if not path.exists():
        return colors, keys, None
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # preserve case (style classes, key values)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        return colors, keys, f"nshrc not loaded (not valid INI): {path} - using defaults"
    if parser.has_section("colors"):
        colors = {k.strip(): v.strip() for k, v in parser.items("colors")}
    if parser.has_section("keys"):
        keys = {k.strip(): _norm_key(v) for k, v in parser.items("keys") if v.strip()}
    return colors, keys, None
