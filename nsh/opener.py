"""Extension-based open rules: which program opens which file.

Two layers, like yazi's ``[opener]`` / ``[open]``::

    [openers]
    play = mpv %f                 # a named, reusable way to open something

    [open]
    *.{mp4,mkv} = play, default   # which files use it, best choice first

The value of an ``[open]`` line is a comma-separated list of opener names, so
the same program is described once and reused. A token that is not a known name
is taken as a command written inline, so a one-off needs no name at all.

Patterns are globs matched against the file *name* (never the whole path),
case-insensitively, with ``{a,b}`` brace expansion. There is deliberately no
MIME matching: it would mean ``file``/libmagic, which does not exist on Windows.

The first matching line wins. A line whose programs are all missing from PATH is
skipped and matching continues, so one nshrc can describe several machines —
ranger's ``has`` condition without the syntax.
"""
import fnmatch
import os
import re
import shlex
import shutil
from collections import namedtuple
from pathlib import Path

# Openers nsh performs itself. They always work (no program on PATH is needed)
# and their names are reserved: [openers] may not redefine them.
BUILTIN_OPENERS = ("default", "edit", "browse", "preview", "shell")
BUILTIN_HELP = {
    "default": "the system's default application",
    "edit": "the editor from [general] editor",
    "browse": "step into it (directory or archive)",
    "preview": "focus the preview pane",
    "shell": "put the path on the command line",
}

# Patterns that match everything. A catch-all rule never overrides stepping into
# a directory or an archive — only a pattern that names the kind of file, such
# as ``*.zip``, is specific enough to claim one.
CATCH_ALL = frozenset({"*", "*.*"})

# %f file  %F every selected file  %d containing directory  %n name without suffix
_MACRO = re.compile(r"%(.)")
_ANY_MACRO = re.compile(r"%[fFdn]")
_SPREAD_MACRO = re.compile(r"%F")
# what may be an opener *name* rather than a command: one bare word
_NAME_LIKE = re.compile(r"^[A-Za-z0-9_.+-]+$")


class Opener(namedtuple("Opener", "name command")):
    """A way to open a file. ``command`` is empty for a built-in opener."""
    __slots__ = ()

    @property
    def label(self):
        """What the "Open with…" menu shows: the name, else the command."""
        return self.name or self.command

    @property
    def is_builtin(self):
        return not self.command


Rule = namedtuple("Rule", "pattern globs openers")


