"""elevation.py — does THIS feature actually need Administrator?

The launcher ships with `uac_admin=True`, so every session is elevated and every user gets a UAC
prompt at boot. Most of what the app does does not need it: the archive/ROS/XEX edits are ordinary
file writes, and they only need admin when the game files themselves sit somewhere protected
(Program Files, or a folder whose ACL the user can't write).

The one family that can need it is the LIVE-MEMORY features — listing players/goalies out of the
running game, and the diagnostic live writes. And even those need it only conditionally: Windows
lets a process open another process owned by the SAME user at the SAME integrity level, so
attaching to a normally-launched Xenia works fine unelevated. Elevation is required only when
**Xenia itself is elevated** (run-as-admin, or launched by an already-elevated launcher — which is
exactly what happens today, and is self-fulfilling).

This module answers the two questions the UI needs to ask, so a feature can say "this one needs
Admin" instead of the whole app demanding it up front:

    is_admin()            — are we elevated right now?
    process_elevated(pid) — is that process elevated?  (True / False / None = couldn't tell)
    live_memory_status()  — (ok, message) for the live-memory features, right now.

⚠ VERIFICATION STATUS: theory-with-a-documented-mechanism. The integrity rule above is standard
Win32 behaviour, but whether the launcher can be shipped with `uac_admin=False` has NOT been
tested against a running game — that test is: build unelevated, launch Xenia normally, and open
**Teams → Team Rosters (running game)** (ros_live_editor), confirming the roster lists. (It used to
say "the Portraits tab"; Portraits and Goalie Equipment both list from Roster.ROS as of 2026-08-17,
so neither exercises live memory any more — ros_live_editor and the overlay debug lab are the only
features left that do.) Until someone runs it, leave the spec alone and use this module only to
EXPLAIN failures.
"""
from __future__ import annotations
import ctypes
import ctypes.wintypes as wt

try:
    from . import xenia_mem as XM
except ImportError:                                    # bare import (launcher/ is on sys.path)
    import xenia_mem as XM

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20
ERROR_ACCESS_DENIED = 5


def is_admin() -> bool:
    """True if this process is running elevated."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def process_elevated(pid: int):
    """True/False if `pid` is/isn't elevated, or None if it can't be determined.

    ACCESS DENIED opening a process for a query-only handle is itself the answer we care about:
    a same-user, same-integrity process would have opened, so it is above us."""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return True if ctypes.get_last_error() == ERROR_ACCESS_DENIED else None
    tok = wt.HANDLE()
    try:
        if not _adv.OpenProcessToken(wt.HANDLE(h), TOKEN_QUERY, ctypes.byref(tok)):
            return None
        val = wt.DWORD(0)
        ret = wt.DWORD(0)
        ok = _adv.GetTokenInformation(tok, TokenElevation, ctypes.byref(val),
                                      ctypes.sizeof(val), ctypes.byref(ret))
        return bool(val.value) if ok else None
    finally:
        if tok:
            _k32.CloseHandle(tok)
        _k32.CloseHandle(h)


def live_memory_status():
    """(ok, message) for the live-memory features as things stand right now.

    ok=False means the feature cannot work in this session; the message says what to change."""
    pid = XM.find_pid()
    if not pid:
        return False, "Xenia is not running — launch the game first."
    if is_admin():
        return True, ""
    if process_elevated(pid) is True:
        return False, ("Xenia is running as Administrator and this launcher is not, so Windows "
                       "won't let it read the game's memory. Restart the launcher as "
                       "Administrator (right-click → Run as administrator), or start Xenia "
                       "normally.")
    return True, ""


def explain_open_failure(err) -> str:
    """The message to show when OpenProcess on Xenia failed."""
    if is_admin():
        return (f"can't attach to Xenia ({err}). The launcher IS elevated, so this is not a "
                f"permissions problem — check that the process is still alive.")
    return (f"can't attach to Xenia ({err}) — run the launcher as Administrator "
            f"(Xenia is running at a higher integrity level than this launcher).")


# ── which features need it, for per-feature messaging in the UI ──────────────
# "live" = reads/writes another process's memory (conditional: only if Xenia is elevated).
# "files" = ordinary file writes (needs admin only if the game folder itself is protected).
NEEDS_ADMIN = {
    # Portraits lists from the selected Roster.ROS since 2026-08-17 (portrait_assign.list_players
    # takes a ros_path); it only touches live memory when no roster file is set.
    "portraits_list":  "files",
    "goalies_list":    "files",    # same as portraits — goalie_equipment.list_goalies(ros_path)
    "live_assign":     "live",     # set_portrait_key / set_mask (diagnostics)
    "roster_live":     "live",     # ros_live_editor
    "overlay_lab":     "live",
}


def requires_admin_note(feature: str) -> str:
    """A one-line note for a UI control, or "" when the feature never needs elevation."""
    if NEEDS_ADMIN.get(feature) != "live":
        return ""
    return ("Needs the launcher and Xenia at the same permission level — if Xenia is running as "
            "Administrator, run the launcher as Administrator too.")
