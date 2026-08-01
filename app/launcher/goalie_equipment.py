"""goalie_equipment.py — READING goalie masks out of the running game.

The game reads a goalie's mask from the loaded player struct as
    shell   = (*(u32*)(player+0xB4) >> 23) & 0xF      (Goalie_GetMaskModelIndex   @0x840a8888)
    pattern =  *(u32*)(player+0xB8)        & 0x1F      (Goalie_GetMaskPatternIndex @0x840a9010)

This module is now the LISTING half of the Goalie tab, not the writing half. Player NAMES only
resolve in the running game (the record holds a pointer into a name pool; nothing in the save file
spells the name inline), so the tab still enumerates live — but the assignment is written into
**Roster.ROS** via player_assign.py, because the on-disk record is the same struct at the same
offsets. A live memory patch dies with the Xenia process and can never reach a console; a file edit
travels with the game files. set_mask()/apply_masks() below are kept for diagnostics only.

Verified live (xenia_canary):
  g_RosterManager global @ guest VA 0x849DE29C -> manager base pointer.
  manager chunk table @ manager+0x08, entries {hash u32, count u32, ptr u32}.
  PLAYER chunk hash 0x1E159C31 -> count (~2715), ptr = player-record array.
  player record stride 0x1A4; +0x00 last-name ptr, +0x04 first-name ptr (UTF-16BE name pool),
  +0x40 position dword (goalie iff (v>>3)&7 == 0), +0xB4 mask-shell dword, +0xB8 mask-pattern dword.
  host address = 0x100000000 + guest_addr.

The mask assignment names the texture helmet_g{shell+1:02d}_pattern_{pattern:02d}.iff (the game's
name builder uses shell index+? — see NOTE) — align this with add_custom_mask()'s slot.
"""
import struct

try:
    from . import xenia_mem as XM
except ImportError:
    import xenia_mem as XM

VIRT = 0x100000000                       # host = VIRT + guest_addr
G_ROSTER_MANAGER_VA = 0x849DE29C         # global holding the manager base pointer
PLAYER_CHUNK_HASH   = 0x1E159C31
REC        = 0x1A4
OFF_LNAME  = 0x00
OFF_FNAME  = 0x04
OFF_POS    = 0x40
OFF_SHELL  = 0xB4                        # shell = (dword >> 23) & 0xF
OFF_PAT    = 0xB8                        # pattern = dword & 0x1F
OFF_KEY    = 0x1C                        # u16 BE = portrait key (identity check against Roster.ROS)
SHELL_SHIFT = 23
SHELL_MASK  = 0xF
PAT_MASK    = 0x1F
# The mask is team-RECOLORED: the game substitutes 3 per-goalie colors (0xFFRRGGBB at +0x158/+0x15C/
# +0x160) weighted by the texture's R/G/B channels (Goalie mesh setup Function_84095150, shader params
# 0xfabd032/0x78ace0a4/-0x1e5a4ee2). Setting them to an RGB IDENTITY makes the substitution a pass-
# through, so a custom TRUE-COLOR texture renders exactly (no recolor). Shipped masks use team colors.
OFF_COLORS      = 0x158
IDENTITY_COLORS = (0xFFFF0000, 0xFF00FF00, 0xFF0000FF)   # red / green / blue basis


def _rd(h, guest, n):
    return XM.read_bytes(h, VIRT + guest, n)


def _u32(h, guest):
    b = _rd(h, guest, 4)
    return struct.unpack(">I", b)[0] if b else None


def manager_base(h):
    m = _u32(h, G_ROSTER_MANAGER_VA)
    return m if (m and 0x1000 < m < 0xFFFFFFFF) else None


def player_array(h, mgr):
    """Walk the manager chunk table for the player chunk -> (guest_ptr, count)."""
    for i in range(80):
        o = mgr + 0x08 + i * 12
        hsh = _u32(h, o)
        if hsh is None:
            break
        if hsh == PLAYER_CHUNK_HASH:
            cnt = _u32(h, o + 4)
            ptr = _u32(h, o + 8)
            if ptr and 0x1000 < ptr < 0xFFFFFFFF and 0 < (cnt or 0) < 20000:
                return ptr, cnt
    return None, None


def _read_utf16(h, guest, maxchars=48):
    if not guest or guest < 0x1000:
        return ""
    b = _rd(h, guest, maxchars * 2)
    if not b:
        return ""
    out = []
    for i in range(0, len(b) - 1, 2):
        c = (b[i] << 8) | b[i + 1]
        if c == 0:
            break
        if 0x20 <= c < 0x3000:
            out.append(chr(c))
    return "".join(out)


