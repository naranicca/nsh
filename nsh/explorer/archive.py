import bz2
import gzip
import lzma
import os
import shutil
import stat
import tarfile
import threading
import zipfile
from collections import OrderedDict, namedtuple
from pathlib import Path

# A member of an archive as the listing needs it. ``key`` is the raw name the
# archive itself stores (what the zip/tar reader must be handed back), which can
# differ from ``name`` when a legacy zip's name had to be re-decoded.
Member = namedtuple("Member", "name is_dir size mtime key")
Format = namedtuple("Format", "kind comp label")

# tar suffixes -> the compression tarfile should use. Matched longest-first so
# ".tar.gz" wins over ".gz".
_TAR_SUFFIXES = {
    ".tar": "",
    ".tar.gz": "gz", ".tgz": "gz", ".taz": "gz",
    ".tar.bz2": "bz2", ".tbz": "bz2", ".tbz2": "bz2", ".tb2": "bz2",
    ".tar.xz": "xz", ".txz": "xz",
}
_TAR_ORDER = sorted(_TAR_SUFFIXES, key=len, reverse=True)
_ZIP_SUFFIXES = {".zip", ".jar", ".war", ".whl", ".egg", ".apk", ".xpi"}
_SOLO_SUFFIXES = {".gz": "gz", ".bz2": "bz2", ".xz": "xz"}

SUGGESTED_SUFFIXES = (".zip", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar",
                      ".gz", ".bz2", ".xz")

MAX_CACHED = 8
_cache = OrderedDict()
_cache_lock = threading.Lock()


class ArchiveError(Exception):
    """An archive could not be read, created, or written."""


# -- format ------------------------------------------------------------------
def archive_format(name):
    """The :class:`Format` implied by ``name``'s extension, else ``None``.

    A name that is *only* a suffix (``.gz``) is not an archive: it is a hidden
    file that happens to be called that.
    """
    low = os.path.basename(str(name)).lower()
    for suffix in _TAR_ORDER:
        if low.endswith(suffix) and len(low) > len(suffix):
            return Format("tar", _TAR_SUFFIXES[suffix], suffix.lstrip("."))
    ext = os.path.splitext(low)[1]
    if ext in _ZIP_SUFFIXES and len(low) > len(ext):
        return Format("zip", "", ext.lstrip("."))
    if ext in _SOLO_SUFFIXES and len(low) > len(ext):
        return Format("solo", _SOLO_SUFFIXES[ext], ext.lstrip("."))
    return None


def is_archive_name(name) -> bool:
    """Whether ``name`` looks like an archive (extension only, no I/O)."""
    return archive_format(name) is not None


def split_path(path):
    """Split a possibly-virtual ``path`` into ``(archive file, inner path)``.

    ``inner`` is ``""`` for the archive itself and a ``/``-joined relative path
    for a member. Returns ``None`` when no component of ``path`` is an archive
    file on disk, which is the common case, so the name check runs first and the
    filesystem is touched only for a path that could plausibly be virtual.
    An archive nested *inside* another archive is not a file on disk and so is
    not entered.
    """
    parts = Path(path).parts
    if not parts:
        return None
    for i, part in enumerate(parts):
        if not is_archive_name(part):
            continue
        candidate = Path(*parts[:i + 1])
        try:
            if candidate.is_file():
                return candidate, "/".join(parts[i + 1:])
        except OSError:
            return None
    return None


def inside_archive(path) -> bool:
    """Whether ``path`` is an archive or something inside one."""
    return split_path(path) is not None


def is_member(path) -> bool:
    """Whether ``path`` names something *inside* an archive.

    Unlike :func:`inside_archive` this is false for the archive file itself,
    which is an ordinary file on disk: it can be copied, renamed and deleted
    like any other. Its members cannot, so write operations check this.
    """
    split = split_path(path)
    return split is not None and bool(split[1])


