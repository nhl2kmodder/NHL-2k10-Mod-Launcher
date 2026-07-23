"""
ros_live_editor.py — edit the NHL 2K10 roster in the RUNNING game (Xenia), with real field labels.

Unlike the file editor, this reads the live in-memory player records (stride 0x1A4) whose layout we
mapped from the game code (Ghidra): names are resolvable pointers, so every player row is labelled by
NAME, and known fields (birthdate, handedness, position, goalie mask, recolour colours) are shown with
labels. Edits are written straight into the running game's memory. To PERSIST them, save the roster
in-game afterwards.

Field map (memory record, from the player generator Function_83FE66F0 + accessors @0x840a6xxx):
  +0x00 last-name ptr   +0x04 first-name ptr
  +0x1C bits0-3 birth month, bits4-15 birth year(offset-encoded)   +0x20 bits27-31 birth day
  +0x34 bit15 shoots(L/R)   +0x40 bits0-2 pos-a, bits3-5 pos-b
  +0xB4 bits23-26 goalie shell   +0xB8 bits0-4 goalie pattern
  +0x158/+0x15C/+0x160 recolour colours (0xAARRGGBB)
Add your own fields (offset + type + bit range) to map ratings/jersey by experiment — the raw hex view
helps. Requires the game at a roster-loaded state (main menu / roster screen).
"""
import sys, struct
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
sys.path.insert(0, str(Path(__file__).parent))
import goalie_equipment as GE
import xenia_mem as XM

# Enum maps (value -> display string). The editor writes the value; the user only sees the string.
POS_MAP     = {0: "G", 1: "D", 2: "LW", 3: "RW", 4: "C"}
CAP_MAP     = {0: "None", 1: "Alternate (A)", 2: "Captain (C)"}
SHOOTS_MAP  = {0: "Left", 1: "Right"}
# 0-2 = forward types, 3-4 = defenseman types, 5-6 = goalie styles (verified: Crosby=0, D.Sedin=1,
# Kesler=2, Chara/Pronger=3, Mitchell=4, Luongo=5, Thomas=6). Labels for 3/5/6 are inferred.
TYPE_MAP    = {0: "Playmaker", 1: "Scorer", 2: "Two-Way", 3: "Offensive D",
               4: "Defensive", 5: "Goalie (Butterfly)", 6: "Goalie (Standup)"}
# Country = 7-bit field at bit 4 of u32@0x28; values 62..91 run alphabetically Austria->USA (verified
# live against known-nationality players: Regehr=Brazil-born 65, Zubrus=Lithuania 79, Kolzig=S.Africa 86).
COUNTRY_MAP = {62: "Austria", 63: "Belarus", 64: "Belgium", 65: "Brazil", 66: "Canada",
               67: "Czech Republic", 68: "Denmark", 69: "Estonia", 70: "Finland", 71: "France",
               72: "Germany", 73: "Great Britain", 74: "Hungary", 75: "Italy", 76: "Japan",
               77: "Kazakhstan", 78: "Latvia", 79: "Lithuania", 80: "Netherlands", 81: "Norway",
               82: "Poland", 83: "Russia", 84: "Slovakia", 85: "Slovenia", 86: "South Africa",
               87: "South Korea", 88: "Sweden", 89: "Switzerland", 90: "Ukraine", 91: "USA"}