def enumerate_goalies(h):
    """Return [{index, addr(guest), first, last, name, shell, pattern, portrait, roster_count}] for
    every goalie in the loaded roster, or [] if the roster manager isn't reachable (game not at a
    roster-loaded state).

    `index`/`roster_count` line a live record up with the same row in Roster.ROS (same table, same
    stride, same order); `portrait` is the per-row key that double-checks the match. See
    player_assign.rows_for_live — that pairing is what lets the Goalie tab write the SAVE FILE
    instead of live memory."""
    mgr = manager_base(h)
    if not mgr:
        return []
    arr, cnt = player_array(h, mgr)
    if not arr:
        return []
    # bulk-read the whole record array (fast), then parse
    blob = _rd(h, arr, cnt * REC)
    if not blob or len(blob) < cnt * REC:
        # region may span an allocation boundary — fall back to chunked reads
        blob = b"".join(_rd(h, arr + o, min(0x40000, cnt * REC - o)) or b""
                        for o in range(0, cnt * REC, 0x40000))
    out = []
    for i in range(cnt):
        base = i * REC
        if base + OFF_PAT + 4 > len(blob):
            break
        pos = struct.unpack_from(">I", blob, base + OFF_POS)[0]
        if ((pos >> 3) & 7) != 0:                       # not a goalie
            continue
        ln = struct.unpack_from(">I", blob, base + OFF_LNAME)[0]
        fn = struct.unpack_from(">I", blob, base + OFF_FNAME)[0]
        shell = (struct.unpack_from(">I", blob, base + OFF_SHELL)[0] >> SHELL_SHIFT) & SHELL_MASK
        pat = struct.unpack_from(">I", blob, base + OFF_PAT)[0] & PAT_MASK
        last = _read_utf16(h, ln)
        first = _read_utf16(h, fn)
        if not (last or first):
            continue
        out.append({"index": i, "addr": arr + base, "first": first, "last": last,
                    "name": (first + " " + last).strip(), "shell": shell, "pattern": pat,
                    "portrait": struct.unpack_from(">H", blob, base + OFF_KEY)[0],
                    "roster_count": cnt})
    return out


def set_mask(h, player_guest_addr, shell, pattern, identity=False):
    """Write shell (bits 23-26 of +0xB4) and pattern (bits 0-4 of +0xB8) in place, preserving the
    other bits of those dwords. If `identity` (a custom TRUE-COLOR mask), also set the 3 recolor
    slots to an RGB identity so the team-recolor substitution passes the texture through unchanged.
    Returns True on success."""
    b4 = _u32(h, player_guest_addr + OFF_SHELL)
    b8 = _u32(h, player_guest_addr + OFF_PAT)
    if b4 is None or b8 is None:
        return False
    nb4 = (b4 & ~(SHELL_MASK << SHELL_SHIFT)) | ((shell & SHELL_MASK) << SHELL_SHIFT)
    nb8 = (b8 & ~PAT_MASK) | (pattern & PAT_MASK)
    ok4 = XM.write_bytes(h, VIRT + player_guest_addr + OFF_SHELL, struct.pack(">I", nb4))
    ok8 = XM.write_bytes(h, VIRT + player_guest_addr + OFF_PAT, struct.pack(">I", nb8))
    if identity:
        for i, c in enumerate(IDENTITY_COLORS):
            XM.write_bytes(h, VIRT + player_guest_addr + OFF_COLORS + i * 4, struct.pack(">I", c))
    return bool(ok4 and ok8)


def read_mask(h, player_guest_addr):
    b4 = _u32(h, player_guest_addr + OFF_SHELL)
    b8 = _u32(h, player_guest_addr + OFF_PAT)
    if b4 is None or b8 is None:
        return None
    return ((b4 >> SHELL_SHIFT) & SHELL_MASK, b8 & PAT_MASK)


# ── high-level helpers the launcher tab uses ────────────────────────────────
def _open():
    """Attach to Xenia. Returns (handle, phys_unused) or (None, reason)."""
    pid = XM.find_pid()
    if not pid:
        return None, "Xenia is not running — launch the game first."
    try:
        h = XM.open_process(pid)
    except OSError as e:
        return None, f"can't open Xenia ({e}); run the launcher as Administrator."
    return h, None


def list_goalies():
    """(goalies, error). goalies = enumerate_goalies output; error = a message or None."""
    h, err = _open()
    if err:
        return [], err
    try:
        gs = enumerate_goalies(h)
        if not gs:
            return [], ("Roster not loaded in memory yet — get to the main menu / a roster screen "
                        "in-game, then refresh.")
        return gs, None
    finally:
        XM.close_handle(h)


def apply_masks(assignments, goalies=None):
    """assignments: {goalie_key: (shell, pattern)}. goalie_key = 'first|last' (stable-ish id).
    Returns (n_applied, error). Re-reads the roster so addresses are current.

    A goalie name can appear in SEVERAL records (active roster + free-agent / all-star / created
    pools) — the game may render any of them — so we write EVERY record that matches the name, not
    just one. (Earlier bug: a name→record dict collapsed the duplicates to one, so the change landed
    on a pool copy the game wasn't drawing.)"""
    h, err = _open()
    if err:
        return 0, err
    try:
        gs = goalies or enumerate_goalies(h)
        by_key = {}
        for g in gs:
            by_key.setdefault(f"{g['first']}|{g['last']}", []).append(g)
        n = 0
        for key, val in assignments.items():
            shell, pat = val[0], val[1]
            identity = len(val) > 2 and bool(val[2])   # custom true-color mask -> RGB-identity colors
            for g in by_key.get(key, []):
                if set_mask(h, g["addr"], shell, pat, identity):
                    n += 1
        return n, None
    finally:
        XM.close_handle(h)