# -- patterns -------------------------------------------------------------
def _split_top_level(text):
    """Split ``text`` on commas that are not inside a nested ``{}`` group."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def expand_braces(pattern):
    """``*.{png,jpg}`` -> ``['*.png', '*.jpg']``. Unbalanced braces stay literal."""
    start = pattern.find("{")
    if start == -1:
        return [pattern]
    depth = 0
    end = -1
    for i in range(start, len(pattern)):
        if pattern[i] == "{":
            depth += 1
        elif pattern[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return [pattern]
    head, body, tail = pattern[:start], pattern[start + 1:end], pattern[end + 1:]
    out = []
    for piece in _split_top_level(body):
        out.extend(expand_braces(head + piece + tail))
    return out


def matches(globs, name):
    """Whether a file called ``name`` matches any of ``globs`` (case-insensitive).

    Case is ignored on every platform, not just Windows: a rule for ``*.jpg``
    that silently skipped ``PHOTO.JPG`` would be a bug report, not a feature.
    """
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, glob.lower()) for glob in globs)


# -- macros -----------------------------------------------------------
def quote(path):
    """``path`` quoted for the host shell, so %f never needs quotes in nshrc."""
    text = str(path)
    if os.name == "nt":
        return '"' + text.replace('"', '""') + '"'
    return shlex.quote(text)


def with_default_macro(command):
    """Append ``%f`` to a command that names no macro, so ``*.md = glow`` works."""
    return command if _ANY_MACRO.search(command) else command + " %f"


def expand(command, path, selection=()):
    """Substitute the macros in ``command`` for ``path``.

    ``%f`` the file, ``%F`` every selected file, ``%d`` its directory, ``%n`` its
    name without the suffix, ``%%`` a literal percent. Every substitution is
    quoted for the host shell. An unknown ``%x`` is left alone.
    """
    path = Path(path)
    files = [Path(p) for p in selection] or [path]

    def substitute(match):
        char = match.group(1)
        if char == "%":
            return "%"
        if char == "f":
            return quote(path)
        if char == "F":
            return " ".join(quote(p) for p in files)
        if char == "d":
            return quote(path.parent)
        if char == "n":
            return quote(path.stem)
        return match.group(0)

    return _MACRO.sub(substitute, command)


def commands_for(command, paths):
    """The shell command(s) that open ``paths``.

    ``%F`` takes the whole selection in one run; ``%f`` runs once per file, so a
    program that handles only one argument still works on a multi-selection.
    """
    paths = [Path(p) for p in paths]
    if not paths:
        return []
    if _SPREAD_MACRO.search(command):
        return [expand(command, paths[0], paths)]
    return [expand(command, path, paths) for path in paths]


def program_of(command):
    """The executable ``command`` starts with, for a PATH check. ``None`` if
    the line cannot be parsed (an unbalanced quote): then assume it is usable
    and let the shell report the real error."""
    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return None
    if not parts:
        return None
    return parts[0].strip("\"'") or None


# -- the rule table ---------------------------------------------------
class OpenRules:
    """The parsed ``[openers]`` / ``[open]`` tables."""

    def __init__(self, openers=(), rules=()):
        self.openers = dict(openers)  # name -> Opener, in nshrc order
        self.rules = list(rules)
        self._on_path = {}  # program -> bool, so shutil.which runs once each

    def __bool__(self):
        return bool(self.rules)

    def available(self, opener):
        """Whether ``opener``'s program exists. Built-ins always do."""
        if opener.is_builtin:
            return True
        program = program_of(opener.command)
        if program is None:
            return True
        if program not in self._on_path:
            self._on_path[program] = shutil.which(program) is not None
        return self._on_path[program]

    def match(self, path):
        """``(Rule, [usable openers])`` for the first rule that can open
        ``path``, else ``None``. A rule whose programs are all missing is
        skipped and the search continues on the next line."""
        name = Path(path).name
        for rule in self.rules:
            if not matches(rule.globs, name):
                continue
            usable = [o for o in rule.openers if self.available(o)]
            if usable:
                return rule, usable
        return None

    def candidates(self, path):
        """Openers for ``path``, best first; empty when no rule applies."""
        found = self.match(path)
        return list(found[1]) if found else []

    def claims_container(self, path):
        """Whether a rule specific enough to override "step into it" matches.

        A directory and an archive are browsed by default. Only a pattern that
        names the kind of file (``*.zip``) takes that over; a catch-all ``*``
        line, which exists to give ordinary files a fallback, never does.
        """
        found = self.match(path)
        return found is not None and found[0].pattern not in CATCH_ALL

    def named(self):
        """Every opener offerable by name: the user's first, built-ins last."""
        return (list(self.openers.values())
                + [Opener(name, "") for name in BUILTIN_OPENERS])


def parse(openers_items=(), open_items=()):
    """Build :class:`OpenRules` from raw INI items; return ``(rules, warnings)``."""
    openers, warnings = {}, []
    for raw_name, raw_command in openers_items:
        name, command = raw_name.strip(), raw_command.strip()
        if not name or not command:
            continue
        if name in BUILTIN_OPENERS:
            warnings.append(f"[openers] '{name}' is a built-in name; ignored")
            continue
        if not _NAME_LIKE.match(name):
            warnings.append(f"[openers] not a usable opener name: {name}")
            continue
        openers[name] = Opener(name, with_default_macro(command))

    rules = []
    for raw_pattern, value in open_items:
        pattern = raw_pattern.strip()
        if not pattern:
            continue
        chosen = []
        for token in _split_top_level(value):
            token = token.strip()
            if not token:
                continue
            if token in openers:
                chosen.append(openers[token])
            elif token in BUILTIN_OPENERS:
                chosen.append(Opener(token, ""))
            elif _NAME_LIKE.match(token) and not shutil.which(token):
                # One bare word that names no opener. If a program by that name
                # exists it is a command (``*.md = glow``); otherwise it is a
                # typo, and saying so beats silently never matching.
                warnings.append(
                    f"[open] {pattern}: '{token}' is not an opener name "
                    f"or a program on PATH")
            else:
                chosen.append(Opener("", with_default_macro(token)))
        if chosen:
            rules.append(Rule(pattern, tuple(expand_braces(pattern)),
                              tuple(chosen)))
    return OpenRules(openers, rules), warnings