def archive_stem(name):
    """``name`` without its archive suffix: ``src.tar.gz`` -> ``src``.

    Used to propose a destination directory when extracting. Returns ``name``
    unchanged when it is not an archive name.
    """
    base = os.path.basename(str(name))
    low = base.lower()
    for suffix in _TAR_ORDER:
        if low.endswith(suffix) and len(low) > len(suffix):
            return base[:-len(suffix)]
    stem, ext = os.path.splitext(base)
    if ext.lower() in _ZIP_SUFFIXES | set(_SOLO_SUFFIXES) and stem:
        return stem
    return base


def real_ancestor(path):
    """The nearest real directory at or above ``path``.

    For a virtual path this is the directory holding the archive, which is where
    a shell started "in" an archive should actually run.
    """
    split = split_path(path)
    if split is None:
        return Path(path)
    return split[0].parent


# -- reading -----------------------------------------------------------------
def _zip_name(info):
    """A zip member's name, repairing a legacy non-UTF-8 one.

    Without the UTF-8 flag :mod:`zipfile` decodes names as cp437, which turns a
    zip written by a Korean Windows tool into mojibake. Re-encode and try the
    encodings the rest of nsh uses before giving up on cp437.
    """
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for encoding in ("utf-8", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return name


def _zip_mtime(info):
    try:
        import calendar
        return int(calendar.timegm(tuple(info.date_time) + (0, 0, -1)))
    except (ValueError, TypeError, OverflowError):
        return 0


def _zip_members(path):
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            name = _zip_name(info).replace("\\", "/").strip("/")
            if not name:
                continue
            yield Member(name, info.is_dir(), 0 if info.is_dir() else info.file_size,
                         _zip_mtime(info), info.filename)


def _tar_members(path):
    with tarfile.open(path, "r:*") as tf:
        for info in tf:
            if not (info.isfile() or info.isdir() or info.issym() or info.islnk()):
                continue  # devices and fifos are not browsable content
            name = info.name.replace("\\", "/").strip("/")
            if not name or name == ".":
                continue
            yield Member(name, info.isdir(), 0 if info.isdir() else info.size,
                         int(info.mtime), info.name)


def _solo_inner_name(path):
    """The single member's name: the archive's name minus its suffix."""
    base = os.path.basename(str(path))
    stem = os.path.splitext(base)[0]
    return stem or base


def _gzip_size(path):
    """The uncompressed size recorded in a gzip trailer (modulo 4 GiB)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(-4, os.SEEK_END)
            return int.from_bytes(fh.read(4), "little")
    except (OSError, ValueError):
        return 0


def _solo_members(path, fmt):
    size = _gzip_size(path) if fmt.comp == "gz" else 0
    try:
        mtime = int(os.stat(path).st_mtime)
    except OSError:
        mtime = 0
    name = _solo_inner_name(path)
    yield Member(name, False, size, mtime, name)


def _read_members(path, fmt):
    if fmt.kind == "zip":
        return list(_zip_members(path))
    if fmt.kind == "tar":
        return list(_tar_members(path))
    return list(_solo_members(path, fmt))


def _build_tree(members):
    """Group ``members`` into ``{directory inner path: {name: Member}}``.

    Archives often omit entries for the directories they imply, so every
    member's parents are synthesized as needed.
    """
    tree = {"": {}}

    def ensure_dir(inner, mtime=0):
        if inner in tree:
            return
        tree[inner] = {}
        parent, _, name = inner.rpartition("/")
        ensure_dir(parent)
        tree[parent].setdefault(name, Member(name, True, 0, mtime, inner))

    for member in members:
        parent, _, base = member.name.rpartition("/")
        ensure_dir(parent, member.mtime)
        if member.is_dir:
            ensure_dir(member.name, member.mtime)
            tree[parent][base] = Member(base, True, 0, member.mtime, member.key)
        else:
            # a later member with the same name replaces the earlier one, which
            # matches how extracting the whole archive in order would end up
            tree[parent][base] = Member(base, False, member.size, member.mtime,
                                        member.key)
    return tree


def _cache_key(path):
    st = os.stat(path)
    return (str(path), st.st_mtime_ns, st.st_size)


def read_tree(archive):
    """``{directory: {name: Member}}`` for ``archive``, cached by file identity.

    Raises :class:`ArchiveError` when the file cannot be opened as its format.
    """
    archive = Path(archive)
    fmt = archive_format(archive.name)
    if fmt is None:
        raise ArchiveError(f"not an archive: {archive.name}")
    try:
        key = _cache_key(archive)
    except OSError as exc:
        raise ArchiveError(str(exc)) from exc
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    try:
        tree = _build_tree(_read_members(archive, fmt))
    except ArchiveError:
        raise
    except Exception as exc:  # noqa: BLE001 - every archive library has its own
        raise ArchiveError(f"{archive.name}: {exc}") from exc
    with _cache_lock:
        _cache[key] = tree
        while len(_cache) > MAX_CACHED:
            _cache.popitem(last=False)
    return tree


def forget(archive=None):
    """Drop cached listings (for ``archive``, or all of them)."""
    with _cache_lock:
        if archive is None:
            _cache.clear()
            return
        prefix = str(archive)
        for key in [k for k in _cache if k[0] == prefix]:
            del _cache[key]


def list_children(path):
    """Members directly inside the virtual directory ``path``.

    Returns ``None`` when ``path`` is not an archive or inside one, so callers
    can fall back to the real filesystem, and ``[]`` for an archive that cannot
    be read or a member path that does not exist.
    """
    split = split_path(path)
    if split is None:
        return None
    archive, inner = split
    try:
        tree = read_tree(archive)
    except ArchiveError:
        return []
    children = tree.get(inner)
    if children is None:
        return []
    return sorted(children.values(), key=lambda m: m.name.lower())


def member_of(path):
    """The :class:`Member` a virtual ``path`` names, or ``None``."""
    split = split_path(path)
    if split is None or not split[1]:
        return None
    archive, inner = split
    parent, _, base = inner.rpartition("/")
    try:
        tree = read_tree(archive)
    except ArchiveError:
        return None
    return tree.get(parent, {}).get(base)


def read_member(path, limit=None):
    """Bytes of the file at virtual ``path`` (at most ``limit`` of them)."""
    split = split_path(path)
    if split is None or not split[1]:
        raise ArchiveError("not a file inside an archive")
    archive, inner = split
    member = member_of(path)
    if member is None:
        raise ArchiveError(f"not in the archive: {inner}")
    if member.is_dir:
        raise ArchiveError(f"is a directory: {inner}")
    fmt = archive_format(archive.name)
    try:
        return _read_one(archive, fmt, member, limit)
    except ArchiveError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ArchiveError(f"{inner}: {exc}") from exc


def _read_one(archive, fmt, member, limit):
    if fmt.kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            with zf.open(member.key) as fh:
                return fh.read() if limit is None else fh.read(limit)
    if fmt.kind == "tar":
        with tarfile.open(archive, "r:*") as tf:
            fh = tf.extractfile(member.key)
            if fh is None:
                raise ArchiveError(f"cannot read {member.name}")
            with fh:
                return fh.read() if limit is None else fh.read(limit)
    opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[fmt.comp]
    with opener(archive, "rb") as fh:
        return fh.read() if limit is None else fh.read(limit)


# -- extracting --------------------------------------------------------------
def _safe_parts(name):
    """``name`` split into components, or ``None`` if it tries to escape.

    A leading ``/`` is stripped the way ``tar`` and ``unzip`` do; ``..`` and a
    Windows drive-relative name (``C:evil``) are refused outright.
    """
    parts = [p for p in str(name).replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    if any(os.path.splitdrive(p)[0] or os.path.isabs(p) for p in parts):
        return None
    return parts


def _safe_target(dest_dir, parts):
    """Where ``parts`` may be written under ``dest_dir``, or ``None``."""
    target = Path(dest_dir).joinpath(*parts)
    try:
        target.relative_to(Path(dest_dir))
    except ValueError:
        return None
    return target


def _unique(path):
    """``path`` if free, else the ``name (2)`` form :mod:`fileops` uses."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    index = 2
    while True:
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _top_level_map(tops, dest_dir):
    """Rename map for the top-level names an extraction would create, so an
    existing ``src/`` becomes ``src (2)/`` instead of being merged into."""
    mapping = {}
    for top in tops:
        if top in mapping:
            continue
        mapping[top] = _unique(Path(dest_dir) / top).name
    return mapping


def extract(archive, dest_dir, inner=None, on_progress=None):
    """Extract ``archive`` into ``dest_dir``; return ``(written, errors)``.

    ``inner`` limits the extraction to one member and everything below it, which
    is how a member is copied out of the virtual filesystem. Existing top-level
    names are never overwritten: they get the usual ``name (2)`` treatment.
    Members that would escape ``dest_dir`` are skipped and reported.
    """
    archive = Path(archive)
    dest_dir = Path(dest_dir)
    fmt = archive_format(archive.name)
    if fmt is None:
        raise ArchiveError(f"not an archive: {archive.name}")
    tree = read_tree(archive)
    members = _flatten(tree, inner or "")
    if not members:
        raise ArchiveError("nothing to extract")
    prefix = ""
    if inner:
        # copying one member out: name it by its own basename, not its full path
        prefix = inner.rpartition("/")[0]
        prefix = prefix + "/" if prefix else ""
    # Drop escaping members *before* the collision map is built: a ".." left in
    # it would come back as a renamed top level and sail past the safety check.
    safe, errors = [], []
    for member in members:
        parts = _safe_parts(member.name[len(prefix):])
        if parts is None:
            errors.append(f"{member.name}: unsafe path, skipped")
            continue
        safe.append((member, parts))
    mapping = _top_level_map([parts[0] for _member, parts in safe], dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    written = []
    total = len(safe)
    for index, (member, parts) in enumerate(safe, 1):
        target = _safe_target(dest_dir, [mapping.get(parts[0], parts[0])] + parts[1:])
        if target is None:
            errors.append(f"{member.name}: unsafe path, skipped")
            continue
        if on_progress is not None:
            on_progress(index, total, member.name)
        try:
            if member.is_dir:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as out:
                    out.write(_read_one(archive, fmt, member, None))
                if member.mtime:
                    os.utime(target, (member.mtime, member.mtime))
            written.append(target)
        except Exception as exc:  # noqa: BLE001 - reported per member
            errors.append(f"{member.name}: {exc}")
    return written, errors


def _flatten(tree, inner):
    """Every member at or below ``inner``, parents before children."""
    out = []
    if inner:
        parent, _, base = inner.rpartition("/")
        member = tree.get(parent, {}).get(base)
        if member is None:
            return []
        if not member.is_dir:
            return [Member(inner, False, member.size, member.mtime, member.key)]
        out.append(Member(inner, True, 0, member.mtime, member.key))

    def walk(directory):
        for member in sorted(tree.get(directory, {}).values(), key=lambda m: m.name):
            full = f"{directory}/{member.name}" if directory else member.name
            out.append(Member(full, member.is_dir, member.size, member.mtime,
                              member.key))
            if member.is_dir:
                walk(full)

    walk(inner)
    return out


# -- creating ----------------------------------------------------------------
def _walk_sources(sources, base_dir):
    """``(real path, arcname)`` pairs for ``sources``, directories expanded."""
    base_dir = Path(base_dir)
    out = []
    for source in sources:
        source = Path(source)
        try:
            root = source.relative_to(base_dir).as_posix()
        except ValueError:
            root = source.name
        if source.is_dir() and not source.is_symlink():
            out.append((source, root))
            for dirpath, dirnames, filenames in os.walk(source):
                dirnames.sort()
                here = Path(dirpath)
                for name in sorted(dirnames):
                    child = here / name
                    out.append((child, f"{root}/{child.relative_to(source).as_posix()}"))
                for name in sorted(filenames):
                    child = here / name
                    out.append((child, f"{root}/{child.relative_to(source).as_posix()}"))
            continue
        out.append((source, root))
    # os.walk already yields parents first; drop the duplicate directory rows
    seen, unique = set(), []
    for path, arcname in out:
        if arcname in seen:
            continue
        seen.add(arcname)
        unique.append((path, arcname))
    return unique


def create(target, sources, base_dir, on_progress=None):
    """Compress ``sources`` into a new archive at ``target``.

    The format comes from ``target``'s extension. Names inside the archive are
    relative to ``base_dir``. ``target`` is never overwritten; the path actually
    written is returned.
    """
    target = Path(target)
    fmt = archive_format(target.name)
    if fmt is None:
        raise ArchiveError(
            f"unknown archive type: {target.name}  "
            f"(use {', '.join(SUGGESTED_SUFFIXES)})")
    sources = [Path(s) for s in sources]
    if not sources:
        raise ArchiveError("nothing to compress")
    target = _unique(target)
    items = _walk_sources(sources, base_dir)
    try:
        if fmt.kind == "zip":
            _create_zip(target, items, on_progress)
        elif fmt.kind == "tar":
            _create_tar(target, items, fmt, on_progress)
        else:
            _create_solo(target, sources, fmt, on_progress)
    except ArchiveError:
        _discard(target)
        raise
    except Exception as exc:  # noqa: BLE001
        _discard(target)
        raise ArchiveError(f"{target.name}: {exc}") from exc
    forget(target)
    return target


def _discard(path):
    """Remove a half-written archive so a failure leaves nothing behind."""
    try:
        path.unlink()
    except OSError:
        pass


def _create_zip(target, items, on_progress):
    total = len(items)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for index, (path, arcname) in enumerate(items, 1):
            if on_progress is not None:
                on_progress(index, total, arcname)
            if path.is_dir() and not path.is_symlink():
                zf.writestr(arcname + "/", b"")
            else:
                zf.write(path, arcname)


def _create_tar(target, items, fmt, on_progress):
    total = len(items)
    with tarfile.open(target, f"w:{fmt.comp}" if fmt.comp else "w") as tf:
        for index, (path, arcname) in enumerate(items, 1):
            if on_progress is not None:
                on_progress(index, total, arcname)
            tf.add(path, arcname=arcname, recursive=False)


def _create_solo(target, sources, fmt, on_progress):
    """gzip/bzip2/xz hold exactly one file, so they cannot take a folder."""
    if len(sources) != 1 or (sources[0].is_dir() and not sources[0].is_symlink()):
        raise ArchiveError(
            f".{fmt.label} holds a single file; use .tar.{fmt.label} instead")
    if on_progress is not None:
        on_progress(1, 1, sources[0].name)
    opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[fmt.comp]
    with open(sources[0], "rb") as src, opener(target, "wb") as dst:
        shutil.copyfileobj(src, dst)


# -- writing back ------------------------------------------------------------
def rename_member(path, new_name):
    """Rename the member at virtual ``path`` to ``new_name`` inside its archive.

    Archives have no way to rename in place, so the whole file is rewritten next
    to itself and swapped in only once it is complete; an interrupted rename
    leaves the original untouched. Renaming a directory moves everything below
    it. Returns the member's new virtual path.
    """
    split = split_path(path)
    if split is None or not split[1]:
        raise ArchiveError("not a member of an archive")
    archive, inner = split
    if not new_name or new_name in (".", "..") or "/" in new_name or "\\" in new_name:
        raise ArchiveError(f"invalid name: {new_name}")
    fmt = archive_format(archive.name)
    if fmt.kind == "solo":
        raise ArchiveError(
            f"a .{fmt.label} file is named by the archive itself; rename that")
    tree = read_tree(archive)
    parent, _, base = inner.rpartition("/")
    if base not in tree.get(parent, {}):
        raise ArchiveError(f"not in the archive: {inner}")
    if new_name in tree.get(parent, {}):
        raise ArchiveError(f"already exists: {new_name}")
    new_inner = f"{parent}/{new_name}" if parent else new_name
    renamed = {}  # raw key -> new raw name

    def remap(name):
        """``name`` with the renamed member's path segment substituted."""
        if name == inner:
            return new_inner
        if name.startswith(inner + "/"):
            return new_inner + name[len(inner):]
        return name

    for member in _flatten(tree, inner):
        renamed[member.key] = remap(member.name)
    temporary = archive.with_name(archive.name + ".nsh-tmp")
    _discard(temporary)
    try:
        if fmt.kind == "zip":
            _rewrite_zip(archive, temporary, renamed)
        else:
            _rewrite_tar(archive, temporary, fmt, renamed)
        os.replace(temporary, archive)
    except ArchiveError:
        _discard(temporary)
        raise
    except Exception as exc:  # noqa: BLE001
        _discard(temporary)
        raise ArchiveError(f"{archive.name}: {exc}") from exc
    forget(archive)
    return archive / new_inner


def _rewrite_zip(source, target, renamed):
    with zipfile.ZipFile(source) as src, \
            zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            name = renamed.get(info.filename)
            if name is None:
                dst.writestr(info, src.read(info.filename))
                continue
            new_info = zipfile.ZipInfo(name + ("/" if info.is_dir() else ""),
                                       date_time=info.date_time)
            new_info.compress_type = info.compress_type
            new_info.external_attr = info.external_attr
            new_info.internal_attr = info.internal_attr
            new_info.create_system = info.create_system
            # the repaired display name is what we rewrote, so it must now be
            # flagged UTF-8 or the next reader would mis-decode it as cp437
            new_info.flag_bits = info.flag_bits | 0x800
            dst.writestr(new_info, src.read(info.filename))


def _rewrite_tar(source, target, fmt, renamed):
    mode = f"w:{fmt.comp}" if fmt.comp else "w"
    with tarfile.open(source, "r:*") as src, tarfile.open(target, mode) as dst:
        for info in src:
            name = renamed.get(info.name)
            # the clone carries no data offset, so the payload must always be
            # read through the *original* member
            out = _copy_tarinfo(info, name) if name is not None else info
            if info.islnk() and info.linkname in renamed:
                out = out if out is not info else _copy_tarinfo(info, info.name)
                out.linkname = renamed[info.linkname]
            if info.isfile():
                dst.addfile(out, src.extractfile(info))
            else:
                dst.addfile(out)


def _copy_tarinfo(info, name):
    clone = tarfile.TarInfo(name)
    for field in ("mode", "uid", "gid", "size", "mtime", "type", "linkname",
                  "uname", "gname", "devmajor", "devminor"):
        setattr(clone, field, getattr(info, field))
    return clone


def describe(path):
    """A one-line summary of an archive for the preview header."""
    try:
        tree = read_tree(path)
    except ArchiveError as exc:
        return str(exc)
    files = sum(1 for members in tree.values() for m in members.values()
                if not m.is_dir)
    folders = max(0, len(tree) - 1)
    fmt = archive_format(Path(path).name)
    return f"{fmt.label} archive · {files} files · {folders} folders"


def member_mode(member):
    """A plausible ``ls -l`` mode string for an archive member."""
    return stat.filemode((stat.S_IFDIR | 0o755) if member.is_dir
                         else (stat.S_IFREG | 0o644))
