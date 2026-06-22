"""System / process information, gathered without third-party dependencies.

Windows uses ctypes (memory, overall CPU) and PowerShell ``Get-Process`` (the
process list, with per-process CPU% derived by sampling). Linux reads ``/proc``;
macOS falls back to ``ps`` / ``sysctl``. Everything degrades gracefully — a
field that can't be read comes back as ``None`` and the UI shows ``n/a``.
"""
import csv
import io
import os
import signal
import subprocess
import sys
import time
from collections import namedtuple

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

Proc = namedtuple("Proc", "pid name cmd cpu mem rss")  # cpu/mem are percentages
# name is the short process name (used to sort / in the kill prompt); cmd is the
# full command line with arguments (shown in the list), falling back to name.
Snapshot = namedtuple("Snapshot", "cpu mem_used mem_total disk_used disk_total procs")

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW, so PowerShell never flashes a console


def _run(cmd, timeout=10):
    try:
        kw = {}
        if IS_WIN:
            kw["creationflags"] = _NO_WINDOW
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              **kw).stdout
    except Exception:  # noqa: BLE001 - any failure -> empty, caller copes
        return ""


# -- disk (cross-platform via shutil) ----------------------------------------
def _disk_usage():
    import shutil
    path = "C:\\" if IS_WIN else "/"
    try:
        u = shutil.disk_usage(path)
        return u.used, u.total
    except Exception:  # noqa: BLE001
        return None, None


# -- memory ------------------------------------------------------------------
def _memory():
    """(used_bytes, total_bytes) or (None, None)."""
    if IS_WIN:
        return _win_memory()
    if IS_MAC:
        return _mac_memory()
    return _linux_memory()


def _win_memory():
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    try:
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys - m.ullAvailPhys, m.ullTotalPhys
    except Exception:  # noqa: BLE001
        return None, None


def _linux_memory():
    try:
        info = {}
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key.strip()] = int(rest.strip().split()[0]) * 1024  # kB -> B
        total = info.get("MemTotal")
        avail = info.get("MemAvailable", info.get("MemFree"))
        if total and avail is not None:
            return total - avail, total
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _mac_memory():
    try:
        total = int(_run(["sysctl", "-n", "hw.memsize"]).strip() or 0) or None
        # best-effort used: total minus free+inactive pages from vm_stat
        used = None
        vm = _run(["vm_stat"])
        if vm and total:
            page = 4096
            free = inactive = 0
            for line in vm.splitlines():
                if "page size of" in line:
                    digits = "".join(c for c in line if c.isdigit())
                    page = int(digits) if digits else page
                elif line.startswith("Pages free:"):
                    free = int(line.split(":")[1].strip().rstrip("."))
                elif line.startswith("Pages inactive:"):
                    inactive = int(line.split(":")[1].strip().rstrip("."))
            used = total - (free + inactive) * page
        return used, total
    except Exception:  # noqa: BLE001
        return None, None


# -- overall CPU -------------------------------------------------------------
class _CpuMeter:
    """Overall CPU usage %, by sampling the difference between two reads."""

    def __init__(self):
        self._prev = None

    def percent(self):
        if IS_WIN:
            return self._win()
        if IS_MAC:
            return self._mac()
        return self._linux()

    def _win(self):
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        def as_int(ft):
            return (ft.high << 32) | ft.low
        try:
            idle, kern, user = FILETIME(), FILETIME(), FILETIME()
            ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
            # kernel time includes idle; total busy = (kernel + user) - idle
            cur = (as_int(idle), as_int(kern) + as_int(user))
        except Exception:  # noqa: BLE001
            return None
        prev, self._prev = self._prev, cur
        if not prev:
            return None
        d_idle = cur[0] - prev[0]
        d_total = cur[1] - prev[1]
        if d_total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (d_total - d_idle) / d_total))

    def _linux(self):
        try:
            with open("/proc/stat", encoding="utf-8") as fh:
                parts = fh.readline().split()
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals)
        except Exception:  # noqa: BLE001
            return None
        prev, self._prev = self._prev, (idle, total)
        if not prev:
            return None
        d_idle, d_total = idle - prev[0], total - prev[1]
        if d_total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (d_total - d_idle) / d_total))

    def _mac(self):
        # rough: average per-core %cpu summed from ps, normalised by core count
        out = _run(["ps", "-A", "-o", "%cpu="])
        if not out:
            return None
        total = 0.0
        for line in out.splitlines():
            try:
                total += float(line.strip())
            except ValueError:
                pass
        return max(0.0, min(100.0, total / (os.cpu_count() or 1)))


