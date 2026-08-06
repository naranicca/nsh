"""Execute commands by wrapping the host's default shell.

Non-interactive commands are run with :func:`asyncio.create_subprocess_exec`
and their output is streamed line-by-line into the scrollback, so a long-running
command never freezes the UI.  Commands that need a real TTY (editors, pagers…)
are run with prompt_toolkit's ``run_in_terminal`` instead.
"""
import asyncio
import codecs
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

from prompt_toolkit.application import run_in_terminal

from .prompt import prompt_fragments

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

# git subcommands that contact a remote and may prompt for credentials. We run
# them optimistically through the streaming pipe (so their output lands in the
# scrollback) with prompting disabled; if the credentials aren't cached git
# fails fast and the caller retries on a real terminal so the user can type a
# username / password.
GIT_NETWORK = {"push", "pull", "fetch", "clone"}

# Output fingerprints that mean a piped git/ssh command bailed out because it
# needed to prompt for credentials we suppressed — the cue to retry on a real
# terminal rather than report a plain failure.
_AUTH_SIGNATURES = (
    "terminal prompts disabled",     # GIT_TERMINAL_PROMPT=0 blocked a git prompt
    "could not read Username",
    "could not read Password",
    "Authentication failed",
    "Permission denied (publickey",  # ssh: no usable key / agent
    "Host key verification failed",  # ssh: unknown host needs an interactive yes
)

# git global options that take a value, so the real subcommand is the token
# after them (e.g. `git -C path push`, `git -c k=v pull`).
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


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


