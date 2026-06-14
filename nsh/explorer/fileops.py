"""File operations: copy / move / delete / rename / mkdir / touch.

Potentially slow operations (copy, move, delete of large trees) run in a worker
thread via :func:`asyncio.to_thread` so the UI event loop stays responsive;
callers ``await`` them and refresh the listing afterwards. The cheap metadata
operations (rename, mkdir, touch) run inline.

Nothing here ever clobbers an existing path: paste targets are de-duplicated by
:func:`unique_target`, and rename/mkdir/touch refuse to overwrite.
"""
import asyncio
import shutil
from pathlib import Path


def unique_target(dst_dir, name: str) -> Path:
    """A path inside ``dst_dir`` named ``name`` that does not yet exist.

    ``report.txt`` -> ``report copy.txt`` -> ``report copy 2.txt`` ...
    """
    dst_dir = Path(dst_dir)
    target = dst_dir / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    i = 1
    while True:
        label = "copy" if i == 1 else f"copy {i}"
        cand = dst_dir / f"{stem} {label}{suffix}"
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

    await asyncio.to_thread(_do)
    return target


async def move(src, dst_dir) -> Path:
    """Move ``src`` into ``dst_dir``; return the new path."""
    src = Path(src)
    target = unique_target(dst_dir, src.name)
    _ensure_not_into_self(src, target)
    await asyncio.to_thread(shutil.move, str(src), str(target))
    return target


async def delete(path) -> None:
    """Permanently delete ``path`` (file, symlink, or directory tree)."""
    path = Path(path)

    def _do():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    await asyncio.to_thread(_do)


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
