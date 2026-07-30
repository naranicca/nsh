"""Make the navigation / action keys work with the Korean IME switched on.

A console application can't see keystrokes before the IME composes them: with
the Korean IME on, pressing ``j`` to scroll arrives as the composed jamo ``ㅓ``
instead of ``j``, so the binding never fires and the user has to flip the IME to
English first. We can't bypass the IME, but we *can* reverse it — the two-beolsik
(두벌식) layout maps each physical key to a fixed jamo, so ``ㅓ`` came from ``j``.

:func:`add_hangul_aliases` walks a finished ``KeyBindings`` and, for every
single lowercase-letter binding, adds a parallel binding under the jamo that key
produces — so the same action runs whatever the IME state. This is applied only
to navigation/list key maps, never to text-entry buffers (where Korean input
must stay Korean).

Only lowercase letters are reversible: a Shift'd letter mostly composes to the
same jamo as its lowercase form (Shift+G and g both yield ``ㅎ``), so that
distinction is lost in the IME and keys like ``G`` / ``D`` still need English.
"""

# two-beolsik: the compatibility jamo (U+3130 block) the IME emits for a lone
# consonant / vowel -> the QWERTY letter sitting on that physical key.
JAMO_TO_QWERTY = {
    "ㅂ": "q", "ㅈ": "w", "ㄷ": "e", "ㄱ": "r", "ㅅ": "t",
    "ㅛ": "y", "ㅕ": "u", "ㅑ": "i", "ㅐ": "o", "ㅔ": "p",
    "ㅁ": "a", "ㄴ": "s", "ㅇ": "d", "ㄹ": "f", "ㅎ": "g",
    "ㅗ": "h", "ㅓ": "j", "ㅏ": "k", "ㅣ": "l",
    "ㅋ": "z", "ㅌ": "x", "ㅊ": "c", "ㅍ": "v", "ㅠ": "b",
    "ㅜ": "n", "ㅡ": "m",
}
QWERTY_TO_JAMO = {v: k for k, v in JAMO_TO_QWERTY.items()}


def add_hangul_aliases(kb):
    """For every single lowercase-letter binding in ``kb``, register the same
    handler under the Korean jamo that key produces, so it fires with the IME on.

    Safe to call on any navigation/list key map; a jamo that already has a
    binding (or has no two-beolsik letter) is left untouched."""
    bound = {b.keys for b in kb.bindings}
    for b in list(kb.bindings):
        if len(b.keys) != 1:
            continue
        key = b.keys[0]
        if not isinstance(key, str) or key not in QWERTY_TO_JAMO:
            continue
        jamo = (QWERTY_TO_JAMO[key],)
        if jamo in bound:
            continue
        bound.add(jamo)
        kb.add(jamo[0], filter=b.filter, eager=b.eager, is_global=b.is_global,
               save_before=b.save_before, record_in_macro=b.record_in_macro)(b.handler)
