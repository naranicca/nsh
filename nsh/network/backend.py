"""Synchronous remote filesystem backends, called from worker threads."""
import base64
import ftplib
import hashlib
import os
import posixpath
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


class HostKeyRequired(Exception):
    """An SSH server presented a host key that is not trusted yet."""

    def __init__(self, hostname, key_type, fingerprint):
        self.hostname = hostname
        self.key_type = key_type
        self.fingerprint = fingerprint
        super().__init__(f"unknown SSH host key for {hostname}: {fingerprint}")


@dataclass
class RemoteEntry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mtime: int = 0
    depth: int = 0
    is_parent: bool = False
    is_symlink: bool = False
    link_target: str = ""
    is_broken: bool = False


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
            if entry.is_dir and not entry.is_symlink:
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
        obj.home = ftp.pwd()
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
    def connect(cls, host, port, username, password, jump=None,
                accept_host_key=None):
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("SFTP requires paramiko (pip install paramiko)") from exc

        config = paramiko.SSHConfig()
        config_path = Path.home() / ".ssh" / "config"
        known_hosts_path = Path.home() / ".ssh" / "known_hosts"
        try:
            with open(config_path, encoding="utf-8") as stream:
                config.parse(stream)
        except OSError:
            pass

        def node(alias, fallback_port=22, fallback_user=""):
            values = config.lookup(alias)
            return {
                "alias": alias,
                "host": values.get("hostname", alias),
                "port": int(values.get("port", fallback_port)),
                "username": values.get("user", fallback_user) or None,
                "keys": [os.path.expanduser(p)
                         for p in values.get("identityfile", [])] or None,
                "host_key_name": values.get("hostkeyalias") or
                                 values.get("hostname", alias),
                "proxyjump": values.get("proxyjump", ""),
                "proxycommand": values.get("proxycommand", "") or "",
            }

        destination = node(host, port, username)
        jump_spec = jump.strip() if jump else destination["proxyjump"].strip()
        jump_nodes = []
        if jump_spec and jump_spec.lower() != "none":
            for spec in jump_spec.split(","):
                jump_host, jump_port, jump_user, _path = parse_target(
                    "sftp", spec.strip())
                jump_nodes.append(node(jump_host, jump_port, jump_user))

        clients = []
        proxy_commands = []
        previous = None

        class ConfirmHostKeyPolicy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                digest = base64.b64encode(
                    hashlib.sha256(key.asbytes()).digest()
                ).decode("ascii").rstrip("=")
                fingerprint = f"SHA256:{digest}"
                if accept_host_key != (hostname, fingerprint):
                    raise HostKeyRequired(
                        hostname, key.get_name(), fingerprint)
                known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
                client.get_host_keys().add(hostname, key.get_name(), key)
                client.save_host_keys(os.fspath(known_hosts_path))
        try:
            for current in [*jump_nodes, destination]:
                sock = None
                if previous is not None:
                    transport = previous.get_transport()
                    if transport is None or not transport.is_active():
                        raise RuntimeError("jump host transport is not active")
                    sock = transport.open_channel(
                        "direct-tcpip", (current["host"], current["port"]),
                        ("127.0.0.1", 0))
                elif (current["proxycommand"] and
                      current["proxycommand"].lower() != "none"):
                    sock = paramiko.ProxyCommand(current["proxycommand"])
                    proxy_commands.append(sock)
                ssh = paramiko.SSHClient()
                clients.append(ssh)
                ssh.load_system_host_keys()
                if known_hosts_path.exists():
                    ssh.load_host_keys(os.fspath(known_hosts_path))
                ssh.set_missing_host_key_policy(ConfirmHostKeyPolicy())
                # With a supplied channel, hostname is used for host-key lookup;
                # network routing itself is handled by direct-tcpip above.
                connect_host = (current["host_key_name"] if sock is not None
                                else current["host"])
                ssh.connect(
                    connect_host, port=current["port"],
                    username=current["username"], password=password or None,
                    key_filename=current["keys"], timeout=15,
                    banner_timeout=15, auth_timeout=15, sock=sock,
                    allow_agent=True, look_for_keys=True)
                previous = ssh

            obj = cls(host, port, destination["username"] or username)
            obj.ssh_clients = clients
            obj.proxy_commands = proxy_commands
            obj.ssh = clients[-1]
            obj.client = obj.ssh.open_sftp()
            try:
                obj.home = obj.client.normalize(".")
            except Exception:
                obj.home = "/"
            return obj
        except Exception:
            for ssh in reversed(clients):
                try:
                    ssh.close()
                except Exception:
                    pass
            for proxy in reversed(proxy_commands):
                try:
                    proxy.close()
                except Exception:
                    pass
            raise

    def close(self):
        try:
            self.client.close()
        finally:
            for ssh in reversed(self.ssh_clients):
                ssh.close()
            for proxy in reversed(getattr(self, "proxy_commands", [])):
                try:
                    proxy.close()
                except Exception:
                    pass

    def listdir(self, path):
        entries = []
        for attr in self.client.listdir_attr(path):
            name = attr.filename
            full = posixpath.join(path, name)
            is_symlink = stat.S_ISLNK(attr.st_mode)
            is_broken = False
            link_target = ""
            target_attr = attr
            if is_symlink:
                try:
                    link_target = self.client.readlink(full)
                    target_attr = self.client.stat(full)
                except (IOError, OSError):
                    is_broken = True
            entries.append(RemoteEntry(
                name, full,
                not is_broken and stat.S_ISDIR(target_attr.st_mode),
                0 if is_broken else (target_attr.st_size or 0),
                int(attr.st_mtime or 0),
                is_symlink=is_symlink, link_target=link_target,
                is_broken=is_broken))
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

    def execute(self, directory, command):
        """Run one command through the authenticated SSH transport."""
        import shlex
        wrapped = f"cd -- {shlex.quote(directory)} && {command}"
        _stdin, stdout, stderr = self.ssh.exec_command(wrapped)
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        return output, error, stdout.channel.recv_exit_status()


def connect(protocol, target, password, jump=None, accept_host_key=None):
    host, port, username, path = parse_target(protocol, target)
    cls = SFTPBackend if protocol == "sftp" else FTPBackend
    if protocol == "sftp":
        backend = cls.connect(host, port, username, password, jump=jump,
                              accept_host_key=accept_host_key)
    else:
        backend = cls.connect(host, port, username, password)
    return backend, posixpath.normpath(path or "/")
