"""Interactive remote file browser shared by FTP and SFTP connections."""
import asyncio
import posixpath
from pathlib import Path

from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window

from ..explorer.fileops import unique_target
from ..util.aio import run_in_thread
from ..util.widgets import WheelScrollControl, visible_slice
from ..util.width import cut_to_width
from . import backend as remote


class NetworkView:
    def __init__(self, app):
        self.app = app
        self.backend = None
        self.path = "/"
        self.entries = []
        self.cursor = 0
        self._top = 0
        self.selected = set()
        self.busy = False
        self.control = WheelScrollControl(
            lambda d: self.move(d * 3), text=self._text, focusable=True,
            show_cursor=False, key_bindings=self._keys(),
            get_cursor_position=lambda: Point(0, self.cursor - self._top),
        )
        self.window = Window(self.control, always_hide_cursor=True,
                             style="class:explorer.file")

    @property
    def connected(self):
        return self.backend is not None

    @property
    def location(self):
        return f"{self.backend.label}{self.path}" if self.backend else "network"

    def connect(self, protocol, target, password):
        if self.busy:
            return
        self.busy = True
        self.app.set_message(f"connecting to {target}…")

        async def do():
            try:
                backend, path = await run_in_thread(
                    remote.connect, protocol, target, password)
                old = self.backend
                self.backend, self.path = backend, path
                if old is not None:
                    await run_in_thread(old.close)
                await self._load()
                self.app.switch_mode("network")
                self.app.set_message(f"connected: {self.location}")
            except Exception as exc:  # noqa: BLE001 - surfaced in status bar
                self.app.set_message(f"connection failed: {exc}")
            finally:
                self.busy = False
                self.app.invalidate()

        asyncio.ensure_future(do())

    def disconnect(self):
        if self.busy:
            self.app.set_message("wait for the remote operation to finish")
            return
        backend, self.backend = self.backend, None
        self.entries = []
        self.selected.clear()
        if backend is not None:
            asyncio.ensure_future(run_in_thread(backend.close))
        self.app.switch_mode("explorer")
        self.app.set_message("remote disconnected")

    def close(self):
        """Close the transport during application shutdown without changing UI."""
        backend, self.backend = self.backend, None
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass

    async def _load(self, select=None):
        entries = await run_in_thread(self.backend.listdir, self.path)
        self.entries = entries
        self.selected.clear()
        self.cursor = 0
        if select:
            self.cursor = next((i for i, e in enumerate(entries)
                                if e.name == select), 0)
        self.app.invalidate()

    def refresh(self):
        if not self.connected or self.busy:
            return
        self.busy = True

        async def do():
            try:
                await self._load(self.current().name if self.current() else None)
            except Exception as exc:
                self.app.set_message(f"remote refresh failed: {exc}")
            finally:
                self.busy = False
                self.app.invalidate()
        asyncio.ensure_future(do())

    def current(self):
        return self.entries[self.cursor] if 0 <= self.cursor < len(self.entries) else None

    def targets(self):
        if self.selected:
            return [e for e in self.entries if e.path in self.selected]
        cur = self.current()
        return [cur] if cur else []

    def move(self, delta):
        if self.entries:
            self.cursor = max(0, min(len(self.entries) - 1, self.cursor + delta))
            self.app.invalidate()

    def toggle(self):
        cur = self.current()
        if cur:
            if cur.path in self.selected:
                self.selected.remove(cur.path)
            else:
                self.selected.add(cur.path)
            self.move(1)

    def open(self):
        cur = self.current()
        if not cur:
            return
        if not cur.is_dir:
            self.download()
            return
        self.path = posixpath.normpath(cur.path)
        self.busy = True

        async def do():
            try:
                await self._load()
            except Exception as exc:
                self.path = posixpath.dirname(self.path) or "/"
                self.app.set_message(f"cannot open directory: {exc}")
            finally:
                self.busy = False
        asyncio.ensure_future(do())

    def up(self):
        if self.path == "/" or self.busy:
            return
        old_path = self.path
        leaf = posixpath.basename(self.path)
        self.path = posixpath.dirname(self.path) or "/"
        self.busy = True

        async def do():
            try:
                await self._load(leaf)
            except Exception as exc:
                self.path = old_path
                self.app.set_message(f"cannot open parent: {exc}")
            finally:
                self.busy = False
        asyncio.ensure_future(do())

    def download(self):
        targets = self.targets()
        if not targets or self.busy:
            return
        backend, local_dir = self.backend, Path(self.app.cwd)
        self.busy = True
        self.app.set_message(f"downloading {len(targets)} item(s)…")

        async def do():
            done = 0
            try:
                for entry in targets:
                    target = unique_target(local_dir, entry.name)
                    if entry.is_dir:
                        await run_in_thread(backend.download_tree, entry.path, target)
                    else:
                        await run_in_thread(backend.download, entry.path, target)
                    done += 1
                self.app.explorer.refresh()
                self.app.set_message(f"downloaded {done} item(s) to {local_dir}")
            except Exception as exc:
                self.app.set_message(f"download failed after {done}: {exc}")
            finally:
                self.busy = False
                self.app.invalidate()
        asyncio.ensure_future(do())

    def upload(self):
        if self.busy:
            return
        paths = list(self.app.explorer._targets())
        if not paths:
            self.app.set_message("no local file selected")
            return
        backend, remote_dir = self.backend, self.path
        self.busy = True
        self.app.set_message(f"uploading {len(paths)} item(s)…")

        async def do():
            done = 0
            try:
                for path in paths:
                    target = await run_in_thread(
                        backend.unique_path, remote_dir, path.name)
                    if path.is_dir() and not path.is_symlink():
                        await run_in_thread(backend.upload_tree, path, target)
                    else:
                        await run_in_thread(backend.upload, path, target)
                    done += 1
                await self._load()
                self.app.set_message(f"uploaded {done} item(s)")
            except Exception as exc:
                self.app.set_message(f"upload failed after {done}: {exc}")
            finally:
                self.busy = False
                self.app.invalidate()
        asyncio.ensure_future(do())

    def new_dir(self):
        self.app.open_input_dialog("New remote folder", "", 0, self._make_dir)

    def _make_dir(self, name):
        name = name.strip()
        if not name or self.busy:
            return
        self._operation(self.backend.mkdir, posixpath.join(self.path, name),
                        success=f"created folder: {name}", select=name)

    def rename(self):
        cur = self.current()
        if cur:
            self.app.open_input_dialog("Rename remote item", cur.name, len(cur.name),
                                       lambda name: self._rename(cur, name))

    def _rename(self, entry, name):
        name = name.strip()
        if name and name != entry.name:
            self._operation(self.backend.rename, entry.path,
                            posixpath.join(self.path, name),
                            success=f"renamed to: {name}", select=name)

    def delete(self):
        targets = self.targets()
        if targets:
            self.app.confirm(
                f"Delete {len(targets)} remote item(s) permanently?",
                lambda ok: self._delete(targets) if ok else None)

    def _delete(self, targets):
        if self.busy:
            return
        self.busy = True

        async def do():
            done = 0
            try:
                for entry in targets:
                    fn = self.backend.remove_tree if entry.is_dir else self.backend.remove
                    await run_in_thread(fn, entry.path)
                    done += 1
                await self._load()
                self.app.set_message(f"deleted {done} remote item(s)")
            except Exception as exc:
                self.app.set_message(f"delete failed after {done}: {exc}")
            finally:
                self.busy = False
                self.app.invalidate()
        asyncio.ensure_future(do())

    def _operation(self, fn, *args, success, select=None):
        if self.busy:
            return
        self.busy = True

        async def do():
            try:
                await run_in_thread(fn, *args)
                await self._load(select)
                self.app.set_message(success)
            except Exception as exc:
                self.app.set_message(f"remote operation failed: {exc}")
            finally:
                self.busy = False
                self.app.invalidate()
        asyncio.ensure_future(do())

    def actions(self):
        self.app.open_menu("Remote actions", [
            ("Download / copy to local", self.download),
            ("Upload local selection", self.upload),
            ("New folder", self.new_dir),
            ("Rename", self.rename),
            ("Delete", self.delete),
            ("Refresh", self.refresh),
            ("Disconnect", self.disconnect),
        ])

    def _text(self):
        if self.busy and not self.entries:
            return [("class:preview.dim", "  working…")]
        if not self.entries:
            return [("class:preview.dim", "  (empty directory)")]
        start, end = visible_slice(
            self.window, len(self.entries), self.cursor, self._top, fallback=20)
        self._top = start
        width = (self.window.render_info.window_width
                 if self.window.render_info else 80)
        out = []
        for i in range(start, end):
            entry = self.entries[i]
            selected = entry.path in self.selected
            cursor = i == self.cursor
            marker = "●" if selected else ("▸" if cursor else " ")
            suffix = "/" if entry.is_dir else ""
            style = "class:explorer.dir" if entry.is_dir else "class:explorer.file"
            if cursor:
                style += " class:search-selected"
            out.append((style, f"{marker} {cut_to_width(entry.name + suffix, width - 2)}\n"))
        return out

    def _keys(self):
        kb = KeyBindings()
        kb.add("up")(lambda e: self.move(-1))
        kb.add("k")(lambda e: self.move(-1))
        kb.add("down")(lambda e: self.move(1))
        kb.add("j")(lambda e: self.move(1))
        kb.add("enter")(lambda e: self.open())
        kb.add("right")(lambda e: self.open())
        kb.add("l")(lambda e: self.open())
        kb.add("left")(lambda e: self.up())
        kb.add("h")(lambda e: self.up())
        kb.add("backspace")(lambda e: self.up())
        kb.add(" ")(lambda e: self.toggle())
        kb.add("tab")(lambda e: self.actions())
        kb.add("c")(lambda e: self.download())
        kb.add("p")(lambda e: self.upload())
        kb.add("n")(lambda e: self.new_dir())
        kb.add("i")(lambda e: self.rename())
        kb.add("D")(lambda e: self.delete())
        kb.add("r")(lambda e: self.refresh())
        kb.add("escape")(lambda e: self.disconnect())
        return kb
