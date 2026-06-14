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
        self._proc = None  # the currently streaming subprocess, if any

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
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
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
        argv = [self.shell, *self.shell_args, command]
        cwd = str(self.app.cwd)

        def _run():
            try:
                subprocess.run(argv, cwd=cwd)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                print(f"nsh: {exc}")

        await run_in_terminal(_run)
