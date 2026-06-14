"""Interactive Tab-completion for the command line.

Yields filesystem completions for the token under the cursor, plus ``$PATH``
command completions for the first token.  prompt_toolkit renders the results in
a drop-down menu that the arrow keys navigate.
"""
import os
from pathlib import Path

from prompt_toolkit.completion import Completer, Completion

MAX_PATHS = 300
MAX_COMMANDS = 200


class ShellCompleter(Completer):
    def __init__(self, app):
        self.app = app

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        boundary = max(text.rfind(" "), text.rfind("\t"))
        token = text[boundary + 1:]
        is_first = boundary == -1

        try:
            yield from self._path_completions(token)
        except OSError:
            pass
        if is_first and token and not any(s in token for s in ("/", "\\")):
            try:
                yield from self._command_completions(token)
            except OSError:
                pass

    def _path_completions(self, token):
        expanded = os.path.expanduser(token)
        ends_sep = token.endswith(("/", "\\"))
        if not expanded:
            base, prefix = self.app.cwd, ""
        else:
            p = Path(expanded)
            if ends_sep:
                base = p if p.is_absolute() else self.app.cwd / p
                prefix = ""
            else:
                base = p.parent if p.is_absolute() else self.app.cwd / p.parent
                prefix = p.name

        count = 0
        for de in sorted(os.scandir(base), key=lambda e: e.name.lower()):
            name = de.name
            if prefix:
                if not name.lower().startswith(prefix.lower()):
                    continue
            elif name.startswith("."):
                continue
            try:
                is_dir = de.is_dir()
            except OSError:
                is_dir = False
            comp = name + ("/" if is_dir else "")
            yield Completion(
                comp,
                start_position=-len(prefix),
                display=comp,
                style="fg:ansiblue bold" if is_dir else "",
            )
            count += 1
            if count >= MAX_PATHS:
                break

    def _command_completions(self, token):
        seen = set()
        count = 0
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory:
                continue
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if not name.lower().startswith(token.lower()):
                    continue
                if name in seen:
                    continue
                seen.add(name)
                yield Completion(
                    name,
                    start_position=-len(token),
                    display=name,
                    style="fg:ansigreen",
                )
                count += 1
                if count >= MAX_COMMANDS:
                    return
