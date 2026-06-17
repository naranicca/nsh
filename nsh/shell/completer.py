"""Interactive Tab-completion for the command line.

Yields filesystem completions for the token under the cursor, plus ``$PATH``
command completions for the first token. After those exact (prefix) results it
appends *pseudo-fuzzy* matches — the typed characters with ``*`` inserted
between them — so e.g. ``abc`` also offers names containing a, b and c in order.
prompt_toolkit renders the results in a drop-down menu the arrow keys navigate.
"""
import fnmatch
import os
from pathlib import Path

from prompt_toolkit.completion import Completer, Completion

MAX_PATHS = 300
MAX_COMMANDS = 200


def _fuzzy_pattern(s):
    """A pseudo-fuzzy glob: insert ``*`` between every character so the typed
    characters need only appear in order (``abc`` -> ``*a*b*c*``). A leading
    ``~`` or ``/`` is kept anchored — the ``*`` run starts at the second
    character — so a home/root path isn't turned into a match-anything pattern.
    """
    if not s:
        return ""
    if s[0] in "~/":
        head, rest = s[0], s[1:]
    else:
        head, rest = "", s
    return head + "*" + "*".join(rest) + "*"


class ShellCompleter(Completer):
    def __init__(self, app):
        self.app = app

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        boundary = max(text.rfind(" "), text.rfind("\t"))
        token = text[boundary + 1:]
        is_first = boundary == -1
        want_cmd = is_first and token and not any(s in token for s in ("/", "\\"))

        # exact path matches, then the pseudo-fuzzy path matches appended after
        # them, then $PATH commands. (Fuzzy matching is deliberately not applied
        # to commands: over a Windows $PATH full of system DLLs it would bury the
        # useful results under thousands of subsequence hits.)
        seen = set()  # full resulting paths, so fuzzy doesn't repeat an exact hit

        def full(c):
            return token[:len(token) + c.start_position] + c.text

        try:
            for c in self._path_completions(token):
                seen.add(full(c))
                yield c
        except OSError:
            pass
        try:
            for c in self._fuzzy_path_completions(token):
                if full(c) in seen:
                    continue
                seen.add(full(c))
                yield c
        except OSError:
            pass
        if want_cmd:
            try:
                yield from self._command_completions(token)
            except OSError:
                pass

    # -- token -> (directory, name being completed) ---------------------------
    def _split(self, token):
        expanded = os.path.expanduser(token)
        ends_sep = token.endswith(("/", "\\"))
        if not expanded:
            return self.app.cwd, "", ends_sep
        p = Path(expanded)
        if ends_sep:
            base = p if p.is_absolute() else self.app.cwd / p
            return base, "", ends_sep
        base = p.parent if p.is_absolute() else self.app.cwd / p.parent
        return base, p.name, ends_sep

    @staticmethod
    def _path_completion(de, prefix):
        try:
            is_dir = de.is_dir()
        except OSError:
            is_dir = False
        comp = de.name + ("/" if is_dir else "")
        return Completion(
            comp,
            start_position=-len(prefix),
            display=comp,
            style="fg:ansiblue bold" if is_dir else "",
        )

    # -- paths ----------------------------------------------------------------
    def _path_completions(self, token):
        base, prefix, _ = self._split(token)
        count = 0
        for de in sorted(os.scandir(base), key=lambda e: e.name.lower()):
            name = de.name
            if prefix:
                if not name.lower().startswith(prefix.lower()):
                    continue
            elif name.startswith("."):
                continue
            yield self._path_completion(de, prefix)
            count += 1
            if count >= MAX_PATHS:
                break

    def _fuzzy_path_completions(self, token):
        """Fuzzy-match every path component, not just the last: ``sou/re/n``
        walks ``*s*o*u*`` then ``*r*e*`` then ``*n*`` to reach e.g.
        ``source/repos/nsh``. A leading ``~/`` or ``/`` is the (non-fuzzy)
        anchor and is preserved verbatim in the completion."""
        if token.startswith("~/"):
            anchor, base, rest = "~/", Path(os.path.expanduser("~")), token[2:]
        elif token.startswith("/"):
            anchor, base, rest = "/", Path(os.path.expanduser("/")), token[1:]
        elif token == "~":
            return
        else:
            anchor, base, rest = "", self.app.cwd, token
        if not rest:
            return
        components = rest.split("/")
        for rel, is_dir in self._fuzzy_walk(base, components):
            comp = anchor + rel + ("/" if is_dir else "")
            yield Completion(
                comp,
                start_position=-len(token),
                display=comp,
                style="fg:ansiblue bold" if is_dir else "",
            )

    def _fuzzy_walk(self, base, components, max_results=200, max_branch=40):
        """Walk ``base`` matching each component fuzzily; return ``(rel, is_dir)``
        for the leaves reached. Only directories are descended for the non-final
        components. Bounded by ``max_results`` and a per-level ``max_branch``."""
        frontier = [(base, "")]  # (directory, path relative to base so far)
        results = []
        for i, comp in enumerate(components):
            last = i == len(components) - 1
            pattern = _fuzzy_pattern(comp).lower() if comp else None
            nxt = []
            for dpath, rel in frontier:
                try:
                    entries = sorted(os.scandir(dpath), key=lambda e: e.name.lower())
                except OSError:
                    continue
                for de in entries:
                    name = de.name
                    if name.startswith(".") and not comp.startswith("."):
                        continue
                    if pattern and not fnmatch.fnmatchcase(name.lower(), pattern):
                        continue
                    try:
                        is_dir = de.is_dir()
                    except OSError:
                        is_dir = False
                    new_rel = f"{rel}/{name}" if rel else name
                    if last:
                        results.append((new_rel, is_dir))
                        if len(results) >= max_results:
                            return results
                    elif is_dir:
                        nxt.append((de.path, new_rel))
            if not last:
                frontier = nxt[:max_branch]
                if not frontier:
                    break
        return results

    # -- commands -------------------------------------------------------------
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
                    name, start_position=-len(token), display=name,
                    style="fg:ansigreen",
                )
                count += 1
                if count >= MAX_COMMANDS:
                    return
