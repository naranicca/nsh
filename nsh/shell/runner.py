"""Execute commands by wrapping the host's default shell.

Non-interactive commands are run with :func:`asyncio.create_subprocess_exec`
and their output is streamed line-by-line into the scrollback, so a long-running
command never freezes the UI.  Commands that need a real TTY (editors, pagers…)
are run with prompt_toolkit's ``run_in_terminal`` instead.
"""
import asyncio
import os
import subprocess

from prompt_toolkit.application import run_in_terminal

# Commands that own the terminal and cannot be driven through a pipe.
# (git is intentionally absent: it auto-disables its pager when stdout is not a
# TTY, so it streams correctly through the pipe.)
INTERACTIVE = {
    "vi", "vim", "nvim", "nano", "emacs", "less", "more", "man", "top",
    "htop", "ssh", "tmux", "screen", "python", "python3", "ipython", "node",
    "watch", "fzf",
}


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

    def is_interactive(self, command: str) -> bool:
        parts = command.strip().split()
        return bool(parts) and os.path.basename(parts[0]) in INTERACTIVE

    async def run(self, command: str):
        """Stream a non-interactive command's stdout+stderr into scrollback."""
        argv = [self.shell, *self.shell_args, command]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.app.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            self.app.shell.append(f"nsh: cannot run shell: {exc}", "class:shell.error")
            return
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            self.app.shell.append(line.decode("utf-8", "replace").rstrip("\r\n"))
            self.app.invalidate()
        await proc.wait()

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