# -- processes ---------------------------------------------------------------
class Monitor:
    """Stateful sampler: keeps the previous per-process CPU read (Windows) and
    the previous overall-CPU read so successive snapshots show live usage."""

    def __init__(self):
        self.ncpu = os.cpu_count() or 1
        self._cpu_meter = _CpuMeter()
        self._prev_proc = {}     # pid -> cpu_seconds (Windows)
        self._prev_proc_t = None
        self._mem_total = None

    def snapshot(self):
        used, total = _memory()
        self._mem_total = total or self._mem_total
        procs = self._processes(total)
        disk_used, disk_total = _disk_usage()
        return Snapshot(self._cpu_meter.percent(), used, total,
                        disk_used, disk_total, procs)

    def _processes(self, mem_total):
        if IS_WIN:
            return self._win_processes(mem_total)
        return self._posix_processes()

    def _win_processes(self, mem_total):
        # Get-Process gives CPU seconds + working set; the full command line
        # lives on Win32_Process, so build a PID->CommandLine map and graft it
        # on. CommandLine is null for processes we can't open (system / other
        # users) -> the row's Cmd comes back empty and we fall back to the name.
        out = _run([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "$c=@{}; Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |"
            " ForEach-Object { $c[[int]$_.ProcessId]=$_.CommandLine };"
            " Get-Process | Select-Object Id,ProcessName,CPU,WorkingSet64,"
            "@{N='Cmd';E={$c[[int]$_.Id]}} | ConvertTo-Csv -NoTypeInformation",
        ])
        now = time.monotonic()
        elapsed = (now - self._prev_proc_t) if self._prev_proc_t else None
        self._prev_proc_t = now
        cur_cpu, procs = {}, []
        for row in csv.DictReader(io.StringIO(out)):
            try:
                pid = int(row["Id"])
            except (TypeError, ValueError):
                continue
            name = row.get("ProcessName") or "?"
            try:
                cpu_sec = float(row["CPU"]) if row.get("CPU") else 0.0
            except ValueError:
                cpu_sec = 0.0
            try:
                rss = int(row["WorkingSet64"]) if row.get("WorkingSet64") else 0
            except ValueError:
                rss = 0
            cur_cpu[pid] = cpu_sec
            # CPU% = busy seconds since the last sample, over the wall time and
            # all cores (so it reads like Task Manager's % of total)
            cpu_pct = 0.0
            if elapsed and elapsed > 0 and pid in self._prev_proc:
                delta = cpu_sec - self._prev_proc[pid]
                cpu_pct = max(0.0, min(100.0, 100.0 * delta / (elapsed * self.ncpu)))
            mem_pct = (100.0 * rss / mem_total) if mem_total else 0.0
            cmd = (row.get("Cmd") or "").strip() or name
            procs.append(Proc(pid, name, cmd, cpu_pct, mem_pct, rss))
        self._prev_proc = cur_cpu
        return procs

    def _posix_processes(self):
        # ps gives %cpu, %mem and rss (KiB) directly — no sampling needed.
        # args is the full command line; the short name is the basename of its
        # first token (argv[0]).
        out = _run(["ps", "-eo", "pid=,pcpu=,pmem=,rss=,args="])
        procs = []
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5:
                continue
            try:
                pid = int(parts[0])
                cpu = float(parts[1])
                mem = float(parts[2])
                rss = int(parts[3]) * 1024
            except ValueError:
                continue
            cmd = parts[4].strip()
            argv0 = cmd.split()[0] if cmd else ""
            name = os.path.basename(argv0) or "?"
            procs.append(Proc(pid, name, cmd or name, cpu, mem, rss))
        return procs


def kill(pid, force=False):
    """Terminate ``pid``. Returns ``(ok, error_message)``."""
    try:
        if IS_WIN:
            cmd = ["taskkill", "/PID", str(pid)]
            if force:
                cmd.append("/F")
            r = subprocess.run(cmd, capture_output=True, text=True,
                               creationflags=_NO_WINDOW)
            if r.returncode != 0:
                return False, (r.stderr or r.stdout or "taskkill failed").strip()
            return True, ""
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
