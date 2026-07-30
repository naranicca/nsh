"""Shell-safe quoting of a single path / word for the command line.

Tab-completion and the explorer's "drop the selected files into the prompt" both
splice real filenames into the shell command. A name with a space — or any other
shell metacharacter, e.g. the parentheses in ``file (1).txt`` — has to be quoted
or the shell mis-parses it and the command never runs. We wrap such a name in
double quotes (which keeps the existing "leave a directory's quote open so you
can keep drilling into it" behaviour working); on POSIX shells the few
characters that stay special *inside* double quotes (``$``, `` ` ``, ``"``,
``\\``) are backslash-escaped too. cmd.exe needs no inner escaping — a Windows
filename can't legally contain those, and double quotes already shield
``( ) & ^`` and spaces.
"""
import re

# Any of these in a word forces quoting. The list is deliberately inclusive:
# over-quoting a name that didn't strictly need it still runs fine, whereas
# under-quoting one that did breaks the command.
_SPECIAL = set(" \t\n\r\"'\\()[]{}<>|&;*?$`!#~^%=")
# On the native Windows shells the backslash is the *path separator*, not an
# escape character, so it must not force quoting — otherwise every directory
# completion (``myfolder\``) would come back wrapped in a double quote.
_SPECIAL_WIN = _SPECIAL - {"\\"}

_POSIX_ESCAPE = re.compile(r'([\\$`"])')
_POSIX_UNESCAPE = re.compile(r'\\([\\$`"])')


def needs_quoting(word, is_posix=True):
    """True when ``word`` contains a character the shell would treat specially."""
    special = _SPECIAL if is_posix else _SPECIAL_WIN
    return any(ch in special for ch in word)


def quote_body(word, is_posix):
    """Escape the *inside* of a double-quoted word (no surrounding quotes added)."""
    if is_posix:
        return _POSIX_ESCAPE.sub(r"\\\1", word)
    return word


def unquote_body(word):
    """Reverse :func:`quote_body` for a POSIX double-quoted word: only the four
    escaped sequences (``\\\\``, ``\\$``, ``\\```, ``\\"``) collapse — a stray
    ``\\b`` is left alone, matching how a real shell reads double quotes."""
    return _POSIX_UNESCAPE.sub(r"\1", word)


def quote_arg(word, is_posix=True):
    """Return ``word`` quoted so the shell sees it as one literal argument."""
    if not needs_quoting(word, is_posix):
        return word
    return '"' + quote_body(word, is_posix) + '"'
