"""Interactive remote file browser shared by FTP and SFTP connections."""
import asyncio
import posixpath
import threading
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.margins import Margin
from prompt_toolkit.mouse_events import MouseModifier

from .. import config
from ..explorer.model import natural_key
from ..explorer.fileops import unique_target
from ..util import state
from ..util.aio import run_in_thread
from ..util.paths import human_size
from ..util.widgets import WheelScrollControl, visible_slice
from ..util.width import pad_to_width
from . import backend as remote


SIZE_COL = 8
REMOTE_PREVIEW_BYTES = 256 * 1024
BINARY_PREVIEW_EXTENSIONS = {
    ".7z", ".avi", ".bin", ".bmp", ".bz2", ".dll", ".doc", ".docx",
    ".exe", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".mkv", ".mov",
    ".mp3", ".mp4", ".pdf", ".png", ".ppt", ".pptx", ".rar", ".so",
    ".tar", ".tif", ".tiff", ".wav", ".webp", ".xls", ".xlsx", ".zip",
}


class _NetworkPreviewScrollbar(Margin):
    """Scrollbar for the manually windowed remote text preview."""

    def __init__(self, view):
        self.view = view

    def get_width(self, get_ui_content):
        return 1 if self.view._preview_total > self.view._preview_view else 0

    def create_margin(self, window_render_info, width, height):
        total = self.view._preview_total
        visible = self.view._preview_view
        if height <= 0 or total <= visible:
            return []
        maximum = total - visible
        scroll = max(0, min(self.view._preview_scroll, maximum))
        thumb = max(1, min(height, height * visible // total))
        top = ((height - thumb) * scroll // maximum) if maximum else 0
        focused = not self.view.app.network_local_focused()
        thumb_style = ("class:scrollbar.button" if focused
                       else "class:scrollbar.button.inactive")
        fragments = []
        for row in range(height):
            style = (thumb_style if top <= row < top + thumb
                     else "class:scrollbar.background")
            fragments.append((style, " "))
            if row < height - 1:
                fragments.append(("", "\n"))
        return fragments


class NetworkView:
    def __init__(self, app):
        self.app = app
        self.backend = None
        # The local explorer pane shown beside the remote one; it is the
        # source/destination for all transfers. It starts as tdhe pane the
        # connection was opened from and follows the current tab from there
        # (see NshApp._sync_network_local_pane), so every tab browses its own
        # local directory beside the one shared remote pane. Running transfers
        # capture it up front, so a tab switch never redirects them.
        self.local_view = None
        self.path = "/"
        self.entries = []
        self.expanded = set()
        self._children = {}
        self.cursor = 0
        self._top = 0
        self.selected = set()
        self._preview_entry = None
        self._preview_data = None
        self._preview_lines = None
        self._preview_error = None
        self._preview_loading = False
        self._preview_scroll = 0
        self._preview_total = 0
        self._preview_view = 0
        self._preview_token = None
        self._binary_cancel = None
        self._temp_dir = None
        settings = getattr(app, "settings", {})
        self.sort = settings.get("sort", "name")
        if self.sort not in ("name", "size", "date", "type"):
            self.sort = "name"
        self.reverse = (settings.get("sort_reverse", "false").lower()
                        in ("true", "1", "yes", "on"))
        self.busy = False
        self.indexing = False
        self._index_token = object()
        self._backend_lock = threading.RLock()
        self.control = WheelScrollControl(
            lambda d: self.move(d * 3), on_click=self._on_mouse,
            text=self._text, focusable=True,
            show_cursor=False, key_bindings=self._keys(),
            get_cursor_position=self._cursor_position,
        )
        self.window = Window(
            self.control, always_hide_cursor=True,
            style="class:explorer.file",
            right_margins=[_NetworkPreviewScrollbar(self)],
        )

    @property
    def connected(self):
        return self.backend is not None

    def _cursor_position(self):
        """Keep prompt_toolkit's cursor inside the currently rendered content."""
        if self._preview_entry is not None:
            return Point(0, 0)
        return Point(0, max(0, self.cursor - self._top))

    @property
    def location(self):
        return f"{self.backend.label}{self.path}" if self.backend else "network"

    def connect(self, protocol, target, password, jump=None,
                accept_host_key=None):
        if self.busy:
            return
        self.busy = True
        self.app.set_message(f"connecting to {target}…")

        async def do():
            try:
                backend, path = await run_in_thread(
                    remote.connect, protocol, target, password, jump,
                    accept_host_key)
                old = self.backend
                # Adopt the pane that is current *now*, not the one the
                # connection was started from: the handshake takes seconds, and
                # a tab switch during it could not re-point us (connected() was
                # still False, so _sync_network_local_pane did nothing). That
                # left the displayed local half on one tab's pane while the
                # refreshes all followed another's.
                self.local_view = self.app.explorer
                self.backend, self.path = backend, path
                if old is not None:
                    await run_in_thread(old.close)
                await self._load()
                # The Network layout becomes interactive as soon as it is
                # shown. Clear the handshake flag first so an immediate `:`
                # keypress can enter the remote shell on the same event-loop turn.
                self.busy = False
                self.app.switch_mode("network")
                if protocol == "sftp":
                    state.set("network_sftp_target", target)
                self.app.set_message(f"connected: {self.location}")
            except remote.HostKeyRequired as exc:
                label = (f"Trust {exc.hostname} {exc.key_type} host key? "
                         f"{exc.fingerprint}")
                accepted_key = (exc.hostname, exc.fingerprint)
                self.app.confirm(
                    label,
                    lambda ok, key=accepted_key: self.connect(
                        protocol, target, password, jump,
                        key) if ok else None,
                )
            except remote.AuthenticationFailed:
                self.app.set_message("authentication failed; enter password again")
                self.app._network_password(protocol, target, jump)
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
        if self.backend is None:
            return
        self.app.confirm(
            f"Disconnect from {self.backend.label}?",
            lambda ok: self._disconnect() if ok else None,
        )

    def _disconnect(self):
        self.cancel_indexing()
        backend, self.backend = self.backend, None
        local_view, self.local_view = self.local_view, None
        self.entries = []
        self.expanded.clear()
        self._children.clear()
        self.selected.clear()
        self.close_preview()
        self._cleanup_temp()
        if backend is not None:
            asyncio.ensure_future(run_in_thread(
                self._backend_call, backend.close))
        # every tab shared the local|remote split, so they all go back to their
        # own explorer layout (the right half returning to the preview)
        self.app.leave_network_views()
        self.app.switch_mode("explorer")
        if local_view is not None:
            try:
                self.app.application.layout.focus(local_view.control)
            except Exception:
                pass
        self.app.set_message("remote disconnected")

    def cancel(self):
        """Clear remote selection without risking the active connection."""
        if self._preview_entry is not None:
            self.close_preview()
            return
        self.selected.clear()
        self.app.invalidate()

    def close(self):
        """Close the transport during application shutdown without changing UI."""
        self.cancel_indexing()
        if self._binary_cancel is not None:
            self._binary_cancel.set()
        backend, self.backend = self.backend, None
        self.local_view = None
        if backend is not None:
            try:
                self._backend_call(backend.close)
            except Exception:
                pass
        self._cleanup_temp()

    def _cleanup_temp(self):
        """Delete only this NetworkView instance's private preview directory."""
        temp, self._temp_dir = self._temp_dir, None
        if temp is not None:
            try:
                temp.cleanup()
            except OSError:
                pass

    async def _load(self, select=None):
        entries = await run_in_thread(
            self._backend_call, self.backend.listdir, self.path)
        self.expanded.clear()
        self._children = {self.path: entries}
        self.entries = self._flatten(self.path)
        self.selected.clear()
        self.cursor = self.first_index()
        if select:
            self.cursor = next((i for i, e in enumerate(self.entries)
                                if e.name == select), self.cursor)
        self.app.invalidate()

    def _backend_call(self, function, *args):
        """Serialize access to clients that do not support concurrent calls."""
        with self._backend_lock:
            return function(*args)

    def _flatten(self, directory, depth=0):
        out = []
        if depth == 0 and directory != "/":
            out.append(remote.RemoteEntry(
                "..", posixpath.dirname(directory) or "/", True,
                depth=0, is_parent=True))
        for entry in self._sorted(self._children.get(directory, [])):
            row = replace(entry, depth=depth)
            out.append(row)
            if row.is_dir and row.path in self.expanded:
                out.extend(self._flatten(row.path, depth + 1))
        return out

    def _sorted(self, entries):
        keys = {
            "name": lambda entry: natural_key(entry.name),
            "size": lambda entry: (entry.size, natural_key(entry.name)),
            "date": lambda entry: (entry.mtime, natural_key(entry.name)),
            "type": lambda entry: (
                posixpath.splitext(entry.name)[1].lower(),
                natural_key(entry.name)),
        }
        rows = sorted(entries, key=keys[self.sort], reverse=self.reverse)
        return sorted(rows, key=lambda entry: not entry.is_dir)

    def first_index(self):
        return 1 if (self.entries and self.entries[0].is_parent and
                     len(self.entries) > 1) else 0

    def _apply_tree(self, cursor_path=None):
        self.entries = self._flatten(self.path)
        if cursor_path is not None:
            self.cursor = next((i for i, entry in enumerate(self.entries)
                                if entry.path == cursor_path), self.cursor)
        self.cursor = max(0, min(self.cursor, len(self.entries) - 1))
        visible = {entry.path for entry in self.entries}
        self.selected &= visible
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
            return [e for e in self.entries
                    if not e.is_parent and e.path in self.selected]
        cur = self.current()
        return [cur] if cur and not cur.is_parent else []

    def move(self, delta):
        if self._preview_entry is not None:
            self._preview_scroll = max(0, self._preview_scroll + delta)
            self.app.invalidate()
            return
        if self.entries:
            self.cursor = max(0, min(len(self.entries) - 1, self.cursor + delta))
            self.app.invalidate()

    def _move_to(self, index):
        if self._preview_entry is not None:
            self._preview_scroll = 0 if index <= 0 else 10 ** 9
            self.app.invalidate()
            return
        if self.entries:
            self.cursor = max(0, min(len(self.entries) - 1, index))
            self.app.invalidate()

    def toggle(self):
        cur = self.current()
        if cur and not cur.is_parent:
            if cur.path in self.selected:
                self.selected.remove(cur.path)
            else:
                self.selected.add(cur.path)
            self.move(1)

    def _on_mouse(self, mouse_event):
        """Match the local pane's selection, caret, and double-click behavior."""
        if self.app.consume_menu_click():
            return
        self.app.focus_network_pane(1)
        if self._preview_entry is not None:
            return
        index = self._top + mouse_event.position.y
        if not 0 <= index < len(self.entries):
            return
        self.cursor = index
        entry = self.entries[index]
        if MouseModifier.CONTROL in getattr(
                mouse_event, "modifiers", frozenset()):
            if entry.is_parent:
                pass
            elif entry.path in self.selected:
                self.selected.discard(entry.path)
            else:
                self.selected.add(entry.path)
        elif (entry.is_dir and not entry.is_parent and
              mouse_event.position.x == 4 + 2 * entry.depth):
            self.toggle_expand()
        elif self.app.double_click(("network", id(self)), index):
            self.open()
        self.app.invalidate()

    def open(self):
        cur = self.current()
        if not cur:
            return
        if cur.is_parent:
            self.up()
            return
        if not cur.is_dir:
            if (getattr(self.backend, "protocol", "") == "sftp" and
                    hasattr(self.backend, "read_preview")):
                self.preview_file(cur)
            else:
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

    def toggle_expand(self):
        """Lazily expand or fold the remote directory under the cursor."""
        entry = self.current()
        if (entry is None or not entry.is_dir or entry.is_parent or
                self.busy):
            return
        if entry.path in self.expanded:
            self.expanded.discard(entry.path)
            self._apply_tree(entry.path)
            return
        if entry.path in self._children:
            self.expanded.add(entry.path)
            self._apply_tree(entry.path)
            return

        self.busy = True

        async def do():
            try:
                self._children[entry.path] = await run_in_thread(
                    self._backend_call, self.backend.listdir, entry.path)
                self.expanded.add(entry.path)
                self._apply_tree(entry.path)
            except Exception as exc:
                self.app.set_message(f"cannot expand directory: {exc}")
            finally:
                self.busy = False
                self.app.invalidate()
        asyncio.ensure_future(do())

    def collapse_or_up(self):
        """Fold a directory, move to its tree parent, or leave the directory."""
        if self._preview_entry is not None:
            self.close_preview()
            return
        entry = self.current()
        if entry is not None and entry.is_dir and entry.path in self.expanded:
            self.expanded.discard(entry.path)
            self._apply_tree(entry.path)
            return
        if entry is not None and entry.depth > 0:
            parent = posixpath.dirname(entry.path) or "/"
            self.cursor = next((i for i, row in enumerate(self.entries)
                                if row.path == parent), self.cursor)
            self.app.invalidate()
            return
        self.up()

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

    def go_home(self):
        """Open the login directory reported by the remote server."""
        if self.busy:
            return
        home = posixpath.normpath(getattr(self.backend, "home", "/") or "/")
        if home == self.path:
            self.cursor = self.first_index()
            self.app.invalidate()
            return
        old_path = self.path
        self.path = home
        self.busy = True

        async def do():
            try:
                await self._load()
            except Exception as exc:
                self.path = old_path
                self.app.set_message(f"cannot open remote home: {exc}")
            finally:
                self.busy = False
                self.app.invalidate()
        asyncio.ensure_future(do())

    def open_sort_menu(self):
        labels = [("Name", "name"), ("Size", "size"),
                  ("Date", "date"), ("Type", "type")]
        items = []
        for label, mode in labels:
            for arrow, reverse in (("↑", False), ("↓", True)):
                active = mode == self.sort and reverse == self.reverse
                items.append((
                    ("● " if active else "  ") + label + arrow,
                    lambda mode=mode, reverse=reverse:
                        self.set_sort(mode, reverse),
                ))
        self.app.open_menu("Sort by", items)

    def set_sort(self, mode, reverse=False):
        current = self.current()
        self.sort, self.reverse = mode, reverse
        self._apply_tree(current.path if current else None)

    def start_search(self):
        if self.busy or self.indexing:
            self.app.set_message("wait for the remote operation to finish")
            return
        self.app.enter_network_search()

    def begin_indexing(self):
        token = object()
        self._index_token = token
        self.indexing = True
        return token

    def finish_indexing(self, token):
        if token is self._index_token:
            self.indexing = False

    def cancel_indexing(self):
        self._index_token = object()
        self.indexing = False

    def search_candidates(self):
        """Already loaded rows for the searcher's immediate first stage."""
        out = []
        for entry in self.entries:
            if entry.is_parent:
                continue
            rel = posixpath.relpath(entry.path, self.path)
            out.append(rel + ("/" if entry.is_dir else ""))
        return out

    def gather_search_candidates(self, token=None):
        """Recursively index remote names; called in a worker thread."""
        out = []
        visited = set()
        root = self.path

        def walk(directory):
            if token is not None and token is not self._index_token:
                return
            directory = posixpath.normpath(directory)
            if directory in visited:
                return
            visited.add(directory)
            try:
                entries = self._backend_call(self.backend.listdir, directory)
            except Exception:
                return
            for entry in entries:
                if token is not None and token is not self._index_token:
                    return
                rel = posixpath.relpath(entry.path, root)
                out.append(rel + ("/" if entry.is_dir else ""))
                # Index the link name, but do not recursively follow directory
                # links: a link to an ancestor would otherwise index forever.
                if entry.is_dir and not entry.is_symlink:
                    walk(entry.path)

        walk(root)
        return out

    def open_search_result(self, relative):
        # leaving the search: stop the background index walk so a later `/`
        # is not blocked by a stale `indexing` flag
        self.cancel_indexing()
        target = posixpath.normpath(posixpath.join(
            self.path, relative.rstrip("/")))
        is_dir = relative.endswith("/")
        old_path = self.path
        self.path = target if is_dir else (posixpath.dirname(target) or "/")
        self.busy = True

        async def do():
            try:
                await self._load(None if is_dir else posixpath.basename(target))
                self.app.switch_mode("network")
                self.app.application.layout.focus(self.control)
            except Exception as exc:
                self.path = old_path
                self.app.set_message(f"cannot open search result: {exc}")
            finally:
                self.busy = False
                self.app.invalidate()
        asyncio.ensure_future(do())

    def download(self):
        targets = ([self._preview_entry] if self._preview_entry is not None
                   else self.targets())
        if not targets or self.busy:
            return
        local_view = self.local_view or self.app.explorer
        backend, local_dir = self.backend, Path(local_view.cwd)
        if getattr(backend, "protocol", "") == "sftp":
            self._queue_sftp_downloads(targets, backend, local_view, local_dir)
            return
        self.busy = True
        self.app.set_message(f"downloading {len(targets)} item(s)…")

        async def do():
            done = 0
            try:
                for entry in targets:
                    target = unique_target(local_dir, entry.name)
                    if entry.is_dir:
                        await run_in_thread(
                            self._backend_call, backend.download_tree,
                            entry.path, target)
                    else:
                        await run_in_thread(
                            self._backend_call, backend.download,
                            entry.path, target)
                    done += 1
                local_view.refresh()
                self.app.set_message(f"downloaded {done} item(s) to {local_dir}")
            except Exception as exc:
                self.app.set_message(f"download failed after {done}: {exc}")
            finally:
                self.busy = False
                self.app.invalidate()
        asyncio.ensure_future(do())

    def preview_file(self, entry):
        """Show a bounded SFTP file preview in the remote pane."""
        if self.busy:
            return
        token = object()
        self._preview_token = token
        self._preview_entry = entry
        self._preview_data = None
        self._preview_lines = None
        self._preview_error = None
        self._preview_loading = True
        self._preview_scroll = 0
        self.busy = True
        self.app.invalidate()

        async def do():
            try:
                data = await run_in_thread(
                    self._backend_call, self.backend.read_preview,
                    entry.path, REMOTE_PREVIEW_BYTES)
                if token is self._preview_token:
                    if self._is_binary_preview(entry, data):
                        await self._download_binary_preview(entry)
                    else:
                        self._store_preview_data(data)
            except Exception as exc:
                if token is self._preview_token:
                    self._preview_error = str(exc)
            finally:
                if token is self._preview_token:
                    self._preview_loading = False
                self.busy = False
                self.app.invalidate()
        asyncio.ensure_future(do())

    @staticmethod
    def _is_binary_preview(entry, data):
        if Path(entry.name).suffix.lower() in BINARY_PREVIEW_EXTENSIONS:
            return True
        if b"\x00" in data:
            return True
        if not data:
            return False
        controls = sum(byte < 32 and byte not in (9, 10, 12, 13) for byte in data)
        return controls / len(data) > 0.10

    def _store_preview_data(self, data):
        """Cache remote preview bytes and their decoded logical lines."""
        self._preview_data = data
        text = data[:REMOTE_PREVIEW_BYTES].decode("utf-8", errors="replace")
        self._preview_lines = text.splitlines() or [""]
        self._preview_total = len(self._preview_lines)

    async def _download_binary_preview(self, entry):
        """Download a binary preview modally, then open it with the OS."""
        if self._temp_dir is None:
            self._temp_dir = TemporaryDirectory(
                prefix="nsh-remote-preview-", ignore_cleanup_errors=True)
        safe_name = "".join(
            "_" if char in '<>:"/\\|?*' else char for char in entry.name)
        target = unique_target(Path(self._temp_dir.name), safe_name or "remote-file")
        cancel = threading.Event()
        self._binary_cancel = cancel
        loop = asyncio.get_running_loop()
        self.app.open_progress_dialog(
            "Downloading remote preview", entry.name, cancel.set)

        def progress(done, total):
            if cancel.is_set():
                raise InterruptedError("download cancelled")
            loop.call_soon_threadsafe(
                self.app.update_progress_dialog, done, total)

        succeeded = False
        try:
            await run_in_thread(
                self._backend_call, self.backend.download,
                entry.path, target, progress)
            if not cancel.is_set():
                succeeded = True
        except Exception as exc:
            if cancel.is_set():
                self.app.set_message("preview download cancelled")
            else:
                self.app.set_message(f"preview download failed: {exc}")
        finally:
            self._binary_cancel = None
            self.app.close_progress_dialog()
            self.close_preview()
        if succeeded:
            self.app.open_file(target)

    def close_preview(self):
        self._preview_token = None
        self._preview_entry = None
        self._preview_data = None
        self._preview_lines = None
        self._preview_error = None
        self._preview_loading = False
        self._preview_scroll = 0
        self._preview_total = 0
        self._preview_view = 0
        self.app.invalidate()

    def _queue_sftp_downloads(self, targets, backend, local_view, local_dir):
        for entry in targets:
            label = f"download {entry.path} -> {local_dir}"

            async def operation(cancel, report, entry=entry):
                def progress(done, total):
                    if cancel.is_set():
                        raise InterruptedError("transfer cancelled")
                    report(done, total)

                def transfer():
                    target = unique_target(local_dir, entry.name)
                    if entry.is_dir:
                        backend.download_tree(entry.path, target, callback=progress)
                    else:
                        backend.download(entry.path, target, callback=progress)
                    return target

                target = await run_in_thread(self._backend_call, transfer)
                local_view.refresh()
                self.app.invalidate()
                return f"downloaded {entry.path} -> {target}"

            self.app.remote_shell.enqueue_transfer(label, operation)
        self.selected.clear()
        self.app.open_remote_shell()

    def upload(self):
        if self.busy:
            return
        local_view = self.local_view or self.app.explorer
        paths = list(local_view._targets())
        if not paths:
            self.app.set_message("no local file selected")
            return
        backend, remote_dir = self.backend, self.path
        if getattr(backend, "protocol", "") == "sftp":
            self._queue_sftp_uploads(paths, backend, remote_dir)
            return
        self.busy = True
        self.app.set_message(f"uploading {len(paths)} item(s)…")

        async def do():
            done = 0
            try:
                for path in paths:
                    target = await run_in_thread(
                        self._backend_call, backend.unique_path,
                        remote_dir, path.name)
                    if path.is_dir() and not path.is_symlink():
                        await run_in_thread(
                            self._backend_call, backend.upload_tree, path, target)
                    else:
                        await run_in_thread(
                            self._backend_call, backend.upload, path, target)
                    done += 1
                await self._load()
                self.app.set_message(f"uploaded {done} item(s)")
            except Exception as exc:
                self.app.set_message(f"upload failed after {done}: {exc}")
            finally:
                self.busy = False
                self.app.invalidate()
        asyncio.ensure_future(do())

    def _queue_sftp_uploads(self, paths, backend, remote_dir):
        for path in paths:
            label = f"upload {path} -> {remote_dir}"

            async def operation(cancel, report, path=path):
                def progress(done, total):
                    if cancel.is_set():
                        raise InterruptedError("transfer cancelled")
                    report(done, total)

                def transfer():
                    target = backend.unique_path(remote_dir, path.name)
                    if path.is_dir() and not path.is_symlink():
                        backend.upload_tree(path, target, callback=progress)
                    else:
                        backend.upload(path, target, callback=progress)
                    return target

                target = await run_in_thread(self._backend_call, transfer)
                await self._load()
                return f"uploaded {path} -> {target}"

            self.app.remote_shell.enqueue_transfer(label, operation)
        self.app.open_remote_shell()

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
        if cur and not cur.is_parent:
            self.app.open_input_dialog("Rename remote item", cur.name, len(cur.name),
                                       lambda name: self._rename(cur, name))

    def _rename(self, entry, name):
        name = name.strip()
        if name and name != entry.name:
            self._operation(self.backend.rename, entry.path,
                            posixpath.join(posixpath.dirname(entry.path), name),
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
                    # Directory links are navigable, but delete must unlink the
                    # link itself instead of recursively deleting its target.
                    fn = (self.backend.remove_tree
                          if entry.is_dir and not entry.is_symlink
                          else self.backend.remove)
                    await run_in_thread(self._backend_call, fn, entry.path)
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
                await run_in_thread(self._backend_call, fn, *args)
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
        if self._preview_entry is not None:
            return self._preview_text()
        if self.busy and not self.entries:
            return [("class:preview.dim", "  working…")]
        if not self.entries:
            return [("class:preview.dim", "  (empty directory)")]
        start, end = visible_slice(
            self.window, len(self.entries), self.cursor, self._top, fallback=20)
        self._top = start
        width = (self.window.render_info.window_width
                 if self.window.render_info else 80)
        name_w = max(4, width - 7 - SIZE_COL)
        try:
            cursor_shown = self.app.application.layout.has_focus(self.control)
        except (AttributeError, RuntimeError):
            cursor_shown = True
        out = []
        for i in range(start, end):
            entry = self.entries[i]
            selected = entry.path in self.selected
            on = cursor_shown and i == self.cursor
            base_style = ("class:git.conflict" if entry.is_broken else
                          ("class:explorer.link" if entry.is_symlink else
                           ("class:explorer.dir" if entry.is_dir
                            else ("class:explorer.image" if entry.is_image
                                else "class:explorer.file"))))
            style = "class:explorer.selected" if selected else base_style
            cursor_style = (style + " reverse").strip() if on else style
            name = self._display_name(entry)
            size = "" if entry.is_dir else human_size(entry.size)
            size_style = cursor_style if on else (
                "class:explorer.size" if size else style)
            indent = "  " * entry.depth
            row_name_w = max(4, name_w - 2 * entry.depth)
            icon = (" " if entry.is_parent else
                    ("▾" if entry.is_dir and entry.path in self.expanded
                     else (config.ICONS["dir"] if entry.is_dir
                           else (config.ICONS["image"] if entry.is_image
                                 else config.ICONS["file"]))))
            if entry.is_symlink:
                icon = config.ICONS["link"]
            out.extend([
                (cursor_style, "● " if selected else "  "),
                (cursor_style, "  " + indent),
                (cursor_style, f"{icon} "),
                (cursor_style, pad_to_width(name, row_name_w)),
                (cursor_style, " "),
                (size_style, pad_to_width(size, SIZE_COL, align="right")),
            ])
            if i != end - 1:
                out.append(("", "\n"))
        return out

    @staticmethod
    def _display_name(entry):
        if entry.is_parent:
            return ".."
        suffix = "/" if entry.is_dir else ""
        if entry.is_symlink:
            target = entry.link_target or "?"
            if entry.is_dir:
                target = target.rstrip("/\\") + suffix
            return f"{entry.name}{suffix} -> {target}"
        return entry.name + suffix

    def _preview_text(self):
        """Render the bounded remote file preview in the remote pane."""
        self._preview_total = 0
        self._preview_view = 0
        entry = self._preview_entry
        header = [
            ("class:preview.header", f" {entry.name}\n"),
            ("class:preview.meta", f" {human_size(entry.size)}  {entry.path}\n\n"),
        ]
        if self._preview_loading:
            return header + [("class:preview.dim", " loading preview…")]
        if self._preview_error:
            return header + [
                ("class:preview.dim", f" preview failed: {self._preview_error}")]
        data = self._preview_data or b""
        if not data:
            return header + [("class:preview.dim", " (empty file)")]
        if self._is_binary_preview(entry, data):
            return header + [
                ("class:preview.dim", " (binary file — press c to download)")]
        truncated = len(data) > REMOTE_PREVIEW_BYTES
        lines = getattr(self, "_preview_lines", None)
        if lines is None:  # compatibility for previews populated directly
            self._store_preview_data(data)
            lines = self._preview_lines
        height = (self.window.render_info.window_height
                  if self.window.render_info else 20)
        body_height = max(1, height - 3)
        self._preview_total = len(lines)
        self._preview_view = body_height
        maximum = max(0, len(lines) - body_height)
        self._preview_scroll = min(self._preview_scroll, maximum)
        visible = lines[self._preview_scroll:self._preview_scroll + body_height]
        out = list(header)
        for index, line in enumerate(visible):
            out.append(("class:shell.output", line))
            if index != len(visible) - 1:
                out.append(("", "\n"))
        if truncated and self._preview_scroll >= maximum:
            out.extend([("", "\n"),
                        ("class:preview.dim", " … (preview truncated)")])
        return out

    def _keys(self):
        kb = KeyBindings()
        kb.add("up")(lambda e: self.move(-1))
        kb.add("k")(lambda e: self.move(-1))
        kb.add("down")(lambda e: self.move(1))
        kb.add("j")(lambda e: self.move(1))
        kb.add("g")(lambda e: self._move_to(0))
        kb.add("home")(lambda e: self._move_to(0))
        kb.add("G")(lambda e: self._move_to(len(self.entries) - 1))
        kb.add("end")(lambda e: self._move_to(len(self.entries) - 1))
        kb.add("~")(lambda e: self.go_home())
        kb.add("s")(lambda e: self.open_sort_menu())
        kb.add("/")(lambda e: self.start_search())
        kb.add(":")(lambda e: self.app.open_remote_shell())
        kb.add("enter")(lambda e: self.open())
        kb.add("right")(lambda e: self.toggle_expand())
        kb.add("l")(lambda e: self.toggle_expand())
        kb.add("left")(lambda e: self.collapse_or_up())
        kb.add("h")(lambda e: self.collapse_or_up())
        kb.add("backspace")(lambda e: self.collapse_or_up())
        kb.add(" ")(lambda e: self.toggle())
        kb.add("tab")(lambda e: self.actions())
        kb.add("c")(lambda e: self.download())
        kb.add("n")(lambda e: self.new_dir())
        kb.add("i")(lambda e: self.rename())
        kb.add("D")(lambda e: self.delete())
        kb.add("r")(lambda e: self.refresh())
        kb.add("q")(lambda e: self.app.exit())
        kb.add("H")(lambda e: self.app.focus_network_pane(-1))
        kb.add("L")(lambda e: self.app.focus_network_pane(1))
        kb.add("escape")(lambda e: self.cancel())
        kb.add("2")(lambda e: None)
        return kb
