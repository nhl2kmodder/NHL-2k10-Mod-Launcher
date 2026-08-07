"""
player_editor_gui.py — the Players grid of the Roster Editor tab, backed by the Roster.ROS FILE.

This is the static twin of `ros_live_editor.LiveEditorFrame`, and it exists because the live one
turned out to be unnecessary. The on-disk player record is the SAME struct as the in-memory record
(see project_ros_disk_memory_delta): once `player_assign.PlayerTable` was reframed onto the true
record base, every offset the live editor mapped out of Ghidra — portrait +0x1C, position +0x40,
ratings +0x52.., mask +0xB4/+0xB8, recolours +0x158.. — addresses the same bytes in the file. So
the whole field map is reused verbatim from `ros_live_editor.KNOWN_FIELDS` rather than copied: one
place to fix when a new field is mapped.

What that buys, and why this replaces the live editor for everyday work:
  · no running game, no Xenia attach, no "get to the main menu first";
  · edits are ordinary file writes with a .bak, so they survive without an in-game roster save —
    and they ride in a .n2kpack, which live memory writes never could;
  · it works on a roster the user just downloaded, before they have ever booted it.
The live editor is still the right tool for watching a value change while the game runs, and the
Team Rosters editor still needs live memory (it rewrites pointer slots in the team table), so
neither is removed — but neither is where you edit a player any more.

Names are read-only here for the same reason they are in the live editor: they are self-relative
pointers into a shared string pool, so a longer name needs the pool repointing (the machinery
exists for TEAM names in roster_editor.py; players have ~5,000 of them and no slack).
"""
import struct
import threading
from collections import OrderedDict
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from .player_assign import PlayerTable, EYE_COLORS, OFF_HEAD, OFF_EYES, EYE_SHIFT, EYE_MASK
from .ros_live_editor import KNOWN_FIELDS
try:
    from launcher import archive_textures as AT
except ImportError:                                   # standalone run
    import archive_textures as AT
try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

# The two appearance fields the live editor never had, in their own group so they sit next to the
# portrait rather than being lost among 40 ratings. Head is a u16 (the only one in the record that
# matters here), eyes are two bits sharing the goalie-mask dword — the adapter's read-modify-write
# is what keeps a mask pattern from being wiped by an eye-colour change.
APPEARANCE = [
    {"name": "Head asset", "off": OFF_HEAD, "type": "u16", "group": "Appearance"},
    {"name": "Eye colour", "off": OFF_EYES, "type": "enum", "lo": EYE_SHIFT, "w": 2,
     "map": {i: c for i, c in enumerate(EYE_COLORS)}, "group": "Appearance"},
]
FIELDS = APPEARANCE + list(KNOWN_FIELDS)


class DiskRoster:
    """Adapter giving `PlayerTable` the get/set shape `KNOWN_FIELDS` was written against.

    Deliberately mirrors `ros_live_editor.LiveRoster` method for method, so the form-rendering code
    below is the same code in both editors and a field added to KNOWN_FIELDS shows up in both.
    The one behavioural difference: `set` cannot fail on a memory write, so it returns an error
    string only for a bad VALUE, and nothing reaches the file until `save()`."""

    def __init__(self, path):
        self.path = str(path)
        self.t = PlayerTable(path)
        self.cnt = self.t.nrec

    # ── reads ──
    def names(self, i):
        return self.t.name(i)                          # (first, last)

    def team(self, i):
        return self.t.team(i)

    def rec(self, i):
        o = self.t.base + i * 420
        return bytes(self.t.ros.data[o:o + 420])

    def _u32(self, i, off):
        return self.t._u32(i, off)

    def get(self, i, f):
        t = f["type"]
        if t == "name":
            fn, ln = self.names(i)
            return fn if f["off"] == 4 else ln
        if t == "u16":
            return self.t._u16(i, f["off"])
        if t == "u8":
            return self.rec(i)[f["off"]]
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
        return v

    # ── writes ──
    def set(self, i, f, text):
        t = f["type"]
        if t == "name":
            return ("player names are pointers into a shared string pool — editing them needs the "
                    "pool repointed, which isn't wired up yet")
        if f.get("readonly"):
            return "this field is read-only (the game computes it)"
        try:
            if t == "u16":
                self.t._set_u16(i, f["off"], int(text, 0) & 0xFFFF)
                return None
            if t == "u8":
                o = self.t._off(i, f["off"])
                self.t.ros.data[o] = int(text, 0) & 0xFF
                return None
            cur = self._u32(i, f["off"])
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
            else:
                nv = int(text, 0) & 0xFFFFFFFF
        except Exception as e:
            return f"bad value: {e}"
        self.t._set_u32(i, f["off"], nv)
        return None

    def dirty(self):
        return self.t.dirty()

    def save(self):
        self.t.save()


