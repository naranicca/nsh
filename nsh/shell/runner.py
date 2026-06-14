"""Execute commands by wrapping the host's default shell.

Non-interactive commands are run with :func:`asyncio.create_subprocess_exec`
and their output is streamed line-by-line into the scrollback, so a long-running
command never freezes the UI.  Commands that need a real TTY (editors, pagers…)
are run with prompt_toolkit's ``run_in_terminal`` instead.
"""
import asyncio
import codecs
import os
import signal
import subprocess

from prompt_toolkit.application import run_in_terminal

# Commands that always own the terminal and cannot be driven through a pipe.
# (git is intentionally absent: it auto-disables its pager when stdout is not a
# TTY, so it streams correctly through the pipe.)
INTERACTIVE = {
    "vi", "vim", "nvim", "nano", "emacs", "less", "more", "man", "top",
    "htop", "ssh", "tmux", "screen", "watch", "fzf",
}

# REPLs that only need a TTY when launched bare: `python` is a prompt, but
# `python -V` / `-c …` / `script.py` just run and print, so those must stream.
REPL = {"python", "python3", "ipython", "node", "deno", "bun"}


def _oem_fallback():
    """Legacy code page name that Windows console tools emit, or ``None``.

    cmd built-ins and many native tools write their (localized) output in the
    OEM code page — e.g. cp949 on Korean Windows — not UTF-8. Returns a codec
    name like ``"cp949"`` to fall back to, or ``None`` off Windows / when it is
    already UTF-8 so the decoder just stays on plain UTF-8.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        cp = int(ctypes.windll.kernel32.GetOEMCP())
    except Exception:  # noqa: BLE001 - any failure -> no fallback
        return None
    if not cp or cp == 65001:
        return None
    name = f"cp{cp}"
    try:
        codecs.lookup(name)
    except LookupError:
        return None
    return name


def _utf8_safe_cut(buf: bytes) -> int:
    """Length of the largest prefix of ``buf`` not ending mid UTF-8 sequence."""
    n = len(buf)
    for back in range(1, min(4, n) + 1):
        b = buf[n - back]
        if b < 0x80:           # ASCII byte: clean boundary right after it
            return n
        if b >= 0xC0:          # lead byte of a multibyte sequence
            seq_len = 2 if b < 0xE0 else (3 if b < 0xF0 else 4)
            return n if back >= seq_len else n - back
        # else 0x80..0xBF continuation byte: keep scanning back
    return n


class _StreamDecoder:
    """Decode subprocess byte chunks, preferring UTF-8 with an OEM fallback.

    Most modern tools (git, python) emit UTF-8, so that is tried first. The
    moment a chunk isn't valid UTF-8 the stream switches to ``fallback`` (the
    OEM code page) for the rest — that is where cmd's localized error messages
    land. A multibyte character split across a 4 KiB chunk boundary is held
    back until the next chunk so it never decodes to a replacement char.
    """

    def __init__(self, fallback=None):
        self._fallback = fallback
        self._pending = b""
        self._fb = None  # incremental fallback decoder, created once we switch

    def decode(self, data: bytes = b"", final: bool = False) -> str:
        if self._fb is not None:
            return self._fb.decode(data, final)
        buf = self._pending + data
        cut = len(buf) if final else _utf8_safe_cut(buf)
        head, tail = buf[:cut], buf[cut:]
        try:
            text = head.decode("utf-8")
        except UnicodeDecodeError:
            if not self._fallback:
                self._pending = tail
                return head.decode("utf-8", "replace")
            # not UTF-8: switch the remainder of this stream to the OEM codec
            self._fb = codecs.getincrementaldecoder(self._fallback)("replace")
            self._pending = b""
            return self._fb.decode(buf, final)
        self._pending = tail
        return text


def detect_shell():
    """Return ``(executable, prefix_args)`` for the host's default shell."""
    if os.name == "nt":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        base = os.path.basename(comspec).lower()
        if "powershell" in base or "pwsh" in base:
            return comspec, ["-NoProfile", "-Command"]
        return comspec, ["/c"]
    shell = os.environ.get("SHELL", "/bin/sh")
    return shell, ["-c"]


class CommandRunner:
    def __init__(self, app):
        self.app = app
        self.shell, self.shell_args = detect_shell()
        self._fallback_encoding = _oem_fallback()  # OEM code page, for non-UTF-8 output
        self._proc = None  # the currently streaming subprocess, if any

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def interrupt(self) -> bool:
        """Kill the running command and its children. True if one was running."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False
        try:
            if os.name == "nt":
                # taskkill /T terminates the whole tree (cmd + the real command)
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        return True

    def is_interactive(self, command: str) -> bool:
        parts = command.strip().split()
        if not parts:
            return False
        cmd = os.path.basename(parts[0])
        if cmd in INTERACTIVE:
            return True
        if cmd in REPL:
            args = parts[1:]
            return not args or "-i" in args  # bare prompt (or forced) only
        return False

    @staticmethod
    def _child_env():
        """Environment that nudges tools to emit ANSI colour through the pipe.

        Their stdout is not a TTY, so colour is normally suppressed; nsh parses
        the escape codes itself, so we ask for it. ``setdefault`` lets the user
        override any of these from their own environment.
        """
        env = dict(os.environ)
        env.setdefault("CLICOLOR_FORCE", "1")
        env.setdefault("CLICOLOR", "1")
        env.setdefault("FORCE_COLOR", "1")
        # git only colours when told to; force color.ui for every git command
        param = "'color.ui=always'"
        existing = env.get("GIT_CONFIG_PARAMETERS")
        env["GIT_CONFIG_PARAMETERS"] = f"{existing} {param}" if existing else param
        return env

    async def run(self, command: str):
        """Stream a non-interactive command's stdout+stderr into scrollback."""
        argv = [self.shell, *self.shell_args, command]
        # Put the command in its own process group so Ctrl-C can kill the whole
        # tree (the shell plus the command it spawns), not just the shell.
        group = {"start_new_session": True} if os.name != "nt" else {}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.app.cwd),
                env=self._child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **group,
            )
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            self.app.shell.append(f"nsh: cannot run shell: {exc}", "class:shell.error")
            return
        self._proc = proc
        try:
            assert proc.stdout is not None
            # Read in chunks (not readline): a progress bar that only emits
            # carriage returns would otherwise stay buffered until the newline.
            decoder = _StreamDecoder(self._fallback_encoding)
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    self.app.shell.feed_output(text)
                    self.app.invalidate()
            tail = decoder.decode(b"", final=True)
            if tail:
                self.app.shell.feed_output(tail)
            self.app.shell.flush_output()
            self.app.invalidate()
            await proc.wait()
        finally:
            self._proc = None

    async def run_in_term(self, command: str):
        """Run an interactive command with the full-screen app suspended."""
        cwd = str(self.app.cwd)
        # Windows: pass the raw command string via shell=True so cmd.exe parses
        # the quotes itself. Building a [cmd, "/c", command] list instead lets
        # Python's list2cmdline escape the inner quotes (e.g. a quoted path) into
        # \" , which cmd then mis-parses. Unix has no such re-quoting: argv goes
        # straight to execve and ``$SHELL -c`` handles the quoting.
        if os.name == "nt":
            argv, kwargs = command, {"shell": True}
        else:
            argv, kwargs = [self.shell, *self.shell_args, command], {}

        def _run():
            try:
                subprocess.run(argv, cwd=cwd, **kwargs)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                print(f"nsh: {exc}")

        await run_in_terminal(_run)