# type: scalar u8/u16/u32 | bits(lo,w) | scaled(lo,w,mul,add) | enum(lo,w,map) | color | name(read-only).
# "readonly": True -> shown but not editable. group = UI section.
KNOWN_FIELDS = [
    {"name": "First name", "off": 0x04, "type": "name", "group": "Bio"},
    {"name": "Last name",  "off": 0x00, "type": "name", "group": "Bio"},
    {"name": "Jersey #",   "off": 0x20, "type": "bits", "lo": 12, "w": 7, "group": "Bio"},
    {"name": "Position", "off": 0x40, "type": "enum", "lo": 3, "w": 3, "map": POS_MAP, "group": "Bio"},
    {"name": "Captaincy", "off": 0x20, "type": "enum", "lo": 0, "w": 2, "map": CAP_MAP, "group": "Bio"},
    {"name": "Shoots", "off": 0x34, "type": "enum", "lo": 15, "w": 1, "map": SHOOTS_MAP, "group": "Bio"},
    {"name": "Height (in)", "off": 0x0C, "type": "scaled", "lo": 15, "w": 8, "mul": 0.2, "add": 50, "group": "Bio"},
    {"name": "Weight (lb)", "off": 0x11, "type": "scaled", "lo": 26, "w": 6, "mul": 4, "add": 133, "group": "Bio"},
    {"name": "Country", "off": 0x28, "type": "enum", "lo": 4, "w": 7, "map": COUNTRY_MAP, "group": "Bio"},
    {"name": "Overall", "off": 0x34, "type": "bits", "lo": 0, "w": 7, "readonly": True, "group": "Bio"},
    {"name": "Type", "off": 0x34, "type": "enum", "lo": 10, "w": 3, "map": TYPE_MAP, "group": "Bio"},
    {"name": "Salary ($)", "off": 0x38, "type": "bits", "lo": 3, "w": 24, "group": "Bio"},
    {"name": "Contract (yrs)", "off": 0x39, "type": "bits", "lo": 0, "w": 3, "group": "Bio"},
    {"name": "Birth year", "off": 0x1C, "type": "bits", "lo": 4, "w": 12, "group": "Bio"},
    {"name": "Birth month", "off": 0x1C, "type": "bits", "lo": 0, "w": 4, "group": "Bio"},
    {"name": "Birth day",   "off": 0x20, "type": "bits", "lo": 27, "w": 5, "group": "Bio"},
    {"name": "Portrait ID", "off": 0x1C, "type": "bits", "lo": 16, "w": 16, "group": "Bio"},
    {"name": "Goalie shell",   "off": 0xB4, "type": "bits", "lo": 23, "w": 4, "group": "Goalie mask"},
    {"name": "Goalie pattern", "off": 0xB8, "type": "bits", "lo": 0, "w": 5, "group": "Goalie mask"},
    {"name": "Recolor 1", "off": 0x158, "type": "color", "group": "Colors"},
    {"name": "Recolor 2", "off": 0x15C, "type": "color", "group": "Colors"},
    {"name": "Recolor 3", "off": 0x160, "type": "color", "group": "Colors"},
]
# Ratings block: sequential 2-byte slots 0x52..0xA6, each a 10-bit value at bit 4 where the
# DISPLAYED rating = stored * 0.16 (stored = rating * 6.25). Verified 0-mismatch vs 4 known players.
_ATTR = [(0x52, "Speed"), (0x54, "Acceleration"), (0x56, "Agility"), (0x58, "Balance"),
         (0x5A, "Strength"), (0x5C, "Stamina"), (0x5E, "Durability"), (0x60, "Fighting"),
         (0x66, "Puck Handling"), (0x68, "Passing"), (0x6A, "Poke Check"), (0x6C, "Body Check"),
         (0x6E, "Faceoffs"), (0x70, "Shot Power"), (0x72, "Shot Accuracy"), (0x74, "Hand-Eye"),
         (0x80, "Off. Awareness"), (0x82, "Def. Awareness"), (0x84, "Discipline"),
         (0x86, "Aggressiveness"), (0x88, "Clutch"), (0x8A, "Creativity"), (0x8C, "Courage"),
         (0x8E, "Influence"), (0x90, "Hustle"), (0x98, "Potential")]
_GOALIE = [(0x62, "Quickness"), (0x64, "Reflex"), (0x76, "Glove"), (0x78, "Blocker"),
           (0x7A, "Pads"), (0x7C, "Rebound"), (0x7E, "Positioning"), (0x92, "Vision"),
           (0x94, "Anticipation"), (0x96, "Focus")]
_TEND = [(0x9A, "Tend: Shoot"), (0x9C, "Tend: Stick Handle"), (0x9E, "Tend: Body Check"),
         (0xA0, "Tend: Poke Check"), (0xA2, "Tend: Fight"), (0xA4, "Tend: Block Shot"),
         (0xA6, "Tend: Challenge")]
