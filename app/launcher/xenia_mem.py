"""Win32 process memory access for Xenia emulator (64-bit)."""
import ctypes
import ctypes.wintypes as wt
from typing import Generator, Optional, Tuple

_k32 = ctypes.WinDLL('kernel32', use_last_error=True)

PROCESS_VM_READ           = 0x0010
PROCESS_VM_WRITE          = 0x0020
PROCESS_VM_OPERATION      = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS        = 0x00000002
MEM_COMMIT                = 0x1000
PAGE_GUARD                = 0x100
PAGE_NOACCESS             = 0x001

XENIA_NAMES  = frozenset(['xenia.exe', 'xenia_canary.exe', 'xenia_canary_netplay.exe'])
PHYS_BASE    = 0x1A0000000   # Xenia host VA for Xbox 360 physical address 0x00000000
PHYS_SIZE    = 0x20000000    # 512 MB physical RAM window


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ('dwSize',              wt.DWORD),
        ('cntUsage',            wt.DWORD),
        ('th32ProcessID',       wt.DWORD),
        ('th32DefaultHeapID',   ctypes.POINTER(ctypes.c_ulong)),
        ('th32ModuleID',        wt.DWORD),
        ('cntThreads',          wt.DWORD),
        ('th32ParentProcessID', wt.DWORD),
        ('pcPriClassBase',      ctypes.c_long),
        ('dwFlags',             wt.DWORD),
        ('szExeFile',           ctypes.c_char * 260),
    ]


class _MBI(ctypes.Structure):
    """MEMORY_BASIC_INFORMATION (64-bit layout)."""
    _fields_ = [
        ('BaseAddress',       ctypes.c_ulonglong),
        ('AllocationBase',    ctypes.c_ulonglong),
        ('AllocationProtect', wt.DWORD),
        ('_pad1',             wt.DWORD),
        ('RegionSize',        ctypes.c_ulonglong),
        ('State',             wt.DWORD),
        ('Protect',           wt.DWORD),
        ('Type',              wt.DWORD),
        ('_pad2',             wt.DWORD),
    ]


_k32.OpenProcess.restype  = wt.HANDLE
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]

_k32.ReadProcessMemory.restype  = wt.BOOL
_k32.ReadProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]

_k32.WriteProcessMemory.restype  = wt.BOOL
_k32.WriteProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]

_k32.VirtualQueryEx.restype  = ctypes.c_size_t
_k32.VirtualQueryEx.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(_MBI), ctypes.c_size_t,
]

_k32.VirtualProtectEx.restype  = wt.BOOL
_k32.VirtualProtectEx.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD,
    ctypes.POINTER(wt.DWORD),
]

PAGE_READWRITE           = 0x04
PAGE_EXECUTE_READWRITE   = 0x40


def find_pid(names=None) -> Optional[int]:
    """Return PID of the first matching process (case-insensitive), or None."""
    targets = frozenset(n.lower() for n in (names or XENIA_NAMES))
    snap    = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == ctypes.c_void_p(-1).value:
        return None
    entry = _PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
    try:
        if not _k32.Process32First(snap, ctypes.byref(entry)):
            return None
        while True:
            if entry.szExeFile.decode(errors='replace').lower() in targets:
                return entry.th32ProcessID
            if not _k32.Process32Next(snap, ctypes.byref(entry)):
                return None
    finally:
        _k32.CloseHandle(snap)


def open_process(pid: int) -> int:
    """Open Xenia process with VM read/write access. Raises OSError on failure."""
    flags  = PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION
    handle = _k32.OpenProcess(flags, False, pid)
    if not handle:
        raise OSError(f'OpenProcess failed (pid={pid}): Win32 error {ctypes.get_last_error()}')
    return handle


def close_handle(handle: int) -> None:
    _k32.CloseHandle(handle)


def read_bytes(handle: int, address: int, size: int) -> Optional[bytes]:
    """Read size bytes from address in process. Returns None on any failure."""
    buf  = (ctypes.c_char * size)()
    read = ctypes.c_size_t(0)
    ok   = _k32.ReadProcessMemory(
        handle, ctypes.c_void_p(address), buf, size, ctypes.byref(read))
    if not ok or read.value != size:
        return None
    return bytes(buf)


def write_bytes(handle: int, address: int, data: bytes) -> bool:
    """Write data to address in process.

    Xenia maps guest RAM across multiple VirtualAlloc regions, so a single
    VirtualProtectEx call spanning a region boundary fails (ERROR_INVALID_PARAMETER).
    We unlock page-by-page (4 KB each) to stay within one region per call.
    """
    size = len(data)
    buf  = (ctypes.c_char * size)(*data)
    written = ctypes.c_size_t(0)

    # Fast path — direct write (succeeds when pages are already READWRITE)
    ok = _k32.WriteProcessMemory(
        handle, ctypes.c_void_p(address), buf, size, ctypes.byref(written))
    if ok and written.value == size:
        return True

    # Slow path — unlock each 4 KB page individually, then write, then restore.
    PAGE_READWRITE = 0x04
    start_page = address & ~0xFFF
    end_page   = (address + size + 0xFFF) & ~0xFFF
    restored: list = []

    for p in range(start_page, end_page, 0x1000):
        old = wt.DWORD(0)
        if _k32.VirtualProtectEx(handle, ctypes.c_void_p(p), 0x1000,
                                  PAGE_READWRITE, ctypes.byref(old)):
            restored.append((p, old.value or PAGE_READWRITE))

    if restored:
        written = ctypes.c_size_t(0)
        ok = _k32.WriteProcessMemory(
            handle, ctypes.c_void_p(address), buf, size, ctypes.byref(written))
        dummy = wt.DWORD(0)
        for p, old in restored:
            _k32.VirtualProtectEx(handle, ctypes.c_void_p(p), 0x1000, old,
                                   ctypes.byref(dummy))
        return bool(ok) and written.value == size

    return False


