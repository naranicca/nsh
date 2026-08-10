"""File operations: copy / move / delete / rename / mkdir / touch.

Potentially slow operations (copy, move, delete of large trees) run in a worker
thread via :func:`run_in_thread` so the UI event loop stays responsive; callers
``await`` them and refresh the listing afterwards. The cheap metadata operations
(rename, mkdir, touch) run inline.

Nothing here ever clobbers an existing path: paste targets are de-duplicated by
:func:`unique_target`, and rename/mkdir/touch refuse to overwrite.
"""
import ctypes
import os
import stat
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from ..util.aio import run_in_thread


def unique_target(dst_dir, name: str) -> Path:
    """A path inside ``dst_dir`` named ``name`` that does not yet exist.

    ``report.txt`` -> ``report (2).txt`` -> ``report (3).txt`` ...
    """
    dst_dir = Path(dst_dir)
    target = dst_dir / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    i = 2
    while True:
        cand = dst_dir / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


def _ensure_not_into_self(src: Path, target: Path) -> None:
    if src.is_dir() and (target == src or src in target.parents):
        raise ValueError("cannot copy a directory into itself")


async def copy(src, dst_dir) -> Path:
    """Copy ``src`` into ``dst_dir``; return the created path."""
    src = Path(src)
    target = unique_target(dst_dir, src.name)
    _ensure_not_into_self(src, target)

    def _do():
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, target, symlinks=True)
        else:
            shutil.copy2(src, target, follow_symlinks=False)

    await run_in_thread(_do)
    return target


async def move(src, dst_dir) -> Path:
    """Move ``src`` into ``dst_dir``; return the new path."""
    src = Path(src)
    target = unique_target(dst_dir, src.name)
    _ensure_not_into_self(src, target)
    await run_in_thread(shutil.move, str(src), str(target))
    return target


async def delete(path) -> None:
    """Permanently delete ``path`` (file, symlink, or directory tree)."""
    path = Path(path)

    def remove_readonly(func, item, _exc_info):
        """Clear a read-only bit and retry the failed rmtree operation."""
        os.chmod(item, stat.S_IWRITE | stat.S_IREAD)
        func(item)

    def _unlink():
        try:
            path.unlink()
        except PermissionError:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            path.unlink()

    def _do():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, onerror=remove_readonly)
        else:
            _unlink()

    await run_in_thread(_do)


def _trash_windows(path: Path) -> None:
    """Move a path to the Windows Recycle Bin using the native shell API."""
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3  # FO_DELETE
    # Do not use Path.resolve(): it follows symlinks, while trashing a symlink
    # must move the link itself rather than its target.
    operation.pFrom = os.path.abspath(str(path)) + "\0\0"
    operation.fFlags = 0x0040 | 0x0010 | 0x0004  # ALLOWUNDO, NOCONFIRMATION, SILENT
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted:
        raise OSError(result or 1, "could not move item to the Recycle Bin", path)


def _trash_macos(path: Path) -> None:
    trash_dir = Path.home() / ".Trash"
    trash_dir.mkdir(mode=0o700, exist_ok=True)
    shutil.move(str(path), str(unique_target(trash_dir, path.name)))


def _trash_freedesktop(path: Path) -> None:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    trash_root = data_home / "Trash"
    files_dir, info_dir = trash_root / "files", trash_root / "info"
    files_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    info_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    target = unique_target(files_dir, path.name)
    info = info_dir / f"{target.name}.trashinfo"
    # An interrupted/externally modified trash may contain orphan metadata.
    # Never overwrite it; pick the same numbered form used for file collisions.
    index = 2
    while info.exists():
        target = files_dir / f"{path.stem} ({index}){path.suffix}"
        info = info_dir / f"{target.name}.trashinfo"
        if not target.exists() and not info.exists():
            break
        index += 1
    original = path.absolute()
    info.write_text(
        "[Trash Info]\n"
        f"Path={quote(str(original), safe='/')}\n"
        f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n",
        encoding="utf-8")
    try:
        shutil.move(str(path), str(target))
    except Exception:
        try:
            info.unlink()
        except FileNotFoundError:
            pass
        raise


async def trash(path) -> None:
    """Move ``path`` to the platform trash without falling back to deletion."""
    path = Path(path)

    def _do():
        if sys.platform == "win32":
            _trash_windows(path)
        elif sys.platform == "darwin":
            _trash_macos(path)
        else:
            _trash_freedesktop(path)

    await run_in_thread(_do)


def rename(path, new_name: str) -> Path:
    path = Path(path)
    target = path.with_name(new_name)
    if target.exists():
        raise FileExistsError(new_name)
    path.rename(target)
    return target


def make_dir(parent, name: str) -> Path:
    target = Path(parent) / name
    target.mkdir(parents=False, exist_ok=False)
    return target


def make_file(parent, name: str) -> Path:
    target = Path(parent) / name
    if target.exists():
        raise FileExistsError(name)
    target.touch()
    return target