for _grp, _lst in (("Attributes", _ATTR), ("Goalie", _GOALIE), ("Tendencies", _TEND)):
    for _o, _nm in _lst:
        KNOWN_FIELDS.append({"name": _nm, "off": _o, "type": "scaled", "lo": 4, "w": 10,
                             "mul": 0.16, "add": 0, "group": _grp})


class LiveRoster:
    def __init__(self):
        self.h, err = GE._open()
        if not self.h:
            raise RuntimeError(err or "could not attach to Xenia")
        mgr = GE.manager_base(self.h)
        if not mgr:
            raise RuntimeError("Roster not loaded in memory — get the game to the main menu / a roster "
                               "screen, then click Reload.")
        self.arr, self.cnt = GE.player_array(self.h, mgr)
        if not self.arr:
            raise RuntimeError("Player table not found in the running game.")
        self.reload()

    def reload(self):
        self.blob = GE._rd(self.h, self.arr, self.cnt * GE.REC) or b""

    def rec(self, i):
        return self.blob[i * GE.REC:(i + 1) * GE.REC]

    def names(self, i):
        b = self.rec(i)
        fn = struct.unpack(">I", b[4:8])[0]; ln = struct.unpack(">I", b[0:4])[0]
        return GE._read_utf16(self.h, fn), GE._read_utf16(self.h, ln)

    def _u32(self, i, off):
        return struct.unpack(">I", self.blob[i * GE.REC + off:i * GE.REC + off + 4])[0]

    def get(self, i, f):
        t = f["type"]
        if t == "name":
            fn, ln = self.names(i); return fn if f["off"] == 4 else ln
        v = self._u32(i, f["off"])
        if t == "bits":
            return (v >> f["lo"]) & ((1 << f["w"]) - 1)
        if t == "enum":
            raw = (v >> f["lo"]) & ((1 << f["w"]) - 1)
            return f["map"].get(raw, str(raw))
        if t == "scaled":
            return round(((v >> f["lo"]) & ((1 << f["w"]) - 1)) * f["mul"] + f["add"])
        if t == "color":
            return "%08X" % v
        if t == "u8":
            return self.rec(i)[f["off"]]
        return v

    def set(self, i, f, text):
        """Write a field into live memory. Returns None on success or an error string."""
        t = f["type"]
        if t == "name":
            return "editing names isn't supported yet (use the in-game name editor)"
        if f.get("readonly"):
            return "this field is read-only (the game computes it)"
        cur = self._u32(i, f["off"])
        try:
            if t == "color":
                nv = int(text, 16) & 0xFFFFFFFF
            elif t == "enum":
                m = (1 << f["w"]) - 1
                rev = {lbl: val for val, lbl in f["map"].items()}
                raw = rev[text] if text in rev else (int(text, 0) & m)
                nv = (cur & ~(m << f["lo"])) | ((raw & m) << f["lo"])
            elif t == "bits":
                m = (1 << f["w"]) - 1
                nv = (cur & ~(m << f["lo"])) | ((int(text, 0) & m) << f["lo"])
            elif t == "scaled":
                m = (1 << f["w"]) - 1
                raw = int(round((float(text) - f["add"]) / f["mul"])) & m
                nv = (cur & ~(m << f["lo"])) | (raw << f["lo"])
            elif t == "u8":
                b = bytearray(struct.pack(">I", cur)); b[3] = int(text, 0) & 0xFF
                nv = struct.unpack(">I", bytes(b))[0]
            else:
                nv = int(text, 0) & 0xFFFFFFFF
        except Exception as e:
            return f"bad value: {e}"
        ok = XM.write_bytes(self.h, GE.VIRT + self.arr + i * GE.REC + f["off"], struct.pack(">I", nv))
        # keep local snapshot in sync
        base = i * GE.REC + f["off"]
        self.blob = self.blob[:base] + struct.pack(">I", nv) + self.blob[base + 4:]
        return None if ok else "memory write failed"

    def close(self):
        if self.h:
            XM.close_handle(self.h); self.h = None


class LiveEditorFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.roster = None
        self.fields = list(KNOWN_FIELDS)
        self.sel = None
        self._build()
        self._attach()

    def _build(self):
        top = ttk.Frame(self, padding=6); top.pack(fill=tk.X)
        ttk.Button(top, text="Reload from game", command=self._attach).pack(side=tk.LEFT)
        self._status = ttk.Label(top, text="", foreground="#888"); self._status.pack(side=tk.LEFT, padx=10)
        ttk.Label(top, text="Edits go straight into the running game — SAVE THE ROSTER IN-GAME to keep them.",
                  foreground="#e0a030").pack(side=tk.RIGHT)

        pane = ttk.Panedwindow(self, orient=tk.HORIZONTAL); pane.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(pane); pane.add(left, weight=1)
        fb = ttk.Frame(left, padding=(6, 4)); fb.pack(fill=tk.X)
        ttk.Label(fb, text="Find:").pack(side=tk.LEFT)
        self._q = tk.StringVar(); self._q.trace_add("write", lambda *_: self._fill_players())
        ttk.Entry(fb, textvariable=self._q).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._plist = ttk.Treeview(left, columns=("pos",), show="tree headings", selectmode="browse")
        self._plist.heading("#0", text="Player"); self._plist.column("#0", width=190)
        self._plist.heading("pos", text="#"); self._plist.column("pos", width=48, anchor=tk.E)
        psb = ttk.Scrollbar(left, command=self._plist.yview); self._plist.configure(yscroll=psb.set)
        psb.pack(side=tk.RIGHT, fill=tk.Y); self._plist.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        self._plist.bind("<<TreeviewSelect>>", self._on_player)

        right = ttk.Frame(pane); pane.add(right, weight=3)
        fw = ttk.LabelFrame(right, text="Fields (Enter to write to the game)")
        fw.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        cv = tk.Canvas(fw, highlightthickness=0)
        fsb = ttk.Scrollbar(fw, orient=tk.VERTICAL, command=cv.yview); cv.configure(yscrollcommand=fsb.set)
        fsb.pack(side=tk.RIGHT, fill=tk.Y); cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._form = ttk.Frame(cv, padding=6)
        _win = cv.create_window((0, 0), window=self._form, anchor="nw")
        self._form.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfig(_win, width=e.width))
        cv.bind("<Enter>", lambda e: cv.bind_all("<MouseWheel>", lambda ev: cv.yview_scroll(int(-ev.delta / 120), "units")))
        cv.bind("<Leave>", lambda e: cv.unbind_all("<MouseWheel>"))
        # custom field adder
        cf = ttk.Frame(right, padding=(6, 2)); cf.pack(fill=tk.X)
        ttk.Label(cf, text="add field  name").pack(side=tk.LEFT)
        self._cf_name = ttk.Entry(cf, width=12); self._cf_name.pack(side=tk.LEFT, padx=2)
        ttk.Label(cf, text="off").pack(side=tk.LEFT); self._cf_off = ttk.Entry(cf, width=6); self._cf_off.pack(side=tk.LEFT, padx=2)
        ttk.Label(cf, text="type").pack(side=tk.LEFT)
        self._cf_type = ttk.Combobox(cf, values=["u8", "u16", "u32", "bits", "color"], width=6, state="readonly")
        self._cf_type.set("u8"); self._cf_type.pack(side=tk.LEFT, padx=2)
        ttk.Label(cf, text="bit lo/w").pack(side=tk.LEFT)
        self._cf_lo = ttk.Entry(cf, width=3); self._cf_lo.pack(side=tk.LEFT); self._cf_w = ttk.Entry(cf, width=3); self._cf_w.pack(side=tk.LEFT, padx=2)
        ttk.Button(cf, text="Add", command=self._add_field).pack(side=tk.LEFT, padx=4)
        hb = ttk.LabelFrame(right, text="Selected record — raw bytes", padding=4); hb.pack(fill=tk.X, padx=6, pady=4)
        self._hex = tk.Text(hb, height=8, font=("Consolas", 9), wrap=tk.NONE); self._hex.pack(fill=tk.X)

    def _attach(self):
        try:
            if self.roster:
                self.roster.close()
            self.roster = LiveRoster()
        except Exception as e:
            self.roster = None
            self._status.config(text=str(e), foreground="#e06060")
            self._plist.delete(*self._plist.get_children()); return
        self._status.config(text=f"{self.roster.cnt} players in the loaded roster.", foreground="#8c8")
        self._fill_players()

    def _fill_players(self):
        if not self.roster:
            return
        q = self._q.get().strip().lower()
        self._plist.delete(*self._plist.get_children())
        for i in range(self.roster.cnt):
            fn, ln = self.roster.names(i)
            nm = (fn + " " + ln).strip()
            if not nm:
                continue
            if q and q not in nm.lower():
                continue
            self._plist.insert("", tk.END, iid=str(i), text=nm, values=(i,))

    def _on_player(self, _=None):
        sel = self._plist.selection()
        if not sel:
            return
        self.sel = int(sel[0]); self._render_form(); self._render_hex()

    def _render_form(self):
        from collections import OrderedDict
        for w in self._form.winfo_children():
            w.destroy()
        if self.sel is None or not self.roster:
            return
        self._vars = {}
        groups = OrderedDict()
        for f in self.fields:
            groups.setdefault(f.get("group", "Custom"), []).append(f)
        row = 0
        for gname, gfields in groups.items():
            ttk.Label(self._form, text=gname, font=("Segoe UI", 9, "bold"),
                      foreground="#4a9").grid(row=row, column=0, columnspan=4, sticky=tk.W, pady=(9, 2))
            row += 1
            for i, f in enumerate(gfields):
                r = row + i // 2; col = (i % 2) * 2
                ttk.Label(self._form, text=f["name"]).grid(row=r, column=col, sticky=tk.W, pady=1, padx=(4, 2))
                v = tk.StringVar(value=str(self.roster.get(self.sel, f)))
                if f["type"] == "enum":
                    labels = [f["map"][k] for k in sorted(f["map"])]
                    e = ttk.Combobox(self._form, textvariable=v, values=labels, width=15, state="readonly")
                    e.bind("<<ComboboxSelected>>", lambda _e, ff=f, vv=v: self._commit(ff, vv))
                else:
                    ro = f["type"] == "name" or f.get("readonly")
                    e = ttk.Entry(self._form, textvariable=v, width=13,
                                  state="readonly" if ro else "normal")
                    if not ro:
                        e.bind("<Return>", lambda _e, ff=f, vv=v: self._commit(ff, vv))
                e.grid(row=r, column=col + 1, sticky=tk.W, padx=(0, 12))
                self._vars[f["name"]] = v
            row += (len(gfields) + 1) // 2

    def _commit(self, f, var):
        err = self.roster.set(self.sel, f, var.get().strip())
        if err:
            messagebox.showerror("Write", err)
        var.set(str(self.roster.get(self.sel, f)))
        self._render_hex()

    def _add_field(self):
        try:
            off = int(self._cf_off.get(), 0)
        except Exception:
            messagebox.showerror("Field", "offset must be a number"); return
        f = {"name": self._cf_name.get().strip() or f"@{off:X}", "off": off, "type": self._cf_type.get()}
        if f["type"] == "bits":
            f["lo"] = int(self._cf_lo.get() or 0); f["w"] = int(self._cf_w.get() or 1)
        self.fields.append(f); self._render_form()

    def _render_hex(self):
        if self.sel is None or not self.roster:
            return
        rec = self.roster.rec(self.sel)
        self._hex.delete("1.0", tk.END)
        for o in range(0, len(rec), 16):
            c = rec[o:o + 16]
            self._hex.insert(tk.END, f"{o:03X}  " + " ".join(f"{b:02X}" for b in c) +
                             "   " + "".join(chr(b) if 32 <= b < 127 else "." for b in c) + "\n")