class PlayerEditorFrame(ttk.Frame):
    """Left: every row in the save, searchable. Right: portrait, appearance, fields, raw bytes."""

    _PORT_SIZE = 148

    def __init__(self, master, path=None, game_root=None, on_edit_head=None):
        super().__init__(master)
        self.roster = None
        self.game_root = str(game_root) if game_root else None
        # The launcher passes a callback that opens the head editor for (row, table); left None in
        # a standalone run, in which case the button simply isn't shown.
        self._on_edit_head = on_edit_head
        self.fields = list(FIELDS)
        self.sel = None
        self._port_photo = None
        self._port_token = None
        self._build()
        if path:
            self.open(path)

    # ── UI ──
    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill=tk.X)
        ttk.Button(top, text="Reload", command=self._reload).pack(side=tk.LEFT)
        self._status = ttk.Label(top, text="no roster loaded", foreground="#888")
        self._status.pack(side=tk.LEFT, padx=10)
        self._save_btn = ttk.Button(top, text="Save to Roster.ROS", command=self._save,
                                    state=tk.DISABLED)
        self._save_btn.pack(side=tk.RIGHT)
        self._dirty_lbl = ttk.Label(top, text="", foreground="#e0a030")
        self._dirty_lbl.pack(side=tk.RIGHT, padx=8)

        pane = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(pane)
        pane.add(left, weight=1)
        fb = ttk.Frame(left, padding=(6, 4))
        fb.pack(fill=tk.X)
        ttk.Label(fb, text="Find:").pack(side=tk.LEFT)
        self._q = tk.StringVar()
        self._q.trace_add("write", lambda *_: self._fill_players())
        ttk.Entry(fb, textvariable=self._q).pack(side=tk.LEFT, fill=tk.X, expand=True)
        # The save holds ~2,700 rows and only a few hundred are NHL players; the rest are minor
        # leaguers, national teams and template rows. Default to hiding them — the Team column is
        # what makes the difference visible, so the filter is honest rather than magic.
        self._nhl_only = tk.BooleanVar(value=True)
        ttk.Checkbutton(fb, text="NHL only", variable=self._nhl_only,
                        command=self._fill_players).pack(side=tk.LEFT, padx=(6, 0))

        cols = ("num", "pos", "team")
        self._plist = ttk.Treeview(left, columns=cols, show="tree headings", selectmode="browse")
        self._plist.heading("#0", text="Player")
        self._plist.column("#0", width=160)
        for c, w, t in (("num", 42, "Row"), ("pos", 40, "Pos"), ("team", 62, "Team")):
            self._plist.heading(c, text=t)
            self._plist.column(c, width=w, anchor=tk.CENTER)
        psb = ttk.Scrollbar(left, command=self._plist.yview)
        self._plist.configure(yscroll=psb.set)
        psb.pack(side=tk.RIGHT, fill=tk.Y)
        self._plist.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        self._plist.bind("<<TreeviewSelect>>", self._on_player)

        right = ttk.Frame(pane)
        pane.add(right, weight=3)
        pf = ttk.LabelFrame(right, text="Portrait & head — as currently in the game files "
                                        "(mods included)")
        pf.pack(fill=tk.X, padx=6, pady=(4, 0))
        self._port_img = ttk.Label(pf, text="—", anchor=tk.CENTER)
        self._port_img.pack(side=tk.LEFT, padx=4, pady=4)
        side = ttk.Frame(pf)
        side.pack(side=tk.LEFT, padx=8, fill=tk.BOTH)
        self._port_info = ttk.Label(side, text="select a player", foreground="#888",
                                    justify=tk.LEFT)
        self._port_info.pack(anchor=tk.W)
        self._head_info = ttk.Label(side, text="", foreground="#888", justify=tk.LEFT)
        self._head_info.pack(anchor=tk.W, pady=(4, 0))
        if self._on_edit_head is not None:
            self._head_btn = ttk.Button(side, text="Edit head…", command=self._edit_head,
                                        state=tk.DISABLED)
            self._head_btn.pack(anchor=tk.W, pady=(6, 0))
        else:
            self._head_btn = None

        fw = ttk.LabelFrame(right, text="Fields (Enter to apply — nothing is written until Save)")
        fw.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        cv = tk.Canvas(fw, highlightthickness=0)
        fsb = ttk.Scrollbar(fw, orient=tk.VERTICAL, command=cv.yview)
        cv.configure(yscrollcommand=fsb.set)
        fsb.pack(side=tk.RIGHT, fill=tk.Y)
        cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._form = ttk.Frame(cv, padding=6)
        _win = cv.create_window((0, 0), window=self._form, anchor="nw")
        self._form.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfig(_win, width=e.width))
        cv.bind("<Enter>", lambda e: cv.bind_all(
            "<MouseWheel>", lambda ev: cv.yview_scroll(int(-ev.delta / 120), "units")))
        cv.bind("<Leave>", lambda e: cv.unbind_all("<MouseWheel>"))

        hb = ttk.LabelFrame(right, text="Selected record — raw bytes", padding=4)
        hb.pack(fill=tk.X, padx=6, pady=4)
        self._hex = tk.Text(hb, height=7, font=("Consolas", 9), wrap=tk.NONE)
        self._hex.pack(fill=tk.X)

    # ── open / reload / save ──
    def open(self, path):
        try:
            self.roster = DiskRoster(path)
        except Exception as e:
            self.roster = None
            self._status.config(text=str(e), foreground="#e06060")
            self._plist.delete(*self._plist.get_children())
            self._save_btn.config(state=tk.DISABLED)
            return
        self._path = str(path)
        self._status.config(text=f"{self.roster.cnt} records — {Path(path).name}",
                            foreground="#8c8")
        self._save_btn.config(state=tk.NORMAL)
        self.sel = None
        self._fill_players()
        self._refresh_dirty()

    def _reload(self):
        p = getattr(self, "_path", None)
        if not p:
            p = filedialog.askopenfilename(title="Select Roster.ROS",
                                           filetypes=[("Roster save", "*.ROS"), ("All", "*.*")])
            if not p:
                return
        if self.roster is not None and self.roster.dirty():
            if not messagebox.askyesno("Reload", "Discard unsaved edits and reload?", parent=self):
                return
        self.open(p)

    def _save(self):
        if not self.roster:
            return
        try:
            self.roster.save()
        except Exception as e:
            messagebox.showerror("Save", f"Could not save:\n{e}", parent=self)
            return
        self._refresh_dirty()
        messagebox.showinfo("Save", "Roster.ROS written (a .bak was made).\n\n"
                                    "Load the save in-game to see it — do NOT have the game "
                                    "running with this roster loaded, or it will write its own "
                                    "copy back over yours on exit.", parent=self)

    def _refresh_dirty(self):
        d = bool(self.roster and self.roster.dirty())
        self._dirty_lbl.config(text="unsaved edits" if d else "")

    # ── player list ──
    def _fill_players(self):
        if not self.roster:
            return
        q = self._q.get().strip().lower()
        nhl = self._nhl_only.get()
        self._plist.delete(*self._plist.get_children())
        pos_f = next(f for f in KNOWN_FIELDS if f["name"] == "Position")
        for i in range(self.roster.cnt):
            fn, ln = self.roster.names(i)
            nm = (fn + " " + ln).strip()
            if not nm:
                continue
            city, nick, code = self.roster.team(i)
            # "Minors" and "Team <country>" are the two shapes the non-NHL clubs take; a row with
            # no club at all is a template/free-agent placeholder and is hidden with them.
            if nhl and ("Minors" in nick or city == "Team" or not code):
                continue
            if q and q not in nm.lower() and q not in code.lower() and q not in nick.lower():
                continue
            self._plist.insert("", tk.END, iid=str(i), text=nm,
                               values=(i, self.roster.get(i, pos_f), code))
        n = len(self._plist.get_children())
        self._status.config(text=f"{n} shown of {self.roster.cnt} records — "
                                 f"{Path(self._path).name}", foreground="#8c8")

    def _on_player(self, _=None):
        sel = self._plist.selection()
        if not sel:
            return
        self.sel = int(sel[0])
        self._render_form()
        self._render_hex()
        self._update_portrait()

    # ── form ──
    def _render_form(self):
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
                      foreground="#4a9").grid(row=row, column=0, columnspan=4, sticky=tk.W,
                                              pady=(9, 2))
            row += 1
            for i, f in enumerate(gfields):
                r = row + i // 2
                col = (i % 2) * 2
                ttk.Label(self._form, text=f["name"]).grid(row=r, column=col, sticky=tk.W,
                                                           pady=1, padx=(4, 2))
                v = tk.StringVar(value=str(self.roster.get(self.sel, f)))
                if f["type"] == "enum":
                    labels = [f["map"][k] for k in sorted(f["map"])]
                    e = ttk.Combobox(self._form, textvariable=v, values=labels, width=15,
                                     state="readonly")
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
            messagebox.showerror("Edit", err, parent=self)
        var.set(str(self.roster.get(self.sel, f)))
        self._render_hex()
        self._refresh_dirty()
        if f["name"] == "Portrait ID":
            self._update_portrait()
        elif f["name"] == "Head asset":
            self._update_head_info()

    def _render_hex(self):
        if self.sel is None or not self.roster:
            return
        rec = self.roster.rec(self.sel)
        self._hex.delete("1.0", tk.END)
        for o in range(0, len(rec), 16):
            c = rec[o:o + 16]
            self._hex.insert(tk.END, f"{o:03X}  " + " ".join(f"{b:02X}" for b in c) + "   " +
                             "".join(chr(b) if 32 <= b < 127 else "." for b in c) + "\n")

    # ── portrait + head ──
    def _update_head_info(self):
        if self.sel is None or not self.roster:
            return
        hid = self.roster.t.head(self.sel)
        shared = self.roster.t.head_usage().get(hid, [])
        who = ("used by this player only" if len(shared) == 1 else
               f"SHARED with {len(shared) - 1} other player(s)")
        self._head_info.config(text=f"Head asset {hid} — {who}",
                               foreground="#8c8" if len(shared) == 1 else "#e0a030")
        if self._head_btn is not None:
            self._head_btn.config(state=tk.NORMAL)

    def _edit_head(self):
        if self.sel is None or not self.roster or self._on_edit_head is None:
            return
        # The editor gets the live PlayerTable, so its head-id and eye-colour writes are edits to
        # the roster THIS grid saves — no second parse, no chance of the two disagreeing. It calls
        # `refresh_selected` back when it changes one.
        self._on_edit_head(self.roster.t, self.sel, self.refresh_selected)

    def refresh_selected(self):
        """Re-read the selected row — for when another window has edited it."""
        if self.sel is None or not self.roster:
            return
        self._render_form()
        self._render_hex()
        self._update_head_info()
        self._refresh_dirty()

    def _update_portrait(self):
        """Decode the selected player's portrait off-thread — the first call reads the 66MB pack."""
        if self.sel is None or not self.roster:
            return
        self._update_head_info()
        key = int(self.roster.t.portrait(self.sel))
        self._port_token = tok = object()
        if ImageTk is None:
            self._port_info.config(text="Pillow is not installed — no preview", foreground="#e06060")
            return
        self._port_info.config(text=f"Portrait ID {key} — loading…", foreground="#888")

        def work():
            img = err = blob = None
            try:
                blob = AT.portrait_key_blob_map().get(key)
                if blob is None:
                    err = f"Portrait ID {key}: no portrait with this ID"
                else:
                    img = AT.decode_portrait_current(blob)
                    if img is None:
                        err = f"Portrait ID {key} -> #{blob}: decode failed"
            except Exception as e:
                err = str(e)

            def done():
                if tok is not self._port_token:        # a newer selection superseded this decode
                    return
                if img is not None:
                    ph = ImageTk.PhotoImage(img.resize((self._PORT_SIZE,) * 2, Image.LANCZOS))
                    self._port_photo = ph
                    self._port_img.config(image=ph, text="")
                    self._port_info.config(text=f"Portrait ID {key} -> portrait #{blob}",
                                           foreground="#8c8")
                else:
                    self._port_photo = None
                    self._port_img.config(image="", text="—")
                    self._port_info.config(text=err or "?", foreground="#e0a030")
            try:
                self.after(0, done)
            except tk.TclError:                        # window closed while decoding
                pass
        threading.Thread(target=work, daemon=True).start()


def open_editor(parent, path=None, game_root=None):
    win = tk.Toplevel(parent)
    win.title("NHL 2K10 — Players (Roster.ROS)")
    win.geometry("1120x780")
    PlayerEditorFrame(win, path, game_root).pack(fill=tk.BOTH, expand=True)
    return win
