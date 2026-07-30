"""Display-width helpers built on ``wcwidth``.

Terminal layout must be computed from the *rendered cell width* of a string,
not ``len()`` — otherwise CJK (한/中/日) and other wide characters break the
columns.  Every padding/truncation decision in the UI goes through here.
"""
from wcwidth import wcswidth, wcwidth


def char_width(ch: str) -> int:
    """Cell width of a single character (control/zero-width chars count as 0)."""
    w = wcwidth(ch)
    return 0 if w is None or w < 0 else w


def text_width(text: str) -> int:
    """Total rendered cell width of ``text``."""
    w = wcswidth(text)
    if w >= 0:
        return w
    # wcswidth returns -1 if the string contains a control char; fall back to a
    # per-character sum that simply ignores those.
    return sum(char_width(c) for c in text)


def cut_to_width(text: str, width: int) -> str:
    """Return the longest prefix of ``text`` whose width is <= ``width``.

    A wide character is never split in half: if it would overflow it is dropped.
    """
    if width <= 0:
        return ""
    out = []
    cur = 0
    for ch in text:
        cw = char_width(ch)
        if cur + cw > width:
            break
        out.append(ch)
        cur += cw
    return "".join(out)


def pad_to_width(text, width, align="left", fill=" ", ellipsis="…"):
    """Pad/truncate ``text`` to exactly ``width`` cells (wide-char aware)."""
    if width <= 0:
        return ""
    tw = text_width(text)
    if tw > width:
        ew = text_width(ellipsis)
        text = cut_to_width(text, max(0, width - ew)) + ellipsis
        tw = text_width(text)
    pad = max(0, width - tw)
    if align == "right":
        return fill * pad + text
    if align == "center":
        left = pad // 2
        return fill * left + text + fill * (pad - left)
    return text + fill * pad
