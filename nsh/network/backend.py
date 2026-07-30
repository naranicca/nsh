"""Synchronous remote filesystem backends, called from worker threads."""
import ftplib
import os
import posixpath
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


@dataclass
class RemoteEntry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mtime: int = 0


def parse_target(protocol, target):
    """Return ``(host, port, username, path)`` from a compact connection target."""
    raw = target.strip()
    if "://" not in raw:
        raw = protocol + "://" + raw
    parsed = urlsplit(raw)
    if not parsed.hostname:
        raise ValueError("host is required")
    default_port = 22 if protocol == "sftp" else 21
    username = unquote(parsed.username or ("anonymous" if protocol == "ftp" else ""))
    if protocol == "sftp" and not username:
        username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    return parsed.hostname, parsed.port or default_port, username, unquote(parsed.path or "/")


class RemoteBackend:
    protocol = "remote"

    def __init__(self, host, port, username):
        self.host, self.port, self.username = host, port, username

    @property
    def label(self):
        user = f"{self.username}@" if self.username else ""
        return f"{self.protocol}://{user}{self.host}:{self.port}"

    def close(self):
        pass

    def remove_tree(self, path):
        for entry in self.listdir(path):
            if entry.is_dir:
                self.remove_tree(entry.path)
            else:
                self.remove(entry.path)
        self.rmdir(path)

    def download_tree(self, remote, local):
        local = Path(local)
        local.mkdir(parents=True, exist_ok=True)
        for entry in self.listdir(remote):
            target = local / entry.name
            if entry.is_dir:
                self.download_tree(entry.path, target)
            else:
                self.download(entry.path, target)

    def upload_tree(self, local, remote):
        local = Path(local)
        self.mkdir(remote)
        for child in local.iterdir():
            target = posixpath.join(remote, child.name)
            if child.is_dir() and not child.is_symlink():
                self.upload_tree(child, target)
            else:
                self.upload(child, target)

    def unique_path(self, directory, name):
        """Non-existing remote path using nsh's ``name (2)`` convention."""
        existing = {entry.name for entry in self.listdir(directory)}
        if name not in existing:
            return posixpath.join(directory, name)
        stem, suffix = posixpath.splitext(name)
        index = 2
        while True:
            candidate = f"{stem} ({index}){suffix}"
            if candidate not in existing:
                return posixpath.join(directory, candidate)
            index += 1


class FTPBackend(RemoteBackend):
    protocol = "ftp"

    @classmethod
    def connect(cls, host, port, username, password):
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=15)
        ftp.login(username or "anonymous", password or "anonymous@")
        ftp.encoding = "utf-8"
        obj = cls(host, port, username or "anonymous")
        obj.client = ftp
        return obj

    def close(self):
        try:
            self.client.quit()
        except Exception:
            try:
                self.client.close()
            except Exception:
                pass

    def listdir(self, path):
        entries = []
        try:
            rows = list(self.client.mlsd(path, facts=["type", "size", "modify"]))
            for name, facts in rows:
                if name in (".", ".."):
                    continue
                kind = facts.get("type", "")
                entries.append(RemoteEntry(
                    name, posixpath.join(path, name), kind in ("dir", "cdir", "pdir"),
                    int(facts.get("size", 0) or 0), 0))
        except (ftplib.error_perm, AttributeError):
            # Older servers may not implement MLSD. Probe each NLST result.
            old = self.client.pwd()
            for full in self.client.nlst(path):
                name = posixpath.basename(full.rstrip("/"))
                if name in (".", ".."):
                    continue
                full = full if full.startswith("/") else posixpath.join(path, full)
                is_dir = False
                try:
                    self.client.cwd(full)
                    is_dir = True
                    self.client.cwd(old)
                except ftplib.error_perm:
                    pass
                size = 0
                if not is_dir:
                    try:
                        size = self.client.size(full) or 0
                    except ftplib.error_perm:
                        pass
                entries.append(RemoteEntry(name, full, is_dir, size))
        return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))

    def mkdir(self, path):
        return self.client.mkd(path)

    def remove(self, path):
        self.client.delete(path)

    def rmdir(self, path):
        self.client.rmd(path)

    def rename(self, old, new):
        self.client.rename(old, new)

    def download(self, remote, local):
        with open(local, "wb") as stream:
            self.client.retrbinary("RETR " + remote, stream.write)

    def upload(self, local, remote):
        with open(local, "rb") as stream:
            self.client.storbinary("STOR " + remote, stream)


class SFTPBackend(RemoteBackend):
    protocol = "sftp"

    @classmethod
    def connect(cls, host, port, username, password):
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("SFTP requires paramiko (pip install paramiko)") from exc
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        ssh.connect(host, port=port, username=username or None,
                    password=password or None, timeout=15,
                    allow_agent=True, look_for_keys=True)
        obj = cls(host, port, username)
        obj.ssh = ssh
        obj.client = ssh.open_sftp()
        return obj

    def close(self):
        try:
            self.client.close()
        finally:
            self.ssh.close()

    def listdir(self, path):
        entries = []
        for attr in self.client.listdir_attr(path):
            name = attr.filename
            entries.append(RemoteEntry(
                name, posixpath.join(path, name), stat.S_ISDIR(attr.st_mode),
                attr.st_size or 0, int(attr.st_mtime or 0)))
        return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))

    def mkdir(self, path):
        self.client.mkdir(path)

    def remove(self, path):
        self.client.remove(path)

    def rmdir(self, path):
        self.client.rmdir(path)

    def rename(self, old, new):
        self.client.rename(old, new)

    def download(self, remote, local):
        self.client.get(remote, os.fspath(local))

    def upload(self, local, remote):
        self.client.put(os.fspath(local), remote)


def connect(protocol, target, password):
    host, port, username, path = parse_target(protocol, target)
    cls = SFTPBackend if protocol == "sftp" else FTPBackend
    backend = cls.connect(host, port, username, password)
    return backend, posixpath.normpath(path or "/")
