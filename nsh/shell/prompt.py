"""The command-line prompt fragments (cwd + git branch + ``$``).

Shared by the live shell prompt, the scrollback echo, and the terminal echo
printed before a command that runs outside nsh (``run_in_term``), so all three
render the prompt identically.
"""
from ..util.paths import shorten_home


def prompt_fragments(app):
    """The prompt as ``[(style, text), ...]``: the cwd in the explorer's
    directory colour, the git branch as ` (branch)`, then ``$`` in the default
    text colour.

    The branch is coloured by repo state like the title bar: yellow when
    behind/ahead the upstream, red with uncommitted changes, else green."""
    frags = [("class:explorer.dir", shorten_home(app.cwd))]
    gs = app.git_status
    if gs and gs.is_repo and gs.branch:
        # same precedence as the title bar: behind shows -count, uncommitted
        # changes show red (no count), ahead shows +count, else green
        if gs.behind > 0:
            style, suffix = "class:shell.branch.behind", f" -{gs.behind}"
        elif gs.dirty:
            style, suffix = "class:shell.branch.dirty", ""
        elif gs.ahead > 0:
            style, suffix = "class:shell.branch.behind", f" +{gs.ahead}"
        else:
            style, suffix = "class:shell.branch", ""
        frags.append((style, f" ({gs.branch}{suffix})"))
    frags.append(("", "$ "))
    return frags
