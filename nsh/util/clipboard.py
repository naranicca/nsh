"""Minimal system-clipboard access with no third-party dependency.

Copy/paste go through the OS: the Win32 API (via ctypes) on Windows so Unicode
round-trips cleanly — the ``clip`` / ``Get-Clipboard`` shell tools mangle
non-ASCII through the console code page — and the usual command-line helpers
elsewhere (``pbcopy``/``pbpaste`` on macOS, ``wl-clipboard`` / ``xclip`` /
``xsel`` on Linux). Everything degrades to a no-op when no tool is available, so
the caller simply reports "clipboard unavailable" instead of erroring.
"""
import os
import shutil
import subprocess
import sys


def copy_text(text: str) -> bool:
    """Put ``text`` on the system clipboard. Returns True on success."""
    if os.name == "nt":
        return _win_copy(text)
    cmd = _unix_copy_cmd()
    if not cmd:
        return False
    try:
        subprocess.run(cmd, input=text.encode("utf-8"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def paste_text():
    """Return the system clipboard's text (newlines normalised to ``\\n``), or
    None when the clipboard is empty or no tool is available."""
    if os.name == "nt":
        text = _win_paste()
    else:
        cmd = _unix_paste_cmd()
        if not cmd:
            return None
        try:
            out = subprocess.run(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, check=True).stdout
            text = out.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return None
    if not text:
        return None
    return text.replace("\r\n", "\n").replace("\r", "\n")


# -- POSIX helpers (pick whichever tool is installed) -------------------------
# (session env-var, command). The env var gates the obvious choice — wl-* under
# Wayland, x* under X11 — so an installed-but-wrong-session tool (e.g. wl-copy
# on an X11 login) isn't picked over one that works. _pick still falls back to
# any installed tool if none of the session vars are set.
_COPY_TOOLS = [
    ("WAYLAND_DISPLAY", ["wl-copy"]),
    ("DISPLAY", ["xclip", "-selection", "clipboard"]),
    ("DISPLAY", ["xsel", "--clipboard", "--input"]),
]
_PASTE_TOOLS = [
    ("WAYLAND_DISPLAY", ["wl-paste", "--no-newline"]),
    ("DISPLAY", ["xclip", "-selection", "clipboard", "-o"]),
    ("DISPLAY", ["xsel", "--clipboard", "--output"]),
]


def _pick(tools):
    # first pass: a tool whose session var is actually set; second pass: any
    # installed tool, regardless of the (possibly unset) session vars
    for require_env in (True, False):
        for env, cmd in tools:
            if (os.environ.get(env) if require_env else True) and shutil.which(cmd[0]):
                return cmd
    return None


def _unix_copy_cmd():
    return ["pbcopy"] if sys.platform == "darwin" else _pick(_COPY_TOOLS)


def _unix_paste_cmd():
    return ["pbpaste"] if sys.platform == "darwin" else _pick(_PASTE_TOOLS)


# -- Windows (Win32 clipboard via ctypes, CF_UNICODETEXT) --------------------
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _win_api():
    """user32 / kernel32 with handle-safe (64-bit) arg/return types set."""
    import ctypes
    from ctypes import c_size_t, c_void_p, wintypes

    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    k32.GlobalAlloc.restype = c_void_p
    k32.GlobalAlloc.argtypes = [wintypes.UINT, c_size_t]
    k32.GlobalLock.restype = c_void_p
    k32.GlobalLock.argtypes = [c_void_p]
    k32.GlobalUnlock.argtypes = [c_void_p]
    k32.GlobalFree.argtypes = [c_void_p]
    u32.OpenClipboard.argtypes = [c_void_p]
    u32.SetClipboardData.restype = c_void_p
    u32.SetClipboardData.argtypes = [wintypes.UINT, c_void_p]
    u32.GetClipboardData.restype = c_void_p
    u32.GetClipboardData.argtypes = [wintypes.UINT]
    return ctypes, u32, k32


def _win_copy(text: str) -> bool:
    try:
        ctypes, u32, k32 = _win_api()
    except Exception:  # noqa: BLE001 - not really Windows / API missing
        return False
    data = text.replace("\r\n", "\n").replace("\n", "\r\n")  # clipboard wants CRLF
    buf = ctypes.create_unicode_buffer(data)  # NUL-terminated wide string
    size = ctypes.sizeof(buf)
    if not u32.OpenClipboard(None):
        return False
    try:
        u32.EmptyClipboard()
        handle = k32.GlobalAlloc(_GMEM_MOVEABLE, size)
        if not handle:
            return False
        ptr = k32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, buf, size)
        k32.GlobalUnlock(handle)
        if not u32.SetClipboardData(_CF_UNICODETEXT, handle):
            k32.GlobalFree(handle)  # ownership wasn't taken; release it
            return False
        return True
    finally:
        u32.CloseClipboard()


def _win_paste():
    try:
        ctypes, u32, k32 = _win_api()
    except Exception:  # noqa: BLE001
        return None
    if not u32.OpenClipboard(None):
        return None
    try:
        handle = u32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return None
        ptr = k32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            k32.GlobalUnlock(handle)
    finally:
        u32.CloseClipboard()