def _windows_parent_process():
    """Return the executable name of nsh's parent process on Windows."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            return ""
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return ""
            pid = os.getpid()
            while True:
                if entry.th32ProcessID == pid:
                    parent_pid = entry.th32ParentProcessID
                    break
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    return ""
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return ""
            while True:
                if entry.th32ProcessID == parent_pid:
                    return entry.szExeFile
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    return ""
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:  # noqa: BLE001 - shell detection must degrade gracefully
        return ""


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
    """Return ``(executable, prefix_args, is_posix)`` for the host's shell.

    ``is_posix`` means invoke the shell directly (``sh -c CMD``) instead of via
    cmd.exe. On Windows nsh honours a Git Bash / MSYS2 session (``MSYSTEM`` is
    set) so ``./script.sh`` and other POSIX commands run as expected; otherwise
    it uses cmd.exe / PowerShell.
    """
    if os.name == "nt":
        if os.environ.get("MSYSTEM"):  # launched from Git Bash / MSYS2
            bash = shutil.which("bash")
            if bash:
                return bash, ["-c"], True
        parent = _windows_parent_process()
        parent_base = os.path.basename(parent).lower()
        if parent and ("powershell" in parent_base or "pwsh" in parent_base):
            return parent, ["-Command"], False
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        base = os.path.basename(comspec).lower()
        if "powershell" in base or "pwsh" in base:
            # nsh command mode represents the user's shell, so load $PROFILE;
            # functions and aliases defined there must be available.
            return comspec, ["-Command"], False
        return comspec, ["/c"], False
    shell = os.environ.get("SHELL", "/bin/sh")
    return shell, ["-c"], True


def _wait_for_key():
    """Print a prompt and block until the user presses one key, then return.

    Used after a run_in_term command so its output stays on screen until the
    user is ready (nsh redraws over it the moment run_in_terminal returns).
    Reads a single keypress without waiting for Enter — ``msvcrt`` on Windows, a
    momentary raw ``termios`` mode elsewhere — and degrades to a no-op if stdin
    isn't a usable terminal (e.g. redirected), so it never blocks a script.
    """
    try:
        sys.stdout.write("press any key to continue . . . ")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        return
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.getwch()
        else:
            import termios
            import tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:  # noqa: BLE001 - no usable tty; don't block
        pass
    try:
        sys.stdout.write("\n\n")  # end the message line, then a blank line
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


class CommandRunner:
    def __init__(self, app, session=None):
        self.app = app
        # the shell session this runner streams output into (so a command keeps
        # writing to its own tab even after the user switches away). ``None`` for
        # the app-level runner that only ever drives run_in_term (editors, etc.).
        self.session = session
        self.shell, self.shell_args, self._is_posix = detect_shell()
        self._is_powershell = (
            os.name == "nt"
            and any(name in os.path.basename(self.shell).lower()
                    for name in ("powershell", "pwsh"))
        )
        self._fallback_encoding = _oem_fallback()  # OEM code page, for non-UTF-8 output
        self._proc = None  # the currently streaming subprocess, if any
        self._interrupted = False  # set when the user kills the command (Ctrl-C)
        self._started_at = None  # monotonic clock when the current command began
        # the last finished command's (duration, exit code), kept so the prompt
        # can show its run time tinted by success/failure until the next command
        self._last_duration = None
        self._last_rc = None
        self._tail = ""  # rolling buffer of recent output, for auth detection

    @property
    def _sink(self):
        return self.session if self.session is not None else self.app.shell

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def _prepare_command(self, command):
        """Expand native-Windows home paths without consuming wildcards.

        PowerShell 5 can leave ``~/dir/*.jpg`` unresolved in commands launched
        through ``-Command``; cmd also has no tilde expansion. Shell detection
        can conservatively fall back from PowerShell to cmd when nsh is started
        through a launcher, so apply this to every non-POSIX Windows command.
        Only a tilde at the start of a shell word (optionally just inside a
        quote) is touched; POSIX keeps its native tilde expansion semantics.
        """
        if self._is_posix:
            return command
        home = os.path.expanduser("~")
        out = []
        for i, char in enumerate(command):
            if char == "~" and i + 1 < len(command) and command[i + 1] in "/\\":
                prev = command[i - 1] if i else ""
                quoted_start = (prev in "\"'" and
                                (i == 1 or command[i - 2].isspace()
                                 or command[i - 2] in "=,(;|&"))
                if i == 0 or prev.isspace() or prev in "=,(;|&" or quoted_start:
                    out.append(home)
                    continue
            out.append(char)
        return "".join(out)

    def elapsed(self):
        """Seconds the current streaming command has been running, or ``None``."""
        if self._started_at is None or not self.is_running():
            return None
        return time.monotonic() - self._started_at

    def last_result(self):
        """``(duration_seconds, exit_code)`` of the last finished command, or None."""
        if self._last_duration is None:
            return None
        return self._last_duration, self._last_rc

    def reset_result(self):
        """Drop the finished-command status (a new command is being entered)."""
        self._last_duration = None
        self._last_rc = None

    def adopt_term_result(self, rc):
        """After a piped command was retried on the terminal, make the prompt's
        status badge reflect the terminal outcome instead of the failed pipe."""
        self._last_rc = rc

    def auth_prompt_needed(self) -> bool:
        """True when the last piped command failed because git/ssh wanted to
        prompt for credentials we suppressed — the cue to retry on a real
        terminal. A plain non-zero exit (e.g. a rejected push) is *not* this."""
        if self._last_rc in (0, None):
            return False
        return any(sig in self._tail for sig in _AUTH_SIGNATURES)

    def assignment_names(self, command: str):
        """Variable names of a *pure* assignment line, or ``None``.

        Detects a line that is only assignments (``a=10``, ``export PATH=… b=2``)
        with no command word after them. A line such as ``a=10 some-cmd`` returns
        ``None`` — there the assignment is scoped to that one command, which the
        shell already handles. Compound lines (``a=10; echo $a``) also return
        ``None``: they run as-is in a single shell invocation and may have side
        effects we must not re-run.
        """
        if not self._is_posix:
            return None  # cmd/PowerShell use different assignment syntax
        line = command.strip()
        if not line or any(c in line for c in ";|&\n`"):
            return None
        try:
            tokens = shlex.split(line)
        except ValueError:
            return None
        if tokens and tokens[0] == "export":
            tokens = tokens[1:]
        if not tokens:
            return None
        names = []
        for tok in tokens:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", tok)
            if not m:
                return None  # a non-assignment token -> there's a command word
            names.append(m.group(1))
        return names

    def sourced_file(self, command: str):
        """If ``command`` is a plain ``source FILE`` / ``. FILE`` line, return its
        FILE argument, else ``None``.

        Each command runs in its own subprocess, so a ``source``d script's
        functions/aliases/variables don't survive into the next command. nsh
        notes the file here and re-sources it ahead of every later command (see
        :meth:`sourced_prefix`). Compound lines (``; | & `` …) are skipped — they
        may have side effects we must not re-run. POSIX shells only.
        """
        if not self._is_posix:
            return None
        line = command.strip()
        if not line or any(c in line for c in ";|&\n`"):
            return None
        try:
            tokens = shlex.split(line)
        except ValueError:
            return None
        if len(tokens) >= 2 and tokens[0] in (".", "source"):
            return tokens[1]
        return None

    def sourced_prefix(self) -> str:
        """A shell snippet that silently re-sources every file the user has
        ``source``d, to be prepended to a command so its functions/aliases/vars
        are in scope. Empty when nothing's been sourced (or on cmd/PowerShell)."""
        files = getattr(self.app, "sourced_files", None)
        if not files or not self._is_posix:
            return ""
        dots = " ".join(f". {shlex.quote(f)} ;" for f in files)
        # group + redirect so the re-source itself prints nothing and a missing
        # file can't abort the real command (which follows the ';')
        return "{ " + dots + " } >/dev/null 2>&1 ; "

    async def eval_assignment(self, command: str):
        """Evaluate a bare assignment line once and store it in nsh's own env.

        ``a=10`` typed on its own would be lost — each command runs in a fresh
        subprocess. Instead we let the shell evaluate the line a single time
        (so quoting, ``$other`` expansion and ``$(…)`` all work), read the
        resulting values back NUL-delimited, and store them in ``app.shell_vars``
        (not ``os.environ``, which upper-cases keys on Windows). Every later
        subprocess inherits them via :meth:`_child_env`, so the variables are
        simply *there* — no per-command prefix to grow or re-run. They live for
        the nsh session (a child process cannot write its parent shell's env).
        """
        names = self.assignment_names(command)
        if not names:
            return
        line = command.strip()
        # after the assignment, print each assigned value + a NUL terminator;
        # NUL can't appear in a value, so this round-trips any spaces/newlines.
        dump = "printf '%s\\0' " + " ".join(f'"${n}"' for n in names)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.shell, *self.shell_args, f"{line}; {dump}",
                cwd=str(self.app.cwd), env=self._child_env(),
                stdin=asyncio.subprocess.DEVNULL,  # don't share the terminal stdin
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
        except OSError:
            return
        store = self.app.shell_vars
        values = out.split(b"\0")
        for name, value in zip(names, values):
            store[name] = value.decode("utf-8", "replace")

    def interrupt(self) -> bool:
        """Kill the running command and its children. True if one was running."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False
        self._interrupted = True  # so run() tints the badge as a failure
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

    @staticmethod
    def _git_subcommand(parts):
        """The git subcommand (push/commit/…), skipping global options."""
        args = iter(parts[1:])
        for tok in args:
            if tok in _GIT_VALUE_OPTS:
                next(args, None)  # consume the option's value
                continue
            if tok.startswith("-"):
                continue
            return tok
        return None

    def is_git_network(self, command: str) -> bool:
        """True for git subcommands that contact a remote (may prompt for auth)."""
        parts = command.strip().split()
        if not parts or os.path.basename(parts[0]) != "git":
            return False
        return self._git_subcommand(parts) in GIT_NETWORK

    def git_summary(self, command: str, rc):
        """A one-line scrollback note for a network git command.

        Its real output went to the suspended terminal (run_in_term), so leave a
        trace of how it ended. Returns ``(text, style)``.
        """
        parts = command.strip().split()
        sub = self._git_subcommand(parts) if parts else None
        label = f"git {sub}" if sub else "git"
        if rc == 0:
            return f"{label}: done", "class:shell.prompt"
        if rc is None:
            return f"{label}: failed", "class:shell.error"
        return f"{label}: exit code {rc}", "class:shell.error"

    def is_interactive(self, command: str) -> bool:
        parts = command.strip().split()
        if not parts:
            return False
        cmd = os.path.basename(parts[0])
        if cmd in INTERACTIVE:
            return True
        if cmd == "sudo":  # prompts for a password on the terminal
            return True
        if self.is_git_network(command):
            return True
        if cmd in REPL:
            args = parts[1:]
            return not args or "-i" in args  # bare prompt (or forced) only
        return False

    def _child_env(self, allow_prompt=True):
        """Environment for a child command: nsh's own env, the user's shell
        variables, and colour hints.

        Variables set in the shell (``a=10``) live in ``app.shell_vars`` rather
        than ``os.environ`` — on Windows ``os.environ`` upper-cases its keys, so
        a lower-case ``a`` would never reach a case-sensitive (Git Bash) shell.
        We merge them here and pass the result explicitly to every subprocess.

        The colour hints make tools emit ANSI even though their stdout is not a
        TTY (nsh parses the escape codes itself); ``setdefault`` lets the user
        override any of them from their own environment.

        ``allow_prompt=False`` forbids git/ssh from prompting for credentials, so
        a piped network command fails fast (and we retry it on a real terminal)
        instead of hanging on a prompt the user can't see.
        """
        env = dict(os.environ)
        env.update(getattr(self.app, "shell_vars", {}))
        env.setdefault("CLICOLOR_FORCE", "1")
        env.setdefault("CLICOLOR", "1")
        env.setdefault("FORCE_COLOR", "1")
        # Stream output live. We capture the child's stdout through a pipe, not a
        # TTY, so CPython (and most stdio programs) switch to block buffering and
        # hold ~KBs of output until the buffer fills or the process exits — a
        # `print` loop would then appear all at once at the end. Asking Python to
        # stay unbuffered makes its prints land as they happen; setdefault lets
        # the user override it.
        env.setdefault("PYTHONUNBUFFERED", "1")
        # git only colours when told to; force color.ui for every git command
        param = "'color.ui=always'"
        existing = env.get("GIT_CONFIG_PARAMETERS")
        env["GIT_CONFIG_PARAMETERS"] = f"{existing} {param}" if existing else param
        if not allow_prompt:
            env["GIT_TERMINAL_PROMPT"] = "0"  # no built-in username/password prompt
            # ssh otherwise tries the shared console for a passphrase / host-key
            # confirmation, which would corrupt the full-screen UI; make it fail
            # fast instead. Respect a GIT_SSH_COMMAND the user already set.
            env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        return env

    async def run(self, command: str, allow_prompt=True):
        """Stream a non-interactive command's stdout+stderr into scrollback.

        ``allow_prompt=False`` disables git/ssh credential prompts so a remote
        git command fails fast when its credentials aren't cached; the caller
        checks :meth:`auth_prompt_needed` and retries it on a real terminal.
        """
        self._interrupted = False
        self._started_at = time.monotonic()
        self._tail = ""
        # bring any `source`d scripts' functions/aliases/vars into scope first
        command = self.sourced_prefix() + self._prepare_command(command)
        kwargs = dict(
            cwd=str(self.app.cwd),
            env=self._child_env(allow_prompt=allow_prompt),
            # Detach the command's stdin from the terminal. Inheriting it lets the
            # child fiddle with the terminal (cursor-key mode, raw mode), which
            # corrupted nsh's own input afterwards — arrow keys (escape sequences)
            # would stop registering while plain characters still worked. A child
            # that needs real input goes through run_in_term instead.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            if self._is_posix or self._is_powershell:
                # Exec the shell directly (sh/bash -c CMD). On real POSIX, start a
                # new session so Ctrl-C can signal the whole process group; Git
                # Bash on Windows is killed via taskkill instead, so skip it there.
                extra = {"start_new_session": True} if os.name == "posix" else {}
                proc = await asyncio.create_subprocess_exec(
                    self.shell, *self.shell_args, command, **kwargs, **extra,
                )
            else:
                # cmd.exe: pass the raw line through the platform shell.
                proc = await asyncio.create_subprocess_shell(command, **kwargs)
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            self._sink.append(f"nsh: cannot run shell: {exc}", "class:shell.error")
            self._last_duration, self._last_rc = 0.0, 127  # show as a failure
            self._started_at = None
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
                    self._sink.feed_output(text)
                    self._tail = (self._tail + text)[-2048:]
                    self.app.invalidate()
            tail = decoder.decode(b"", final=True)
            if tail:
                self._sink.feed_output(tail)
                self._tail = (self._tail + tail)[-2048:]
            self._sink.flush_output()
            await proc.wait()
            # record the run time + outcome for the prompt's status indicator
            if self._started_at is not None:
                self._last_duration = time.monotonic() - self._started_at
            rc = proc.returncode
            self._last_rc = -1 if self._interrupted else rc
            # (a non-zero exit is shown by the prompt's red time badge, not a line)
            self.app.invalidate()
        finally:
            self._proc = None
            self._started_at = None

    async def run_in_term(self, command: str):
        """Run an interactive command with the full-screen app suspended.

        Returns the command's exit code (``None`` if it could not be launched).
        """
        self._started_at = time.monotonic()
        cwd = str(self.app.cwd)
        env = self._child_env()  # carry the user's shell variables in too
        # Invoke POSIX shells and PowerShell explicitly.  Going through
        # shell=True may route the command via cmd.exe instead of the detected
        # PowerShell, which would also lose the user's profile functions.
        if self._is_posix or self._is_powershell:
            sourced = self.sourced_prefix() + self._prepare_command(command)
            argv, kwargs = [self.shell, *self.shell_args, sourced], {}
        else:
            argv, kwargs = command, {"shell": True}

        result = {"rc": None}

        def _run():
            # Echo the prompt + command first: a command run out here (git asking
            # for a username, an editor…) otherwise shows only its output, with no
            # sign of what produced it. Use the shell prompt format (cwd + git
            # branch + `$`) without the previous command's run-time/exit badge.
            #
            # Write it straight to stdout (plain text) and flush, so it lands
            # above the child's output. print_formatted_text can't be used here:
            # while the full-screen app is suspended inside run_in_terminal its
            # output is swallowed (verified), so the echo never showed and the
            # prompt only reappeared — out of order — when nsh redrew afterwards.
            # A raw stdout write is the only thing that reliably prints first.
            try:
                prompt = "".join(text for _, text in prompt_fragments(self.app))
                sys.stdout.write(prompt + command + "\n")
                sys.stdout.flush()
            except Exception:  # noqa: BLE001 - never let the echo block the command
                pass
            try:
                result["rc"] = subprocess.run(argv, cwd=cwd, env=env, **kwargs).returncode
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                print(f"nsh: {exc}")
            # hold the command's output on screen until the user presses a key —
            # otherwise nsh redraws over it the instant run_in_terminal returns
            _wait_for_key()

        # Run the child in a thread (in_executor=True), like prompt_toolkit's own
        # editor launcher (Buffer.open_in_editor). With the default in_executor=
        # False the blocking subprocess.run would execute on the event-loop
        # thread and freeze the loop; a full-screen editor (vi) started that way
        # never really takes the terminal — it sits there until Ctrl-C. Off the
        # loop thread it gets the tty cleanly (input is detached and the terminal
        # is in cooked mode around it either way).
        try:
            await run_in_terminal(_run, in_executor=True)
        finally:
            # Interactive/forced commands bypass run(), so record their result
            # here as well. This feeds the same elapsed-time badge shown before
            # the next prompt for ordinary streamed commands.
            self._last_duration = time.monotonic() - self._started_at
            self._last_rc = result["rc"]
            self._started_at = None
            self.app.invalidate()
        return result["rc"]
