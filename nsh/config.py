"""Theme, icons and the file/git -> style mappings used across the UI."""
from prompt_toolkit.styles import Style

STYLE = Style.from_dict(
    {
        # chrome
        "titlebar": "bg:#303030 #d0d0d0",
        "titlebar.mode": "bg:#5f87af #ffffff bold",
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
        # popup action menu
        "menu": "bg:#1c1c1c",
        "menu.title": "bg:#5f87af #ffffff bold",
        "menu.item": "bg:#1c1c1c #d0d0d0",
        "menu.selected": "bg:#5fafff #000000 bold",
    }
)

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
