#!/usr/bin/env python3
"""Per-worker memory accounting and limits for workbench workers."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import mmap
import os
from pathlib import Path
import sys


GB_BYTES = 1024 ** 3
QUEUE_JOB_ENV = "LOOSE_ENDS_QUEUE_JOB"
CGROUP_ROOT_ENV = "LOOSE_ENDS_CGROUP_ROOT"
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_ASSIGN_PROCESS = 0x0001
JOB_OBJECT_SET_ATTRIBUTES = 0x0002
JOB_OBJECT_QUERY = 0x0004
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_MORE_DATA = 234
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WORKER_JOB_HANDLE = None


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _windows_installed_memory_bytes() -> int | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetPhysicallyInstalledSystemMemory
    function.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
    function.restype = wintypes.BOOL
    installed_kb = ctypes.c_ulonglong()
    if function(ctypes.byref(installed_kb)):
        return int(installed_kb.value * 1024)
    return None


def _windows_usable_memory_bytes() -> int | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GlobalMemoryStatusEx
    function.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
    function.restype = wintypes.BOOL
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if function(ctypes.byref(status)):
        return int(status.ullTotalPhys)
    return None


def physical_memory_bytes() -> int | None:
    """Return installed physical memory without adding a dependency."""
    if os.name == "nt":
        return (
            _windows_installed_memory_bytes()
            or _windows_usable_memory_bytes()
        )
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None
    return int(page_size * page_count)


def resolved_limit_bytes(settings: dict, physical_bytes: int | None) -> int | None:
    """Resolve a persisted percent/GB/unlimited policy to total allocation."""
    memory = settings.get("memoryLimit", {"mode": "percent", "value": 50})
    mode = memory["mode"]
    if mode == "unlimited":
        return None
    value = float(memory["value"])
    if mode == "gb":
        return max(1, round(value * GB_BYTES))
    if mode == "percent" and physical_bytes is not None:
        return max(1, round(physical_bytes * value / 100.0))
    return None


def per_worker_limit_bytes(
    settings: dict,
    physical_bytes: int | None,
) -> int | None:
    """Divide the configured total allocation across maximum worker slots."""
    allocation = resolved_limit_bytes(settings, physical_bytes)
    if allocation is None:
        return None
    worker_limit = max(1, int(settings.get("workerLimit", 1)))
    return max(1, allocation // worker_limit)


def worker_job_name(database: Path, run_id: str) -> str:
    digest = _database_digest(database)
    run_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    return f"Local\\LooseEndsWorker-{digest}-{run_digest}"


def _database_digest(database: Path) -> str:
    identity = str(database.resolve())
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _linux_cgroup_parent() -> Path:
    mount = Path("/sys/fs/cgroup")
    if not (mount / "cgroup.controllers").is_file():
        raise OSError("cgroup v2 is not mounted at /sys/fs/cgroup")
    override = os.environ.get(CGROUP_ROOT_ENV)
    if override:
        parent = Path(override).resolve()
    else:
        try:
            lines = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
        except OSError as exc:
            raise OSError(f"could not read /proc/self/cgroup: {exc}") from exc
        relative = next(
            (line[3:] for line in lines if line.startswith("0::")),
            None,
        )
        if relative is None:
            raise OSError("this process is not in a cgroup v2 hierarchy")
        parent = (mount / relative.lstrip("/")).resolve()
    try:
        parent.relative_to(mount.resolve())
    except ValueError as exc:
        raise OSError(f"cgroup root must be beneath {mount}") from exc
    if not (parent / "cgroup.controllers").is_file():
        raise OSError(f"{parent} is not a cgroup v2 directory")
    return parent


def _linux_queue_cgroup(database: Path, parent: Path) -> Path:
    return parent / f"loose-ends-workers-{_database_digest(database)}"


def _linux_worker_cgroup(database: Path, parent: Path, run_id: str) -> Path:
    run_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    return _linux_queue_cgroup(database, parent) / f"worker-{run_digest}"


def _raise_last_error(operation: str) -> None:
    error = ctypes.get_last_error()
    raise OSError(error, f"{operation} failed: {ctypes.FormatError(error).strip()}")


def _configure_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenJobObjectW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    return kernel32


def _configure_psapi():
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    return psapi


class QueueMemoryController:
    """Own one independently limited OS resource container per worker run."""

    def __init__(self, database: Path):
        self.database = database
        self.physical_bytes = physical_memory_bytes()
        self.available = False
        self.error: str | None = None
        self.backend: str | None = None
        self._containers: dict[str, object] = {}
        self._observed_peak = 0
        if sys.platform.startswith("linux"):
            try:
                self._initialize_linux()
            except OSError as exc:
                self.error = (
                    "Linux cgroup v2 memory enforcement is unavailable: "
                    f"{exc}. Run the workbench in a delegated cgroup with the "
                    "memory controller enabled."
                )
            return
        if os.name != "nt":
            self.error = (
                "worker memory enforcement requires Windows Job Objects or "
                "a delegated Linux cgroup v2"
            )
            return
        try:
            self._kernel32 = _configure_kernel32()
            self._psapi = _configure_psapi()
            self.backend = "windows_job"
            self.available = True
        except OSError as exc:
            self.error = str(exc)

    @staticmethod
    def _enable_linux_memory_children(cgroup: Path) -> None:
        controllers = (cgroup / "cgroup.controllers").read_text(
            encoding="ascii"
        ).split()
        subtree_control = (cgroup / "cgroup.subtree_control").read_text(
            encoding="ascii"
        ).split()
        if "memory" in subtree_control:
            return
        if "memory" not in controllers:
            raise OSError(f"the memory controller is not available in {cgroup}")
        try:
            (cgroup / "cgroup.subtree_control").write_text(
                "+memory\n", encoding="ascii"
            )
        except OSError as exc:
            raise OSError(
                f"could not enable the memory controller in {cgroup}: {exc}"
            ) from exc

    def _initialize_linux(self) -> None:
        parent = _linux_cgroup_parent()
        self._enable_linux_memory_children(parent)
        manager = _linux_queue_cgroup(self.database, parent)
        try:
            manager.mkdir(exist_ok=True)
        except OSError as exc:
            raise OSError(
                f"could not create worker cgroup manager {manager}: {exc}"
            ) from exc
        self._enable_linux_memory_children(manager)
        self._cgroup_parent = parent
        self._cgroup = manager
        for child in manager.iterdir():
            if not child.name.startswith("worker-") or not child.is_dir():
                continue
            try:
                if not (child / "cgroup.procs").read_text(
                    encoding="ascii"
                ).split():
                    child.rmdir()
            except OSError:
                continue
        self.backend = "linux_cgroup_v2"
        self.available = True

    def close(self) -> None:
        if self.backend == "windows_job":
            for handle in self._containers.values():
                self._kernel32.CloseHandle(handle)
        self._containers.clear()

    def _windows_container_name(self, run_id: str) -> str:
        return worker_job_name(self.database, run_id)

    def _linux_container_path(self, run_id: str) -> Path:
        return _linux_worker_cgroup(
            self.database,
            self._cgroup_parent,
            run_id,
        )

    def _open_existing_container(self, run_id: str):
        if self.backend == "linux_cgroup_v2":
            cgroup = self._linux_container_path(run_id)
            return cgroup if cgroup.is_dir() else None
        handle = self._kernel32.OpenJobObjectW(
            JOB_OBJECT_ASSIGN_PROCESS
            | JOB_OBJECT_SET_ATTRIBUTES
            | JOB_OBJECT_QUERY,
            False,
            self._windows_container_name(run_id),
        )
        return handle or None

    def _create_container(self, run_id: str):
        if self.backend == "linux_cgroup_v2":
            cgroup = self._linux_container_path(run_id)
            try:
                cgroup.mkdir(exist_ok=True)
            except OSError as exc:
                raise OSError(f"could not create worker cgroup {cgroup}: {exc}") from exc
            required = [
                "cgroup.procs",
                "memory.current",
                "memory.max",
                "memory.swap.current",
                "memory.swap.max",
            ]
            missing = [name for name in required if not (cgroup / name).is_file()]
            if missing:
                raise OSError(
                    f"worker cgroup is missing required files: {', '.join(missing)}"
                )
            return cgroup
        handle = self._kernel32.CreateJobObjectW(
            None,
            self._windows_container_name(run_id),
        )
        if not handle:
            _raise_last_error("CreateJobObjectW")
        return handle

    def _container_identifier(self, run_id: str, container) -> str:
        if self.backend == "linux_cgroup_v2":
            return str(container)
        return self._windows_container_name(run_id)

    def _windows_limits(self, handle) -> _JOBOBJECT_EXTENDED_LIMIT_INFORMATION:
        information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not self._kernel32.QueryInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            _raise_last_error("QueryInformationJobObject")
        return information

    def _container_limit(self, container) -> int | None:
        if self.backend == "linux_cgroup_v2":
            value = (container / "memory.max").read_text(
                encoding="ascii"
            ).strip()
            return None if value == "max" else int(value)
        information = self._windows_limits(container)
        if information.BasicLimitInformation.LimitFlags & JOB_OBJECT_LIMIT_JOB_MEMORY:
            return int(information.JobMemoryLimit)
        return None

    def _set_container_limit(self, container, limit_bytes: int | None) -> None:
        if self.backend == "linux_cgroup_v2":
            value = "max" if limit_bytes is None else str(limit_bytes)
            swap_value = "max" if limit_bytes is None else "0"
            (container / "memory.swap.max").write_text(
                swap_value + "\n", encoding="ascii"
            )
            (container / "memory.max").write_text(
                value + "\n", encoding="ascii"
            )
            return
        information = self._windows_limits(container)
        if limit_bytes is None:
            information.BasicLimitInformation.LimitFlags &= ~JOB_OBJECT_LIMIT_JOB_MEMORY
            information.JobMemoryLimit = 0
        else:
            information.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_JOB_MEMORY
            information.JobMemoryLimit = limit_bytes
        if not self._kernel32.SetInformationJobObject(
            container,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            _raise_last_error("SetInformationJobObject")

    def _windows_process_ids(self, handle) -> list[int]:
        capacity = 32
        pointer_size = ctypes.sizeof(ctypes.c_size_t)
        while True:
            size = 2 * ctypes.sizeof(wintypes.DWORD) + capacity * pointer_size
            buffer = ctypes.create_string_buffer(size)
            if self._kernel32.QueryInformationJobObject(
                handle,
                JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                buffer,
                size,
                None,
            ):
                count = wintypes.DWORD.from_buffer(buffer, 4).value
                array_type = ctypes.c_size_t * count
                return list(array_type.from_buffer(buffer, 8))
            error = ctypes.get_last_error()
            if error != ERROR_MORE_DATA:
                _raise_last_error("QueryInformationJobObject")
            assigned = wintypes.DWORD.from_buffer(buffer, 0).value
            capacity = max(capacity * 2, int(assigned) + 8)

    def _container_process_ids(self, container) -> list[int]:
        if self.backend == "linux_cgroup_v2":
            return [
                int(value)
                for value in (container / "cgroup.procs").read_text(
                    encoding="ascii"
                ).split()
            ]
        return self._windows_process_ids(container)

    def _container_current_bytes(self, container) -> int:
        if self.backend == "linux_cgroup_v2":
            memory = int(
                (container / "memory.current").read_text(
                    encoding="ascii"
                ).strip()
            )
            swap = int(
                (container / "memory.swap.current").read_text(
                    encoding="ascii"
                ).strip()
            )
            return memory + swap
        total = 0
        for pid in self._windows_process_ids(container):
            process = self._kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_QUERY_INFORMATION,
                False,
                pid,
            )
            if not process:
                continue
            try:
                counters = _PROCESS_MEMORY_COUNTERS_EX()
                counters.cb = ctypes.sizeof(counters)
                if self._psapi.GetProcessMemoryInfo(
                    process, ctypes.byref(counters), ctypes.sizeof(counters)
                ):
                    total += int(counters.PrivateUsage)
            finally:
                self._kernel32.CloseHandle(process)
        return total

    def run_has_processes(self, run_id: str) -> bool | None:
        """Return whether a run's OS container is nonempty, if knowable."""
        if not self.available:
            return None
        try:
            container = self._containers.get(run_id)
            if container is None:
                container = self._open_existing_container(run_id)
                if container is None:
                    return False
                self._containers[run_id] = container
            alive = bool(self._container_process_ids(container))
            self.error = None
            return alive
        except OSError as exc:
            self.error = str(exc)
            return None

    def _container_limit_matches(
        self,
        container,
        desired: int | None,
        applied: int | None,
    ) -> bool:
        if self.backend != "linux_cgroup_v2":
            return desired == applied
        swap = (container / "memory.swap.max").read_text(
            encoding="ascii"
        ).strip()
        expected_swap = "max" if desired is None else "0"
        return desired == applied and swap == expected_swap

    def _normalize_limit(self, desired: int | None) -> int | None:
        if desired is None or self.backend != "linux_cgroup_v2":
            return desired
        page_size = mmap.PAGESIZE
        return ((desired + page_size - 1) // page_size) * page_size

    def _retire_finished_containers(self, active_run_ids: set[str]) -> None:
        for run_id, container in list(self._containers.items()):
            if run_id in active_run_ids or self._container_process_ids(container):
                continue
            if self.backend == "windows_job":
                self._kernel32.CloseHandle(container)
            else:
                try:
                    container.rmdir()
                except OSError:
                    continue
            del self._containers[run_id]

    def prepare_run(self, run_id: str, settings: dict) -> str | None:
        """Create and configure a worker's container before it starts."""
        desired = self._normalize_limit(
            per_worker_limit_bytes(settings, self.physical_bytes)
        )
        if not self.available:
            if desired is None:
                return None
            raise OSError(self.error or "worker memory enforcement is unavailable")
        try:
            container = self._containers.get(run_id)
            if container is None:
                container = self._open_existing_container(run_id)
            if container is None:
                container = self._create_container(run_id)
            self._containers[run_id] = container
            if not self._container_limit_matches(
                container,
                desired,
                self._container_limit(container),
            ):
                self._set_container_limit(container, desired)
            self.error = None
            return self._container_identifier(run_id, container)
        except OSError as exc:
            self.error = str(exc)
            self.available = False
            raise

    def reconcile(
        self,
        settings: dict,
        active_run_ids: set[str] | None = None,
    ) -> dict:
        active_run_ids = set(active_run_ids or ())
        allocation = resolved_limit_bytes(settings, self.physical_bytes)
        desired = self._normalize_limit(
            per_worker_limit_bytes(settings, self.physical_bytes)
        )
        snapshot = {
            "available": self.available,
            "backend": self.backend,
            "physicalBytes": self.physical_bytes,
            "allocationBytes": allocation,
            "resolvedBytes": desired,
            "appliedBytes": None,
            "currentBytes": None,
            "peakBytes": self._observed_peak or None,
            "managedWorkers": len(self._containers),
            "pending": False,
            "error": self.error,
        }
        if not self.available:
            if (os.name == "nt" or sys.platform.startswith("linux")) and desired is not None:
                snapshot["pending"] = True
            return snapshot
        try:
            for run_id in active_run_ids:
                if run_id in self._containers:
                    continue
                container = self._open_existing_container(run_id)
                if container is not None:
                    self._containers[run_id] = container
            self._retire_finished_containers(active_run_ids)

            total_current = 0
            pending = False
            applied_values: list[int] = []
            for container in self._containers.values():
                current = self._container_current_bytes(container)
                total_current += current
                applied = self._container_limit(container)
                matches = self._container_limit_matches(container, desired, applied)
                if desired is None:
                    if not matches:
                        self._set_container_limit(container, None)
                    applied = None
                elif applied is not None and desired > applied:
                    self._set_container_limit(container, desired)
                    applied = desired
                elif desired < current and (applied is None or desired < applied):
                    pending = True
                elif not matches:
                    self._set_container_limit(container, desired)
                    applied = desired
                if applied is not None:
                    applied_values.append(applied)

            self._observed_peak = max(self._observed_peak, total_current)
            self.error = None
            snapshot.update(
                appliedBytes=(max(applied_values) if applied_values else desired),
                currentBytes=total_current,
                peakBytes=self._observed_peak or None,
                managedWorkers=len(self._containers),
                pending=pending,
                error=None,
            )
        except OSError as exc:
            self.error = str(exc)
            snapshot["error"] = self.error
            snapshot["pending"] = desired is not None
        return snapshot


def join_queue_job_from_environment() -> None:
    """Join the configured worker container before launching child processes."""
    global _WORKER_JOB_HANDLE
    name = os.environ.get(QUEUE_JOB_ENV)
    if not name:
        return
    if sys.platform.startswith("linux"):
        cgroup = Path(name)
        procs = cgroup / "cgroup.procs"
        if not procs.is_file():
            raise OSError(f"worker cgroup is unavailable: {cgroup}")
        procs.write_text(f"{os.getpid()}\n", encoding="ascii")
        return
    if os.name != "nt":
        return
    kernel32 = _configure_kernel32()
    handle = kernel32.OpenJobObjectW(
        JOB_OBJECT_ASSIGN_PROCESS | JOB_OBJECT_QUERY,
        False,
        name,
    )
    if not handle:
        _raise_last_error("OpenJobObjectW")
    try:
        if not kernel32.AssignProcessToJobObject(
            handle, kernel32.GetCurrentProcess()
        ):
            _raise_last_error("AssignProcessToJobObject")
        # Keep the named object reopenable if the workbench server restarts.
        # Windows closes this handle automatically when the worker exits.
        _WORKER_JOB_HANDLE = handle
        handle = None
    finally:
        if handle:
            kernel32.CloseHandle(handle)