def enum_committed_regions(
    handle: int, start: int, length: int
) -> Generator[Tuple[int, int], None, None]:
    """Yield (base_addr, size) for each committed readable region in [start, start+length)."""
    addr     = start
    end      = start + length
    mbi      = _MBI()
    mbi_size = ctypes.sizeof(_MBI)
    while addr < end:
        ret = _k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), mbi_size)
        if not ret:
            addr += 0x1000
            continue
        next_addr = int(mbi.BaseAddress) + int(mbi.RegionSize)
        if (mbi.State == MEM_COMMIT
                and (mbi.Protect & 0xFF) != PAGE_NOACCESS
                and not (mbi.Protect & PAGE_GUARD)):
            yield int(mbi.BaseAddress), int(mbi.RegionSize)
        addr = next_addr if next_addr > addr else addr + 0x1000


def find_phys_base(handle: int) -> Optional[int]:
    """Auto-detect the host VA of Xbox 360 guest physical RAM inside Xenia.

    Xenia reserves at least 512 MB (0x20000000) for guest physical RAM.
    We scan Xenia's address space for large committed regions and return
    the lowest one that is actually readable, sorted with the configured
    known bases tried first for speed.
    """
    # Fast path: try the four addresses Xenia commonly uses
    KNOWN = [0x1A0000000, 0x100000000, 0x200000000, 0x1C0000000,
             0x1E0000000, 0x140000000, 0x180000000]
    for base in KNOWN:
        probe = read_bytes(handle, base, 4096)
        if probe is not None:
            return base

    # Slow path: enumerate Xenia's address space for large committed regions
    candidates: list = []
    addr     = 0x80000000       # start above 2 GB (below is usually Xenia's own code)
    end      = 0x10000000000    # scan up to 1 TB
    mbi      = _MBI()
    mbi_size = ctypes.sizeof(_MBI)
    while addr < end:
        ret = _k32.VirtualQueryEx(handle, ctypes.c_void_p(addr),
                                   ctypes.byref(mbi), mbi_size)
        if not ret:
            addr += 0x10000
            continue
        base      = int(mbi.BaseAddress)
        reg_size  = int(mbi.RegionSize)
        next_addr = base + reg_size
        if (mbi.State == MEM_COMMIT
                and reg_size >= 0x10000000          # at least 256 MB
                and (mbi.Protect & 0xFF) != PAGE_NOACCESS):
            candidates.append(base)
        addr = next_addr if next_addr > addr else addr + 0x10000

    candidates.sort()
    for base in candidates:
        probe = read_bytes(handle, base, 4096)
        if probe is not None:
            return base

    return None


def diagnose(handle: int, phys_base: int) -> str:
    """Return a diagnostic string about Xenia's memory layout."""
    lines = [f"Configured phys_base = 0x{phys_base:X}"]

    # Sample 16 offsets spread across 512 MB, then do a finer pass around live areas
    COARSE = [i * 0x02000000 for i in range(16)]   # every 32 MB
    live_ranges: list = []
    non_trivial = 0
    for off in COARSE:
        addr = phys_base + off
        data = read_bytes(handle, addr, 4096)
        if data is None:
            continue
        unique = len(set(data))
        if unique >= 20:
            non_trivial += 1
            live_ranges.append(off)

    if non_trivial == 0:
        lines.append("  *** ALL COARSE SAMPLES TRIVIAL — no game data visible ***")
        lines.append("  → Is NHL 2K10 past the title screen and actively rendering?")
        lines.append("  → Navigate to the main menu or gameplay, then re-diagnose.")
    else:
        lines.append(f"  Coarse survey: {non_trivial}/16 regions have live data")
        # Fine-grained pass around each live region (±32 MB, 4 MB steps)
        fine_live: list = []
        checked = set()
        for base_off in live_ranges:
            for delta in range(-0x02000000, 0x02000001, 0x00400000):
                off = base_off + delta
                if off < 0 or off >= PHYS_SIZE or off in checked:
                    continue
                checked.add(off)
                data = read_bytes(handle, phys_base + off, 4096)
                if data and len(set(data)) >= 20:
                    fine_live.append(off)
        fine_live.sort()
        if fine_live:
            # Collapse into ranges
            ranges_str = []
            start = fine_live[0]
            prev  = fine_live[0]
            for off in fine_live[1:]:
                if off - prev > 0x00800000:   # gap > 8 MB → new range
                    ranges_str.append(f"0x{start:08X}–0x{prev + 0x400000:08X}")
                    start = off
                prev = off
            ranges_str.append(f"0x{start:08X}–0x{prev + 0x400000:08X}")
            lines.append(f"  Live data ranges: {', '.join(ranges_str)}")

    # Auto-detect
    detected = find_phys_base(handle)
    if detected is not None:
        lines.append(f"  Auto-detected base = 0x{detected:X}"
                     + ("  ✓ matches config" if detected == phys_base
                        else "  ← DIFFERENT from config — update Settings!"))
    else:
        lines.append("  Auto-detect found nothing readable")

    # Count committed regions in the configured range
    regions = list(enum_committed_regions(handle, phys_base, PHYS_SIZE))
    total   = sum(s for _, s in regions)
    lines.append(f"  Committed regions in range: {len(regions)}"
                 f"  ({total // 1024 // 1024} MB total)")

    return "\n".join(lines)
