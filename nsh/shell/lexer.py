"""Lightweight, dependency-free syntax highlighting for the command line."""
import re

from prompt_toolkit.lexers import Lexer

# A token is a quoted string, a run of non-space chars, or a run of spaces.
_TOKEN_RE = re.compile(r"\"[^\"]*\"?|'[^']*'?|\S+|\s+")


def lex_line(text):
    """Tokenize one command line into ``[(style, text), ...]`` fragments.

    Shared by the live input lexer and the scrollback echo so both highlight
    identically.
    """
    tokens = []
    first = True
    for match in _TOKEN_RE.finditer(text):
        tok = match.group(0)
        if tok.isspace():
            tokens.append(("", tok))
            continue
        if tok[0] in ("\"", "'"):
            style = "class:shell.string"
        elif tok.startswith("-"):
            style = "class:shell.option"
        elif first:
            style = "class:shell.command"
        elif any(s in tok for s in ("/", "\\")) or tok[0] in ("~", "."):
            style = "class:shell.path"
        else:
            style = "class:shell.output"
        tokens.append((style, tok))
        first = False
    return tokens


class ShellLexer(Lexer):
    def lex_document(self, document):
        lines = document.lines

        def get_line(lineno):
            return lex_line(lines[lineno])

        return get_line
