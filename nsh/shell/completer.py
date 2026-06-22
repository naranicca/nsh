"""Interactive Tab-completion for the command line.

Yields filesystem completions for the token under the cursor, plus ``$PATH``
command completions for the first token. After those exact (prefix) results it
appends *pseudo-fuzzy* matches — the typed characters with ``*`` inserted
between them — so e.g. ``abc`` also offers names containing a, b and c in order.
prompt_toolkit renders the results in a drop-down menu the arrow keys navigate.

A name that contains a space is wrapped in double quotes so the shell sees it as
one argument. Each completion replaces the *whole* token (including any opening
quote the user already typed), and a directory keeps its quote left open
(``"New folder/``) so you can keep drilling into it.
"""
import fnmatch
import os
from pathlib import Path

from prompt_toolkit.completion import Completer, Completion

from . import quoting

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

    @staticmethod
    def _current_token(text):
        """Return the start index of the token under the cursor, quote-aware: a
        space inside ``'`` or ``"`` quotes does not start a new token, so a
        partially typed ``"New fo`` is treated as one token."""
        start, quote = 0, ""
        for i, ch in enumerate(text):
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in ("'", '"'):
                quote = ch
            elif ch in (" ", "\t"):
                start = i + 1
        return start

    def _is_posix(self):
        try:
            return self.app.shell.runner._is_posix
        except Exception:
            return os.name != "nt"

    def _quote(self, full, is_dir):
        """Wrap ``full`` in double quotes when it contains a shell metacharacter
        (a space, parentheses, ``&``, …). A directory is left with its quote
        open (no closing ``"``) so the menu can be reopened to drill in; a file
        gets a closing quote. On POSIX the characters still special inside double
        quotes are backslash-escaped.

        A leading ``~`` (or ``~user``) is kept *outside* the quotes — the shell
        only performs tilde expansion on an unquoted tilde, so quoting the whole
        path would leave a literal ``~`` the shell can't resolve."""
        prefix, rest = "", full
        if full.startswith("~"):
            sep = min((i for i in (full.find("/"), full.find("\\")) if i != -1),
                      default=-1)
            if sep != -1:
                prefix, rest = full[:sep + 1], full[sep + 1:]
            else:  # ~name with no separator: ~ still goes outside
                prefix, rest = full[:1], full[1:]
        if not quoting.needs_quoting(rest):
            return prefix + rest
        body = quoting.quote_body(rest, self._is_posix())
        if is_dir and rest.endswith(("/", "\\")):
            return prefix + '"' + body
        return prefix + '"' + body + '"'

    def _completion(self, raw, full, display, is_dir, style):
        return Completion(
            self._quote(full, is_dir),
            start_position=-len(raw),
            display=display,
            style=style,
        )

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        start = self._current_token(text)
        raw = text[start:]
        quoted = raw[:1] in ("'", '"')
        if quoted:
            value = raw[1:]
            if value.endswith(raw[0]):
                value = value[:-1]
        else:
            value = raw
        is_first = start == 0
        want_cmd = (
            is_first and value and not quoted
            and not any(s in value for s in ("/", "\\"))
        )

        # exact path matches, then the pseudo-fuzzy path matches appended after
        # them, then $PATH commands. (Fuzzy matching is deliberately not applied
        # to commands: over a Windows $PATH full of system DLLs it would bury the
        # useful results under thousands of subsequence hits.)
        seen = set()  # full (unquoted) paths, so fuzzy doesn't repeat an exact hit
        try:
            for full, comp in self._path_completions(raw, value):
                seen.add(full)
                yield comp
        except OSError:
            pass
        try:
            for full, comp in self._fuzzy_path_completions(raw, value):
                if full in seen:
                    continue
                seen.add(full)
                yield comp
        except OSError:
            pass
        if want_cmd:
            try:
                yield from self._command_completions(raw, value)
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

    # -- paths ----------------------------------------------------------------
    def _path_completions(self, raw, value):
        base, prefix, _ = self._split(value)
        head = value[:len(value) - len(prefix)]  # the typed dir portion, verbatim
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
            disp = name + ("/" if is_dir else "")
            full = head + disp
            style = "fg:ansiblue bold" if is_dir else ""
            yield full, self._completion(raw, full, disp, is_dir, style)
            count += 1
            if count >= MAX_PATHS:
                break

    def _fuzzy_path_completions(self, raw, value):
        """Fuzzy-match every path component, not just the last: ``sou/re/n``
        walks ``*s*o*u*`` then ``*r*e*`` then ``*n*`` to reach e.g.
        ``source/repos/nsh``. A leading ``~/`` or ``/`` is the (non-fuzzy)
        anchor and is preserved verbatim in the completion."""
        if value.startswith("~/"):
            anchor, base, rest = "~/", Path(os.path.expanduser("~")), value[2:]
        elif value.startswith("/"):
            anchor, base, rest = "/", Path(os.path.expanduser("/")), value[1:]
        elif value == "~":
            return
        else:
            anchor, base, rest = "", self.app.cwd, value
        if not rest:
            return
        components = rest.split("/")
        for rel, is_dir in self._fuzzy_walk(base, components):
            full = anchor + rel + ("/" if is_dir else "")
            disp = rel.split("/")[-1] + ("/" if is_dir else "")
            style = "fg:ansiblue bold" if is_dir else ""
            yield full, self._completion(raw, full, disp, is_dir, style)

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
    def _command_completions(self, raw, value):
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
                if not name.lower().startswith(value.lower()):
                    continue
                if name in seen:
                    continue
                seen.add(name)
                yield self._completion(raw, name, name, False, "fg:ansigreen")
                count += 1
                if count >= MAX_COMMANDS:
                    return
