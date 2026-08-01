"""
ros_editor_gui.py — a grid editor for NHL 2K10 Roster.ROS saves.

Pick a table (chunk) on the left; every record is a ROW. Define FIELDS at any byte offset
(name + offset + type) — each becomes an editable COLUMN across every row. A raw hex view of the
selected record helps you find fields by experimenting. Edits are written in place (file size never
changes); a one-time .bak backup is made on first save. Field definitions are remembered per table
in ros_fields.json so your field map grows over time.

Run:  python ros_editor_gui.py  ["<path to Roster.ROS>"]
CAVEAT: field meanings are yours to discover. There is no per-save scramble (see ros_file.py) — the
goalie mask round-trips fine; what does NOT round-trip is anything the game re-derives on save, e.g.
the audio-name ids below. Always test in-game; keep the .bak.
"""
import sys, json, struct
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
sys.path.insert(0, str(Path(__file__).parent))
from ros_file import RosFile, FMT

FIELDS_JSON = Path(__file__).parent / "ros_fields.json"
TYPES = ["u8", "i8", "u16", "i16", "u32", "i32", "f32", "hex"]

# Pre-mapped fields per chunk (seeded when a table has no saved field defs yet). Discovered by
# Ghidra + direct record analysis. The player record 0x1E159C31 (420B) carries an RGBA gear/team
# colour palette at +0xBD..+0x12D (each u32 = 0xRRGGBBAA). The three team-RECOLOUR colours the
# goalie system uses are a different, independently pinned set at +0x158/+0x15C/+0x160 (+0x168 cage)
# — this file used to label the palette block as those, which the 2026-08-01 reframe disproved: the
# two claims were 0x73 apart and only player_assign.py's was in-game verified. More fields (names,
# ratings) need serializer RE / in-game diffs — the file uses its own serialization, distinct from
# the live-memory record layout.
#
# +0x9F/+0xA1 are the announcer "audio name" ids: the id the commentary and PA systems resolve to
# a recorded name. 0xFFFF = no recording = announcer stays silent for that half of the name.
#
# REBASED AGAIN 2026-08-01 — and this time the frame itself is right, so it should be the last
# time. ros_file.py was reading the chunk directory with a fixed DATA_BASE; the offsets are really
# self-relative pointers, and the old reading framed the player chunk at 0x5A2D when record 0 is at
# 0x4F24. That is 6 records + 305 bytes, i.e. the GUI's "records" were a ROTATION straddling two
# real players — fields below +0x73 belonged to the next player along. Every offset here is
# therefore remapped  new = (old + 305) mod 420  (equivalently old - 0x73), with the row it belongs
# to shifting by 7. The two audio ids, empirically pinned at +0x9F/+0xA1 in the old frame, are
# +0x2C/+0x2E in this one. Confirmed by the in-game name-edit series in
# Roster_Investigate/test1..6.ROS — renaming a player moves +0x9F/+0xA1 and nothing else:
#   row 2318 first name Adam -> Alec -> Ashjay:  +0x2E = 5003 -> 5975 -> 0xFFFF (invented name)
#   row 2319 last name -> Lecavalier:            +0x2C = 0 -> 724, then constant while the first
#                                                name changed 5243 -> 5930 -> 5784
# (those rows were 2311/2312 before the +7 row shift; the byte addresses are unchanged.)
# Across the live save +0x2C has 1012 distinct values / 769 sentinels, +0x2E has 506 / 217.
#
# READ-ONLY IN PRACTICE: an earlier single-variable in-game test proved the game IGNORES a
# file-edited id for an existing player (stock Lucic id -> Getzlaf's id, PxP still said "Lucic"),
# while the game's own name picker writing that same byte does take effect. The id is a handle into
# the name string pool; the announcer looks up the pool TEXT, and the serializer reassigns handles
# on save. Treat these two fields as diagnostic — to change what the announcer says, change the
# NAME to one that has a recording. See memory project_player_audio_name_ids.
DEFAULT_FIELDS = {
    "0x1E159C31": (
        [{"name": "portrait", "off": 0x1C, "type": "u16"},          # these four are pinned in game
         {"name": "audio_last", "off": 0x2C, "type": "u16"},
         {"name": "audio_first", "off": 0x2E, "type": "u16"},
         {"name": "position", "off": 0x40, "type": "u32"},          # goalie iff (v>>3)&7 == 0
         {"name": "team_ptr", "off": 0x14, "type": "hex", "len": 4},
         {"name": "const20", "off": 0xA9, "type": "u32"},
         {"name": "mask_shell", "off": 0xB4, "type": "u32"},
         {"name": "mask_pattern", "off": 0xB8, "type": "u32"}]
        + [{"name": "palette_%02d" % ((o - 0xBD) // 4), "off": o, "type": "hex", "len": 4}
           for o in range(0xBD, 0x131, 4)]
        + [{"name": "recolor_%d" % ((o - 0x158) // 4 + 1), "off": o, "type": "hex", "len": 4}
           for o in range(0x158, 0x164, 4)]
        + [{"name": "cage", "off": 0x168, "type": "hex", "len": 4}]
    ),
}
MAX_ROWS = 20000
# No hardcoded roster path: the launcher always passes the configured one (Teams tab -> Browse),
# and a path baked in from one dev machine just made a shipped copy open nothing (or the wrong
# save) on every other machine.
DEFAULT_ROS = ""


class RosEditorFrame(ttk.Frame):
    def __init__(self, master, path=None):
        super().__init__(master)
        self.ros: RosFile | None = None
        self.chunk = None
        self.fielddefs = self._load_fielddefs()      # {hash_hex: [{name,off,type,len}]}
        self._edit_widget = None
        self._build()
        if path or (DEFAULT_ROS and Path(DEFAULT_ROS).exists()):
            self._open(path or DEFAULT_ROS)

    # ── field-def persistence ──
    def _load_fielddefs(self):
        try:
            return json.loads(FIELDS_JSON.read_text())
        except Exception:
            return {}

    def _save_fielddefs(self):
        try:
            FIELDS_JSON.write_text(json.dumps(self.fielddefs, indent=1))
        except Exception:
            pass

    def _defs(self):
        if not self.chunk:
            return []
        import copy
        if self.chunk.name not in self.fielddefs:           # seed pre-mapped fields on first open
            self.fielddefs[self.chunk.name] = copy.deepcopy(DEFAULT_FIELDS.get(self.chunk.name, []))
        else:
            # Anyone who has opened this table before already has a saved field list, so seeding
            # only on first open means newly mapped fields never reach them. Add the ones they are
            # missing (matched on offset, so a field they renamed themselves is left alone).
            have = {f.get("off") for f in self.fielddefs[self.chunk.name]}
            add = [f for f in DEFAULT_FIELDS.get(self.chunk.name, []) if f["off"] not in have]
            if add:
                self.fielddefs[self.chunk.name].extend(copy.deepcopy(add))
                self._save_fielddefs()
        return self.fielddefs[self.chunk.name]

    # ── UI ──
    def _build(self):
        top = ttk.Frame(self, padding=6); top.pack(fill=tk.X)
        ttk.Button(top, text="Open Roster.ROS…", command=self._browse).pack(side=tk.LEFT)
        self._path_lbl = ttk.Label(top, text="(no file)", foreground="#888"); self._path_lbl.pack(side=tk.LEFT, padx=8)
        self._save_btn = ttk.Button(top, text="Save", command=self._save, state=tk.DISABLED)
        self._save_btn.pack(side=tk.RIGHT)
        self._dirty_lbl = ttk.Label(top, text="", foreground="#e0a030"); self._dirty_lbl.pack(side=tk.RIGHT, padx=8)

        pane = ttk.Panedwindow(self, orient=tk.HORIZONTAL); pane.pack(fill=tk.BOTH, expand=True)
        # left: table list
        left = ttk.Frame(pane); pane.add(left, weight=1)
        ttk.Label(left, text="Tables", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, padx=6, pady=(4, 0))
        self._tables = ttk.Treeview(left, columns=("info",), show="tree", selectmode="browse")
        self._tables.column("#0", width=250)
        self._tables.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        self._tables.bind("<<TreeviewSelect>>", self._on_table)

        # right: fields + grid + hex
        right = ttk.Frame(pane); pane.add(right, weight=4)
        fb = ttk.LabelFrame(right, text="Fields (define columns at any offset)", padding=6); fb.pack(fill=tk.X, padx=6, pady=4)
        ttk.Label(fb, text="name").grid(row=0, column=0); ttk.Label(fb, text="offset").grid(row=0, column=2)
        ttk.Label(fb, text="type").grid(row=0, column=4); ttk.Label(fb, text="hex len").grid(row=0, column=6)
        self._f_name = ttk.Entry(fb, width=14); self._f_name.grid(row=0, column=1, padx=3)
        self._f_off = ttk.Entry(fb, width=8); self._f_off.grid(row=0, column=3, padx=3)
        self._f_type = ttk.Combobox(fb, values=TYPES, width=6, state="readonly"); self._f_type.set("u8"); self._f_type.grid(row=0, column=5, padx=3)
        self._f_len = ttk.Entry(fb, width=5); self._f_len.insert(0, "4"); self._f_len.grid(row=0, column=7, padx=3)
        ttk.Button(fb, text="Add field", command=self._add_field).grid(row=0, column=8, padx=6)
        ttk.Button(fb, text="Remove selected column", command=self._remove_field).grid(row=0, column=9)

        gridwrap = ttk.Frame(right); gridwrap.pack(fill=tk.BOTH, expand=True, padx=6)
        self._grid = ttk.Treeview(gridwrap, show="headings", selectmode="browse")
        ysb = ttk.Scrollbar(gridwrap, orient=tk.VERTICAL, command=self._grid.yview)
        xsb = ttk.Scrollbar(gridwrap, orient=tk.HORIZONTAL, command=self._grid.xview)
        self._grid.configure(yscroll=ysb.set, xscroll=xsb.set)
        ysb.pack(side=tk.RIGHT, fill=tk.Y); xsb.pack(side=tk.BOTTOM, fill=tk.X)
        self._grid.pack(fill=tk.BOTH, expand=True)
        self._grid.bind("<Double-1>", self._begin_edit)
        self._grid.bind("<<TreeviewSelect>>", self._show_hex)

        hb = ttk.LabelFrame(right, text="Selected record — raw bytes (offset : hex : ascii)", padding=4); hb.pack(fill=tk.X, padx=6, pady=4)
        self._hex = tk.Text(hb, height=9, font=("Consolas", 9), wrap=tk.NONE); self._hex.pack(fill=tk.X)

    # ── open / tables ──
    def _browse(self):
        p = filedialog.askopenfilename(title="Select Roster.ROS", filetypes=[("Roster save", "*.ROS"), ("All", "*.*")])
        if p:
            self._open(p)

    def _open(self, path):
        try:
            self.ros = RosFile(path)
        except Exception as e:
            messagebox.showerror("Open", f"Could not parse:\n{e}"); return
        self._path_lbl.config(text=str(path))
        self._save_btn.config(state=tk.NORMAL)
        self._tables.delete(*self._tables.get_children())
        recs = [c for c in self.ros.chunks if c.kind == "records"]
        blobs = [c for c in self.ros.chunks if c.kind != "records"]
        rt = self._tables.insert("", tk.END, text="── record tables ──", open=True)
        for c in sorted(recs, key=lambda c: -c.nrec):
            self._tables.insert(rt, tk.END, iid=f"c{c.index}", text=c.label())
        bt = self._tables.insert("", tk.END, text="── blobs ──", open=False)
        for c in blobs:
            self._tables.insert(bt, tk.END, iid=f"c{c.index}", text=c.label())
        self._refresh_dirty()

    def _on_table(self, _=None):
        sel = self._tables.selection()
        if not sel or not sel[0].startswith("c"):
            return
        idx = int(sel[0][1:])
        self.chunk = next(c for c in self.ros.chunks if c.index == idx)
        self._rebuild_grid()

    # ── grid ──
    def _rebuild_grid(self):
        if not self.chunk:
            return
        defs = self._defs()
        cols = ["#"] + [d["name"] for d in defs]
        self._grid["columns"] = cols
        self._grid.heading("#", text="row")
        self._grid.column("#", width=60, anchor=tk.E, stretch=False)
        for d in defs:
            self._grid.heading(d["name"], text=f"{d['name']} @{d['off']} {d['type']}")
            self._grid.column(d["name"], width=90, anchor=tk.W)
        self._grid.delete(*self._grid.get_children())
        c = self.chunk
        nrows = min(c.nrec, MAX_ROWS)
        for r in range(nrows):
            self._grid.insert("", tk.END, iid=str(r), values=[str(r)] + [self._cell(c, r, d) for d in defs])
        if c.nrec > MAX_ROWS:
            self._grid.insert("", tk.END, iid="_more", values=[f"… {c.nrec-MAX_ROWS} more rows not shown"] + [""] * len(defs))

    def _cell(self, c, row, d):
        try:
            if d["type"] == "hex":
                return self.ros.record(c, row)[d["off"]:d["off"] + int(d.get("len", 4))].hex()
            return self.ros.get(c, row, d["off"], FMT[d["type"]])
        except Exception:
            return "?"

    def _add_field(self):
        if not self.chunk:
            return
        name = self._f_name.get().strip() or f"f{len(self._defs())}"
        try:
            off = int(self._f_off.get().strip(), 0)
        except Exception:
            messagebox.showerror("Field", "Offset must be a number (e.g. 12 or 0x0C)."); return
        typ = self._f_type.get()
        d = {"name": name, "off": off, "type": typ}
        if typ == "hex":
            try: d["len"] = max(1, int(self._f_len.get()))
            except Exception: d["len"] = 4
        self._defs().append(d); self._save_fielddefs(); self._rebuild_grid()

    def _remove_field(self):
        # remove the column of the currently focused cell (or the last field)
        defs = self._defs()
        if not defs:
            return
        col = getattr(self, "_last_col_name", None)
        idx = next((i for i, d in enumerate(defs) if d["name"] == col), len(defs) - 1)
        defs.pop(idx); self._save_fielddefs(); self._rebuild_grid()

    # ── inline editing ──
    def _begin_edit(self, ev):
        self._destroy_editor()
        row = self._grid.identify_row(ev.y); col = self._grid.identify_column(ev.x)
        if not row or row == "_more" or col == "#1":       # #1 is the row-index column
            return
        ci = int(col[1:]) - 2                               # 0-based index into defs
        defs = self._defs()
        if ci < 0 or ci >= len(defs):
            return
        self._last_col_name = defs[ci]["name"]
        x, y, w, h = self._grid.bbox(row, col)
        cur = self._grid.set(row, self._grid["columns"][int(col[1:]) - 1])
        e = ttk.Entry(self._grid); e.place(x=x, y=y, width=w, height=h)
        e.insert(0, str(cur)); e.select_range(0, tk.END); e.focus_set()
        e.bind("<Return>", lambda _e: self._commit_edit(row, ci, e.get()))
        e.bind("<Escape>", lambda _e: self._destroy_editor())
        self._edit_widget = e

    def _commit_edit(self, row, ci, text):
        c = self.chunk; d = self._defs()[ci]; r = int(row)
        try:
            if d["type"] == "hex":
                b = bytes.fromhex(text.strip())
                self.ros.set_bytes(c, r, d["off"], b[:int(d.get("len", 4))])
            elif d["type"] == "f32":
                self.ros.set(c, r, d["off"], FMT["f32"], float(text))
            else:
                self.ros.set(c, r, d["off"], FMT[d["type"]], int(text.strip(), 0))
        except Exception as e:
            messagebox.showerror("Edit", f"Bad value: {e}"); return
        self._destroy_editor()
        self._grid.set(row, self._grid["columns"][ci + 1], self._cell(c, r, d))
        self._refresh_dirty(); self._show_hex()

    def _destroy_editor(self):
        if self._edit_widget:
            self._edit_widget.destroy(); self._edit_widget = None

    def _show_hex(self, _=None):
        sel = self._grid.selection()
        if not self.chunk or not sel or sel[0] == "_more":
            return
        r = int(sel[0]); rec = self.ros.record(self.chunk, r)
        self._hex.delete("1.0", tk.END)
        for o in range(0, len(rec), 16):
            chunk = rec[o:o + 16]
            hexs = " ".join(f"{b:02X}" for b in chunk)
            asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            self._hex.insert(tk.END, f"{o:04X}  {hexs:<48}  {asc}\n")

    # ── save ──
    def _refresh_dirty(self):
        try:
            dirty = self.ros and self.ros.data != self.ros.path.read_bytes()
        except Exception:
            dirty = True
        self._dirty_lbl.config(text="● unsaved changes" if dirty else "")

    def _save(self):
        if not self.ros:
            return
        try:
            self.ros.save()
        except Exception as e:
            messagebox.showerror("Save", str(e)); return
        self._refresh_dirty()
        messagebox.showinfo("Save", f"Saved {self.ros.path.name}\n(backup: {self.ros.path.name}.bak)\n\n"
                            "Reload the roster in-game to see changes.")


def open_editor(parent, path=None):
    """Open the ROS editor as a Toplevel window over `parent` (launcher integration)."""
    win = tk.Toplevel(parent)
    win.title("NHL 2K10 — Roster.ROS Editor")
    win.geometry("1280x820")
    RosEditorFrame(win, path).pack(fill=tk.BOTH, expand=True)
    return win


if __name__ == "__main__":
    root = tk.Tk()
    root.title("NHL 2K10 — Roster.ROS Editor")
    root.geometry("1280x820")
    RosEditorFrame(root, sys.argv[1] if len(sys.argv) > 1 else None).pack(fill=tk.BOTH, expand=True)
    root.mainloop()
