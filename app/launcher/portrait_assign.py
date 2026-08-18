"""portrait_assign.py — live in-memory player-portrait assignment.

The player's UI portrait is selected by the u16 at **player_record + 0x1C** ('portrait key'): the game
formats an asset name `"%04d_image" % key`, hashes it (Str_Hash = CRC32), and that crc equals the
header hash of one portrait blob in disc_b9610aac.iff. So a player shows portrait `key_blob[key]`, and
we reassign by writing that u16 in the running game. Reverse-engineered from Function_83D32188 /
FUN_840a69e0 (the reader is literally `*(u16*)(player+0x1C)`); confirmed live (507->200 = grey).

Like goalie_equipment.py, this module is the LISTING half of its tab. It used to enumerate the
running game because that was believed to be the only place player names resolve — that stopped
being true on 2026-08-06, when the on-disk name pointers were solved (player_assign), so
list_players(ros_path) now reads the save directly and the live walk below is the fallback for when
no roster file is set. The assignment itself is written into
**Roster.ROS** by player_assign.py — the on-disk record is the same struct, so the key really is at
+0x1C there too, and a file edit (unlike a memory patch) survives Xenia closing and works on real
hardware. set_portrait_key()/apply_portraits() below are kept for diagnostics only.

key<->blob comes from archive_textures.portrait_key_blob_map(). Roster walk mirrors goalie_equipment.py.
"""
import struct

try:
    from . import xenia_mem as XM
    from . import player_ids as _pid
except ImportError:
    import xenia_mem as XM
    import player_ids as _pid

VIRT = 0x100000000                       # host = VIRT + guest_addr
G_ROSTER_MANAGER_VA = 0x849DE29C         # global holding the manager base pointer
PLAYER_CHUNK_HASH   = 0x1E159C31
REC        = 0x1A4
OFF_LNAME  = 0x00
OFF_FNAME  = 0x04
OFF_KEY    = 0x1C                        # u16 BE = portrait key ( *(u16*)(player+0x1C) )
OFF_BIRTH  = 0x1C                        # u32: bits0-3 birth month, bits4-15 birth year
OFF_NUM    = 0x20                        # u32: bits12-18 jersey number, bits27-31 birth day


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


bio_fields = _pid.bio_fields          # (jersey number, birth fingerprint) out of a raw record


def enumerate_players(h):
    """[{index, addr(guest), first, last, name, key, num, bio, pid, roster_count}] for every NAMED
    player in the loaded roster, or [] if the roster manager isn't reachable (game not at a
    roster-loaded state).

    `index` is the record's slot in the live player array and `roster_count` the array's length —
    together they let a caller line a live record up with the same row in Roster.ROS, which is the
    same table with the same stride and order (see player_assign.rows_for_live).

    `pid` is the stable per-PERSON id (see player_ids.assign_player_ids) — several records can be
    the same player, and two different players can share a name."""
    mgr = manager_base(h)
    if not mgr:
        return []
    arr, cnt = player_array(h, mgr)
    if not arr:
        return []
    blob = _rd(h, arr, cnt * REC)
    if not blob or len(blob) < cnt * REC:                    # region may span an allocation boundary
        blob = b"".join(_rd(h, arr + o, min(0x40000, cnt * REC - o)) or b""
                        for o in range(0, cnt * REC, 0x40000))
    out = []
    for i in range(cnt):
        base = i * REC
        if base + REC > len(blob):
            break
        ln = struct.unpack_from(">I", blob, base + OFF_LNAME)[0]
        fn = struct.unpack_from(">I", blob, base + OFF_FNAME)[0]
        key = struct.unpack_from(">H", blob, base + OFF_KEY)[0]
        last = _read_utf16(h, ln)
        first = _read_utf16(h, fn)
        if not (last or first):
            continue
        num, bio = bio_fields(blob, base)
        out.append({"index": i, "addr": arr + base, "first": first, "last": last,
                    "name": (first + " " + last).strip(), "key": key, "num": num, "bio": bio,
                    "roster_count": cnt})
    _pid.assign_player_ids(out)
    return out


def read_portrait_key(h, player_guest_addr):
    b = _rd(h, player_guest_addr + OFF_KEY, 2)
    return struct.unpack(">H", b)[0] if b else None


def set_portrait_key(h, player_guest_addr, key):
    """Write the u16 portrait key at player+0x1C (big-endian). Returns True on success."""
    return bool(XM.write_bytes(h, VIRT + player_guest_addr + OFF_KEY, struct.pack(">H", key & 0xFFFF)))


# ── high-level helpers the launcher tab uses ────────────────────────────────
def _open():
    """Attach to Xenia. Returns (handle, None) or (None, reason)."""
    pid = XM.find_pid()
    if not pid:
        return None, "Xenia is not running — launch the game first."
    try:
        h = XM.open_process(pid)
    except OSError as e:
        try:
            from . import elevation as EL
        except ImportError:
            import elevation as EL
        return None, EL.explain_open_failure(e)
    return h, None


def list_players(ros_path=None):
    """(players, error). players = enumerate_players output; error = a message or None.

    With `ros_path`, the list is read straight out of that Roster.ROS and the game does NOT have to
    be running — names resolve on disk (player_assign's self-relative, SIGNED name pointers), and
    the record dicts carry the same fields, so the caller can't tell the two sources apart. The
    file is also the right source when both are available: the portrait write lands in the file, and
    a live roster that has drifted from it would list keys the save doesn't have.

    Without one, this falls back to enumerating the running game, as it always did.
    """
    if ros_path:
        try:
            from . import player_assign as _pas
        except ImportError:
            import player_assign as _pas
        try:
            ps = _pas.enumerate_players(ros_path)
        except Exception as e:
            return [], f"can't read {ros_path}: {e}"
        if not ps:
            return [], "no named players in that Roster.ROS — is it a valid save?"
        return ps, None
    h, err = _open()
    if err:
        return [], err
    try:
        ps = enumerate_players(h)
        if not ps:
            return [], ("Roster not loaded in memory yet — get to the main menu / a roster screen "
                        "in-game, then refresh.")
        return ps, None
    finally:
        XM.close_handle(h)


def apply_portraits(assignments, players=None):
    """assignments: {player_id: portrait_key}. player_id = 'first|last'. Writes the portrait key into
    EVERY record that matches the name (a player can appear in several pools). Returns (n_applied, err).
    Re-reads the roster so addresses are current."""
    h, err = _open()
    if err:
        return 0, err
    try:
        ps = players or enumerate_players(h)
        by_key = {}
        for p in ps:
            by_key.setdefault(f"{p['first']}|{p['last']}", []).append(p)
        n = 0
        for pid, portrait_key in assignments.items():
            for p in by_key.get(pid, []):
                if set_portrait_key(h, p["addr"], int(portrait_key)):
                    n += 1
        return n, None
    finally:
        XM.close_handle(h)