def open_live_editor(parent):
    win = tk.Toplevel(parent)
    win.title("NHL 2K10 — Live Roster Editor (running game)")
    win.geometry("1080x760")
    LiveEditorFrame(win).pack(fill=tk.BOTH, expand=True)
    return win


# ── Team-roster editor ───────────────────────────────────────────────────────
TEAM_HASH = 0x8489FAF3
TEAM_STRIDE = 412
ROSTER_END = 0xA0          # roster = player-record pointers in [0x00, 0xA0); names start at 0xA0


class TeamTable:
    """Live team table (chunk 0x8489FAF3): each 412B record = a roster of player-record POINTERS at
    +0x00.. + team name pointers at +0xA0 (city) / +0xA4 (nick) / +0xA8 (code). A pointer is
    player_array_base + player_index*0x1A4, so index = (ptr-arr)/0x1A4."""
    def __init__(self):
        self.h, err = GE._open()
        if not self.h:
            raise RuntimeError(err or "could not attach to Xenia")
        mgr = GE.manager_base(self.h)
        if not mgr:
            raise RuntimeError("Roster not loaded — get the game to a roster/menu screen, then Reload.")
        ch = {}
        for i in range(260):
            o = mgr + 0x08 + i * 12; hs = GE._u32(self.h, o)
            if hs:
                ch[hs] = (GE._u32(self.h, o + 4), GE._u32(self.h, o + 8))
        if GE.PLAYER_CHUNK_HASH not in ch or TEAM_HASH not in ch:
            raise RuntimeError("player/team chunk not found in the running game")
        self.pcnt, self.arr = ch[GE.PLAYER_CHUNK_HASH]
        self.tcnt, self.tptr = ch[TEAM_HASH]
        self.pblob = GE._rd(self.h, self.arr, self.pcnt * GE.REC) or b""
        self._names = self._build_names()

    def _build_names(self):
        # bulk-read the string pool once (name ptrs cluster there) instead of 5000 tiny reads
        ptrs = []
        for i in range(self.pcnt):
            b = self.pblob[i * GE.REC:i * GE.REC + 8]
            ptrs.append((struct.unpack(">I", b[0:4])[0], struct.unpack(">I", b[4:8])[0]))
        flat = [p for pr in ptrs for p in pr if 0x10000 < p < 0xFFFFFFFF]
        lo, hi = (min(flat), max(flat)) if flat else (0, 0)
        pool = GE._rd(self.h, lo, hi - lo + 64) if flat and hi - lo < 8_000_000 else None
        def rs(p):
            if pool is not None and lo <= p < lo + len(pool):
                o = p - lo; s = []
                for k in range(o, len(pool) - 1, 2):
                    c = (pool[k] << 8) | pool[k + 1]
                    if c == 0:
                        break
                    if 0x20 <= c < 0x3000:
                        s.append(chr(c))
                return "".join(s)
            return GE._read_utf16(self.h, p)
        out = {}
        for i, (ln, fn) in enumerate(ptrs):
            nm = (rs(fn) + " " + rs(ln)).strip()
            if nm:
                out[i] = nm
        return out

    def ptr2idx(self, v):
        if self.arr <= v < self.arr + self.pcnt * GE.REC and (v - self.arr) % GE.REC == 0:
            return (v - self.arr) // GE.REC
        return None

    def pname(self, idx):
        return self._names.get(idx, f"#{idx}")

    def _trec(self, t):
        return GE._rd(self.h, self.tptr + t * TEAM_STRIDE, TEAM_STRIDE)

    def team_label(self, t):
        r = self._trec(t)
        def s(o):
            p = struct.unpack_from(">I", r, o)[0]
            return GE._read_utf16(self.h, p) if 0x10000 < p < 0xFFFFFFFF else ""
        return s(0xA0), s(0xA4), s(0xA8)

    def teams(self):
        out = []
        for t in range(self.tcnt):
            city, nick, code = self.team_label(t)
            if city or nick:
                out.append((t, city, nick, code))
        return out

    def roster(self, t):
        r = self._trec(t); out = []
        for o in range(0, ROSTER_END, 4):
            idx = self.ptr2idx(struct.unpack_from(">I", r, o)[0])
            if idx is not None:
                out.append((o, idx, self.pname(idx)))
        return out

    def slot_player(self, t, slot_off):
        r = self._trec(t)
        return self.ptr2idx(struct.unpack_from(">I", r, slot_off)[0])

    def set_slot(self, t, slot_off, player_idx):
        ptr = self.arr + player_idx * GE.REC
        return XM.write_bytes(self.h, GE.VIRT + self.tptr + t * TEAM_STRIDE + slot_off,
                              struct.pack(">I", ptr))

    def all_players(self):
        return sorted(self._names.items(), key=lambda kv: kv[1])

    def close(self):
        if self.h:
            XM.close_handle(self.h); self.h = None


class TeamRosterFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.tt = None; self.team = None; self.slot = None; self.mark = None
        self._build(); self._attach()

    def _build(self):
        top = ttk.Frame(self, padding=6); top.pack(fill=tk.X)
        ttk.Button(top, text="Reload from game", command=self._attach).pack(side=tk.LEFT)
        self._status = ttk.Label(top, text="", foreground="#888"); self._status.pack(side=tk.LEFT, padx=10)
        ttk.Label(top, text="Assign writes into the running game — save the roster IN-GAME to keep it.",
                  foreground="#e0a030").pack(side=tk.RIGHT)
        pane = ttk.Panedwindow(self, orient=tk.HORIZONTAL); pane.pack(fill=tk.BOTH, expand=True)
        # teams
        lf = ttk.Labelframe(pane, text="Teams"); pane.add(lf, weight=2)
        self._tv_team = ttk.Treeview(lf, columns=("nick", "code"), show="tree headings", selectmode="browse")
        self._tv_team.heading("#0", text="City"); self._tv_team.column("#0", width=110)
        self._tv_team.heading("nick", text="Nickname"); self._tv_team.column("nick", width=100)
        self._tv_team.heading("code", text="Code"); self._tv_team.column("code", width=48, anchor=tk.CENTER)
        sb1 = ttk.Scrollbar(lf, command=self._tv_team.yview); self._tv_team.configure(yscroll=sb1.set)
        sb1.pack(side=tk.RIGHT, fill=tk.Y); self._tv_team.pack(fill=tk.BOTH, expand=True)
        self._tv_team.bind("<<TreeviewSelect>>", self._on_team)
        # roster
        mf = ttk.Labelframe(pane, text="Roster (select a slot, then a player →)"); pane.add(mf, weight=2)
        self._tv_ros = ttk.Treeview(mf, columns=("slot",), show="tree headings", selectmode="browse")
        self._tv_ros.heading("#0", text="Player"); self._tv_ros.column("#0", width=190)
        self._tv_ros.heading("slot", text="Slot"); self._tv_ros.column("slot", width=44, anchor=tk.CENTER)
        sb2 = ttk.Scrollbar(mf, command=self._tv_ros.yview); self._tv_ros.configure(yscroll=sb2.set)
        sb2.pack(side=tk.RIGHT, fill=tk.Y); self._tv_ros.pack(fill=tk.BOTH, expand=True)
        self._tv_ros.bind("<<TreeviewSelect>>", lambda e: setattr(self, "slot", self._sel_slot()))
        sw = ttk.Frame(mf, padding=(4, 4)); sw.pack(fill=tk.X)
        ttk.Button(sw, text="Mark slot ↔", command=self._mark).pack(side=tk.LEFT)
        ttk.Button(sw, text="⇄ Swap with marked", command=self._swap).pack(side=tk.LEFT, padx=4)
        self._marklbl = ttk.Label(sw, text="(nothing marked)", foreground="#888"); self._marklbl.pack(side=tk.LEFT, padx=6)
        # player picker
        rf = ttk.Labelframe(pane, text="All players"); pane.add(rf, weight=2)
        pb = ttk.Frame(rf, padding=(4, 4)); pb.pack(fill=tk.X)
        ttk.Label(pb, text="Find:").pack(side=tk.LEFT)
        self._q = tk.StringVar(); self._q.trace_add("write", lambda *_: self._fill_players())
        ttk.Entry(pb, textvariable=self._q).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._tv_all = ttk.Treeview(rf, columns=(), show="tree", selectmode="browse")
        self._tv_all.column("#0", width=200)
        sb3 = ttk.Scrollbar(rf, command=self._tv_all.yview); self._tv_all.configure(yscroll=sb3.set)
        sb3.pack(side=tk.RIGHT, fill=tk.Y); self._tv_all.pack(fill=tk.BOTH, expand=True)
        ttk.Button(rf, text="◀  Assign selected player to selected slot",
                   command=self._assign).pack(fill=tk.X, padx=4, pady=4)

    def _attach(self):
        try:
            if self.tt:
                self.tt.close()
            self._status.config(text="reading team table…", foreground="#888"); self.update_idletasks()
            self.tt = TeamTable()
        except Exception as e:
            self.tt = None; self._status.config(text=str(e), foreground="#e06060")
            self._tv_team.delete(*self._tv_team.get_children()); return
        teams = self.tt.teams()
        self._status.config(text=f"{len(teams)} teams, {len(self.tt._names)} named players.", foreground="#8c8")
        self._tv_team.delete(*self._tv_team.get_children())
        for t, city, nick, code in teams:
            self._tv_team.insert("", tk.END, iid=str(t), text=city, values=(nick, code))
        self._fill_players()

    def _on_team(self, _=None):
        sel = self._tv_team.selection()
        if not sel:
            return
        self.team = int(sel[0]); self.slot = None
        self._tv_ros.delete(*self._tv_ros.get_children())
        for off, idx, nm in self.tt.roster(self.team):
            self._tv_ros.insert("", tk.END, iid=str(off), text=nm, values=(off // 4,))

    def _sel_slot(self):
        s = self._tv_ros.selection()
        return int(s[0]) if s else None

    def _fill_players(self):
        if not self.tt:
            return
        q = self._q.get().strip().lower()
        self._tv_all.delete(*self._tv_all.get_children())
        for idx, nm in self.tt.all_players():
            if q and q not in nm.lower():
                continue
            self._tv_all.insert("", tk.END, iid=str(idx), text=nm)

    def _mark(self):
        if self.team is None or self.slot is None:
            messagebox.showinfo("Swap", "Select a team and a roster slot to mark."); return
        idx = self.tt.slot_player(self.team, self.slot)
        self.mark = (self.team, self.slot, idx)
        city = self.tt.team_label(self.team)[0]
        self._marklbl.config(text=f"Marked: {self.tt.pname(idx)} — {city} slot {self.slot // 4}",
                             foreground="#4a9")

    def _swap(self):
        if not self.mark:
            messagebox.showinfo("Swap", "Mark a slot first (select a slot → Mark slot)."); return
        if self.team is None or self.slot is None:
            messagebox.showinfo("Swap", "Select the second slot to swap with."); return
        tA, oA, iA = self.mark; tB, oB = self.team, self.slot
        iB = self.tt.slot_player(tB, oB)
        if iA is None or iB is None:
            messagebox.showerror("Swap", "One of the slots is empty/invalid."); return
        if (tA, oA) == (tB, oB):
            messagebox.showinfo("Swap", "That's the same slot."); return
        ok = self.tt.set_slot(tA, oA, iB) and self.tt.set_slot(tB, oB, iA)
        if not ok:
            messagebox.showerror("Swap", "memory write failed"); return
        na, nb = self.tt.pname(iA), self.tt.pname(iB)
        self.mark = None; self._marklbl.config(text="(nothing marked)", foreground="#888")
        self._on_team()
        messagebox.showinfo("Swap", f"Swapped:\n  {na} ↔ {nb}\n\nSave the roster in-game to keep it.")

    def _assign(self):
        if self.team is None or self.slot is None:
            messagebox.showinfo("Assign", "Select a team, a roster slot, and a player first."); return
        sp = self._tv_all.selection()
        if not sp:
            messagebox.showinfo("Assign", "Select a player from the list first."); return
        idx = int(sp[0])
        if not self.tt.set_slot(self.team, self.slot, idx):
            messagebox.showerror("Assign", "memory write failed"); return
        self._on_team()   # refresh roster


def open_team_editor(parent):
    win = tk.Toplevel(parent)
    win.title("NHL 2K10 — Team Rosters (running game)")
    win.geometry("1120x720")
    TeamRosterFrame(win).pack(fill=tk.BOTH, expand=True)
    return win


if __name__ == "__main__":
    root = tk.Tk(); root.title("NHL 2K10 — Live Roster Editor"); root.geometry("1080x760")
    LiveEditorFrame(root).pack(fill=tk.BOTH, expand=True)
    root.mainloop()
