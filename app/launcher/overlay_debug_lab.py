"""NHL 2K10 Overlay Debug Lab — a sandbox to inspect and EDIT every field of the overlay/scene
element records, both PERMANENTLY (file -> re-encode + relocate) and LIVE (write Xenia RAM for
instant feedback). Designed for experimentation: tweak anything, see what changes, break/crash,
then Restore-from-Clean to recover.

Run:  python overlay_debug_lab.py     (standalone; reuses the launcher modules)

Sections (tabs):
  • File Records  — scan an IFF for per-element transform+color records (0x80) or activation
                    records (0x40), edit any field, Apply to File (permanent) or Restore.
  • Live Memory   — attach to a running Xenia, read/write any guest address (VA or physical),
                    with presets for the known scorebug structures. Instant, transient.
  • Raw DRAM Poke — write ANY bytes at ANY offset of an IFF's decompressed DRAM, then relocate.
                    Maximum power / maximum break potential.
WARNING: edits relocate the IFF and grow archive 1B. Restore-from-Clean undoes it. Back up first.
"""
import sys, struct, traceback
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

_HERE = Path(__file__).resolve().parent
for p in (str(_HERE), str(_HERE.parents[1])):
    if p not in sys.path:
        sys.path.insert(0, p)

import archive_textures as at
import overlay_editor as oe
try:
    import xenia_mem as xm
except Exception:
    xm = None
try:
    import xex_patch as xp
except Exception:
    xp = None

GAME_DIR = Path(r"C:\Users\cloug\Documents\NHL 2k10 Extracted")
VA_HOST_OFFSET   = 0x100000000     # guest VA  -> host:  VA  + this
PHYS_HOST_OFFSET = 0x1A0000000     # guest phys-> host:  PHYS + this (xenia_mem.PHYS_BASE)

# overlay IFFs worth poking
IFFS = ["overlay_static.iff", "overlay_static_skills.iff", "overlay_wipes.iff",
        "frontend.iff", "titlepage.iff", "global.iff"]

# 0x80 transform+color record field map (offset -> (label, kind))
TC_FIELDS = [
    (0x14, "id",     "hex"),
    (0x1C, "X pos",  "f"),
    (0x20, "Y pos",  "f"),
    (0x30, "R",      "f"),
    (0x34, "G",      "f"),
    (0x38, "B",      "f"),
    (0x3C, "A",      "f"),
    (0x40, "skew?",  "f"),
    (0x44, "scaleA?", "f"),
    (0x4C, "scaleB?", "f"),
]
# 0x40 activation record field map
ACT_FIELDS = [
    (0x00, "id",    "hex"),
    (0x18, "a",     "i"),
    (0x1C, "b",     "i"),
    (0x28, "scale", "f"),
    (0x3C, "type",  "i"),
]

# Live presets: (label, kind 'va'/'phys', guest_addr, type)
LIVE_PRESETS = [
    ("Anchor table DAT_8499ef10 (VA)",        "va",   0x8499EF10, "bytes16"),
    ("Anchor mode-7 entry 0x8499ef48 (VA)",   "va",   0x8499EF48, "2int"),
    ("Scorebug activation list (phys, may move)", "phys", 0x16233458, "bytes64"),
]


class DebugLab(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NHL 2K10 Overlay Debug Lab")
        self.geometry("1100x740")
        self.handle = None
        self.phys_base = PHYS_HOST_OFFSET
        self._dram = None; self._meta = None; self._iff = None
        self._records = []
        self._field_vars = {}
        self._live_rec_addr = {}      # file record offset -> found live host address (cache)
        self._build()

    # ---------- UI ----------
    def _build(self):
        top = ttk.Frame(self, padding=6); top.pack(fill=tk.X)
        ttk.Label(top, text="Game dir:").pack(side=tk.LEFT)
        self.v_game = tk.StringVar(value=str(GAME_DIR))
        ttk.Entry(top, textvariable=self.v_game, width=60).pack(side=tk.LEFT, padx=4)
        self.v_xenia = tk.StringVar(value="Xenia: not attached")
        ttk.Button(top, text="Attach Xenia", command=self._attach).pack(side=tk.LEFT, padx=4)
        ttk.Label(top, textvariable=self.v_xenia, foreground="#888").pack(side=tk.LEFT, padx=4)

        nb = ttk.Notebook(self); nb.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        self._tab_records(nb)
        self._tab_live(nb)
        self._tab_raw(nb)

        self.log = tk.Text(self, height=9, bg="#111", fg="#9f9", font=("Consolas", 9))
        self.log.pack(fill=tk.X, padx=6, pady=4)
        self._log("Ready. WARNING: file edits relocate the IFF (grow 1B). Use Restore-from-Clean to undo.")

    def _tab_records(self, nb):
        t = ttk.Frame(nb); nb.add(t, text="File Records")
        bar = ttk.Frame(t, padding=4); bar.pack(fill=tk.X)
        ttk.Label(bar, text="IFF:").pack(side=tk.LEFT)
        self.v_iff = tk.StringVar(value=IFFS[0])
        ttk.Combobox(bar, textvariable=self.v_iff, values=IFFS, width=24, state="readonly").pack(side=tk.LEFT, padx=4)
        ttk.Label(bar, text="Scan:").pack(side=tk.LEFT)
        self.v_scan = tk.StringVar(value="Transform+Color (0x80)")
        ttk.Combobox(bar, textvariable=self.v_scan, width=24, state="readonly",
                     values=["Transform+Color (0x80)", "Activation (0x40)"]).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Scan", command=self._scan).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Restore IFF from Clean", command=self._restore).pack(side=tk.RIGHT, padx=4)

        body = ttk.Frame(t); body.pack(fill=tk.BOTH, expand=True)
        cols = ("off", "id", "summary")
        self.tv = ttk.Treeview(body, columns=cols, show="headings", height=20)
        for c, w in (("off", 90), ("id", 110), ("summary", 360)):
            self.tv.heading(c, text=c); self.tv.column(c, width=w)
        self.tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tv.bind("<<TreeviewSelect>>", self._on_select)
        sb = ttk.Scrollbar(body, command=self.tv.yview); sb.pack(side=tk.LEFT, fill=tk.Y)
        self.tv.config(yscrollcommand=sb.set)

        self.detail = ttk.LabelFrame(body, text="Edit fields", padding=6)
        self.detail.pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(self.detail, text="Apply to File (permanent + relocate)",
                   command=self._apply_record).pack(side=tk.BOTTOM, fill=tk.X, pady=2)
        liverow = ttk.Frame(self.detail); liverow.pack(side=tk.BOTTOM, fill=tk.X, pady=2)
        ttk.Button(liverow, text="Find Live", command=self._find_live).pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(liverow, text="Write Live", command=self._write_live_record).pack(side=tk.LEFT, expand=True, fill=tk.X)
        self.v_livestat = tk.StringVar(value="live: (use Find Live to locate this record in RAM)")
        ttk.Label(self.detail, textvariable=self.v_livestat, foreground="#888", wraplength=220).pack(side=tk.BOTTOM, fill=tk.X)
        self.fields_frame = ttk.Frame(self.detail); self.fields_frame.pack(fill=tk.BOTH, expand=True)

    def _tab_live(self, nb):
        t = ttk.Frame(nb); nb.add(t, text="Live Memory")
        f = ttk.Frame(t, padding=8); f.pack(fill=tk.X)
        ttk.Label(f, text="Guest address (hex):").grid(row=0, column=0, sticky="w")
        self.v_addr = tk.StringVar(value="8499EF48")
        ttk.Entry(f, textvariable=self.v_addr, width=16).grid(row=0, column=1, padx=4)
        self.v_space = tk.StringVar(value="va")
        ttk.Radiobutton(f, text="VA", variable=self.v_space, value="va").grid(row=0, column=2)
        ttk.Radiobutton(f, text="Phys", variable=self.v_space, value="phys").grid(row=0, column=3)
        ttk.Label(f, text="Type:").grid(row=1, column=0, sticky="w")
        self.v_ltype = tk.StringVar(value="2int")
        ttk.Combobox(f, textvariable=self.v_ltype, width=10, state="readonly",
                     values=["int32", "float", "rgba", "2int", "bytes16", "bytes64"]).grid(row=1, column=1, padx=4)
        ttk.Label(f, text="Value:").grid(row=1, column=2, sticky="w")
        self.v_lval = tk.StringVar()
        ttk.Entry(f, textvariable=self.v_lval, width=40).grid(row=1, column=3, columnspan=3, padx=4, sticky="w")
        ttk.Button(f, text="Read", command=self._live_read).grid(row=2, column=1, pady=4)
        ttk.Button(f, text="Write (live)", command=self._live_write).grid(row=2, column=2, pady=4)
        ttk.Button(f, text="Apply to XEX (permanent)", command=self._xex_write).grid(row=2, column=3, pady=4, sticky="w")

        pf = ttk.LabelFrame(t, text="Presets (double-click to load address)", padding=4)
        pf.pack(fill=tk.BOTH, expand=True, padx=8)
        self.preset_lb = tk.Listbox(pf, height=8)
        for lbl, *_ in LIVE_PRESETS:
            self.preset_lb.insert(tk.END, lbl)
        self.preset_lb.pack(fill=tk.BOTH, expand=True)
        self.preset_lb.bind("<Double-Button-1>", self._load_preset)
        ttk.Label(t, foreground="#c80", text=(
            "Big-endian values are written for you. Live edits are TRANSIENT (lost on relaunch / "
            "relayout). Anchor table @8499ef48 = (x_type,y_type): write 00000005 00000003 = top-right.")
        ).pack(fill=tk.X, padx=8, pady=4)

    def _tab_raw(self, nb):
        t = ttk.Frame(nb); nb.add(t, text="Raw DRAM Poke")
        f = ttk.Frame(t, padding=8); f.pack(fill=tk.X)
        ttk.Label(f, text="IFF:").grid(row=0, column=0, sticky="w")
        self.v_riff = tk.StringVar(value=IFFS[0])
        ttk.Combobox(f, textvariable=self.v_riff, values=IFFS, width=24, state="readonly").grid(row=0, column=1, padx=4)
        ttk.Label(f, text="DRAM offset (hex):").grid(row=1, column=0, sticky="w")
        self.v_roff = tk.StringVar(value="256BD8")
        ttk.Entry(f, textvariable=self.v_roff, width=16).grid(row=1, column=1, padx=4, sticky="w")
        ttk.Label(f, text="Type:").grid(row=1, column=2, sticky="w")
        self.v_rtype = tk.StringVar(value="float")
        ttk.Combobox(f, textvariable=self.v_rtype, width=10, state="readonly",
                     values=["int32", "float", "hexbytes"]).grid(row=1, column=3, padx=4)
        ttk.Label(f, text="Value:").grid(row=2, column=0, sticky="w")
        self.v_rval = tk.StringVar()
        ttk.Entry(f, textvariable=self.v_rval, width=40).grid(row=2, column=1, columnspan=3, padx=4, sticky="w")
        ttk.Button(f, text="Read", command=self._raw_read).grid(row=3, column=1, pady=4)
        ttk.Button(f, text="Poke + Apply (permanent + relocate)", command=self._raw_poke).grid(row=3, column=2, columnspan=2, pady=4)
        ttk.Label(t, foreground="#c80", text=(
            "Writes ANY value at ANY DRAM offset, then re-encodes + relocates the IFF. This is the "
            "'break anything' tool — Restore-from-Clean (File Records tab) recovers.")).pack(fill=tk.X, padx=8, pady=6)

    # ---------- helpers ----------
    def _log(self, msg):
        self.log.insert(tk.END, str(msg) + "\n"); self.log.see(tk.END); self.update_idletasks()

    def _gd(self):
        return Path(self.v_game.get())

    def _attach(self):
        if xm is None:
            messagebox.showerror("Live", "xenia_mem not available"); return
        pid = xm.find_pid()
        if not pid:
            self.v_xenia.set("Xenia: NOT FOUND (launch the game first)"); return
        try:
            self.handle = xm.open_process(pid)
            self.phys_base = xm.find_phys_base(self.handle) or PHYS_HOST_OFFSET
            self.v_xenia.set(f"Xenia: attached pid={pid} phys_base=0x{self.phys_base:X}")
            self._log(f"Attached to Xenia pid={pid}, phys_base=0x{self.phys_base:X}")
        except Exception as e:
            self.v_xenia.set("Xenia: attach failed (need Admin)")
            msg = str(e)
            if "error 5" in msg or "Access" in msg:
                msg += ("\n  → ACCESS DENIED: Xenia runs elevated, so this tool must too. "
                        "Close it and relaunch — accept the UAC prompt — or run your terminal "
                        "'As administrator'. (File-edit tabs work without admin.)")
            self._log(f"attach error: {msg}")

    def _host_addr(self, guest, space):
        return guest + (PHYS_HOST_OFFSET if space == "phys" else VA_HOST_OFFSET) \
               if space != "phys" else guest + self.phys_base

    # ---------- File Records ----------
    def _scan(self):
        try:
            self._iff = self.v_iff.get()
            self._dram, self._meta = oe.load_dram(self._iff, self._gd())
            self.tv.delete(*self.tv.get_children())
            self._records = []
            if self.v_scan.get().startswith("Transform"):
                self._scan_tc()
            else:
                self._scan_act()
            self._log(f"{self._iff}: {len(self._records)} records found ({self.v_scan.get()})")
        except Exception as e:
            self._log(f"scan error: {e}\n{traceback.format_exc()}")

    def _scan_tc(self):
        dram = self._dram
        for o in range(0, len(dram) - 0x80, 4):
            if not oe._is_rec(dram, o):
                continue
            idv = struct.unpack_from(">I", dram, o + 0x14)[0]
            x, y = struct.unpack_from(">f", dram, o + 0x1C)[0], struct.unpack_from(">f", dram, o + 0x20)[0]
            r, g, b, a = struct.unpack_from(">4f", dram, o + 0x30)
            self._records.append((o, "tc"))
            self.tv.insert("", tk.END, iid=str(o), values=(
                f"0x{o:X}", f"0x{idv:08X}",
                f"pos({x:.0f},{y:.0f}) rgba({r:.2f},{g:.2f},{b:.2f},{a:.2f})"))

    def _scan_act(self):
        dram = self._dram
        # find the scorebug activation array by its root id, list 0x40 records
        root = dram.find(struct.pack(">I", 0x523CBCEB))
        if root < 0:
            self._log("activation root id not found in this iff"); return
        o = root
        while o + 0x40 <= len(dram):
            idv = struct.unpack_from(">I", dram, o)[0]
            t = struct.unpack_from(">I", dram, o + 0x3C)[0]
            if t != 3 and idv not in (0,):
                break
            a, b = struct.unpack_from(">I", dram, o + 0x18)[0], struct.unpack_from(">I", dram, o + 0x1C)[0]
            sc = struct.unpack_from(">f", dram, o + 0x28)[0]
            self._records.append((o, "act"))
            self.tv.insert("", tk.END, iid=str(o), values=(
                f"0x{o:X}", f"0x{idv:08X}", f"a={a} b={b} scale={sc:.2f} type={t}"))
            o += 0x40

    def _on_select(self, _e):
        sel = self.tv.selection()
        if not sel:
            return
        o = int(sel[0])
        kind = dict(self._records).get(o, "tc")
        fields = TC_FIELDS if kind == "tc" else ACT_FIELDS
        for w in self.fields_frame.winfo_children():
            w.destroy()
        self._field_vars = {}
        ttk.Label(self.fields_frame, text=f"record @0x{o:X}", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        for i, (foff, lbl, kindf) in enumerate(fields):
            ttk.Label(self.fields_frame, text=lbl).grid(row=i+1, column=0, sticky="w", pady=1)
            if kindf == "f":
                val = struct.unpack_from(">f", self._dram, o + foff)[0]; s = f"{val:.4f}"
            elif kindf == "i":
                val = struct.unpack_from(">I", self._dram, o + foff)[0]; s = str(val)
            else:
                val = struct.unpack_from(">I", self._dram, o + foff)[0]; s = f"{val:08X}"
            var = tk.StringVar(value=s)
            e = ttk.Entry(self.fields_frame, textvariable=var, width=14)
            e.grid(row=i+1, column=1, padx=4, pady=1)
            self._field_vars[foff] = (var, kindf)

    def _pack_field(self, dram, o, foff, var, kindf, live=False):
        s = var.get().strip()
        if kindf == "f":
            struct.pack_into(">f", dram, o + foff, float(s))
        elif kindf == "i":
            struct.pack_into(">I", dram, o + foff, int(s) & 0xFFFFFFFF)
        else:
            struct.pack_into(">I", dram, o + foff, int(s, 16) & 0xFFFFFFFF)

    def _apply_record(self):
        sel = self.tv.selection()
        if not sel or self._dram is None:
            return
        o = int(sel[0])
        try:
            for foff, (var, kindf) in self._field_vars.items():
                self._pack_field(self._dram, o, foff, var, kindf)
            self._log(f"applying {self._iff} record @0x{o:X} ...")
            res = oe.apply_dram(self._dram, self._meta, self._iff, self._gd(), self._log)
            self._log(f"  {res}")
            # reload meta (the iff moved)
            self._dram, self._meta = oe.load_dram(self._iff, self._gd())
        except Exception as e:
            self._log(f"apply error: {e}\n{traceback.format_exc()}")

    def _rec_signature(self, o, kind):
        """Stable data-only byte signature for locating a record live (avoids relocated pointers).
        TC: position+colour [+0x1C:+0x40]. Activation: a/b/scale [+0x18:+0x2C]."""
        if kind == "tc":
            return bytes(self._dram[o + 0x1C:o + 0x40]), 0x1C
        return bytes(self._dram[o + 0x18:o + 0x2C]), 0x18

    def _scan_live(self, sig):
        """Search Xenia's committed guest-physical RAM for sig. Returns the host address of the
        signature START (i.e. of the byte at rec+sig_field_off), or None. One match expected."""
        if xm is None or not self.handle:
            return None
        n = len(sig)
        for base, size in xm.enum_committed_regions(self.handle, self.phys_base, xm.PHYS_SIZE):
            off = 0
            while off < size:
                chunk = xm.read_bytes(self.handle, base + off, min(0x400000, size - off))
                if not chunk or len(chunk) < n:
                    break
                i = chunk.find(sig)
                if i >= 0:
                    return base + off + i
                off += len(chunk) - n + 1      # overlap so a boundary-straddling match isn't missed
        return None

    def _find_live(self):
        sel = self.tv.selection()
        if not sel or self._dram is None:
            return
        if not self.handle:
            self.v_livestat.set("live: not attached (Attach Xenia first)"); return
        o = int(sel[0]); kind = dict(self._records).get(o, "tc")
        sig, sig_off = self._rec_signature(o, kind)
        self._log(f"scanning RAM for record @0x{o:X} signature ({len(sig)} bytes)…")
        hit = self._scan_live(sig)
        if hit is None:
            self.v_livestat.set("live: NOT FOUND in RAM (is the overlay active on screen?)")
            self._log("  not found"); return
        rec_addr = hit - sig_off                      # host addr of record base
        self._live_rec_addr[o] = rec_addr
        guest = rec_addr - self.phys_base
        self.v_livestat.set(f"live: @host 0x{rec_addr:X} (guest-phys 0x{guest:X}) — Write Live enabled")
        self._log(f"  found record live @host 0x{rec_addr:X} (guest-phys 0x{guest:X})")

    def _write_live_record(self):
        sel = self.tv.selection()
        if not sel:
            return
        o = int(sel[0])
        rec_addr = self._live_rec_addr.get(o)
        if rec_addr is None:
            self.v_livestat.set("live: run Find Live first"); return
        if not self.handle:
            self._log("not attached"); return
        # pack each edited field and write it at rec_addr + field_off
        for foff, (var, kindf) in self._field_vars.items():
            s = var.get().strip()
            try:
                if kindf == "f":
                    data = struct.pack(">f", float(s))
                elif kindf == "i":
                    data = struct.pack(">I", int(s) & 0xFFFFFFFF)
                else:
                    data = struct.pack(">I", int(s, 16) & 0xFFFFFFFF)
            except ValueError:
                continue
            xm.write_bytes(self.handle, rec_addr + foff, data)
        self._log(f"live-wrote record fields @host 0x{rec_addr:X} (transient — relayout may revert)")

    def _restore(self):
        if not messagebox.askyesno("Restore", f"Restore {self.v_iff.get()} to the pristine CLEAN copy?"):
            return
        try:
            res = oe.restore_from_clean(self.v_iff.get(), self._gd(), self._log)
            self._log(f"restored: {res}")
        except Exception as e:
            self._log(f"restore error: {e}")

    # ---------- Live ----------
    def _load_preset(self, _e):
        i = self.preset_lb.curselection()
        if not i:
            return
        lbl, space, addr, typ = LIVE_PRESETS[i[0]]
        self.v_addr.set(f"{addr:X}"); self.v_space.set(space); self.v_ltype.set(typ)
        self._log(f"preset: {lbl}")

    def _live_host(self):
        guest = int(self.v_addr.get(), 16)
        space = self.v_space.get()
        return guest + (self.phys_base if space == "phys" else VA_HOST_OFFSET)

    def _live_read(self):
        if not self.handle:
            self._log("not attached"); return
        host = self._live_host(); typ = self.v_ltype.get()
        n = {"int32": 4, "float": 4, "rgba": 16, "2int": 8, "bytes16": 16, "bytes64": 64}[typ]
        data = xm.read_bytes(self.handle, host, n)
        if data is None:
            self._log(f"read failed @host 0x{host:X}"); return
        if typ == "int32":
            self.v_lval.set(str(struct.unpack(">I", data)[0]))
        elif typ == "float":
            self.v_lval.set(f"{struct.unpack('>f', data)[0]:.4f}")
        elif typ == "rgba":
            self.v_lval.set(",".join(f"{c:.3f}" for c in struct.unpack(">4f", data)))
        elif typ == "2int":
            self.v_lval.set(",".join(str(x) for x in struct.unpack(">2I", data)))
        else:
            self.v_lval.set(" ".join(f"{b:02X}" for b in data))
        self._log(f"read @0x{host:X} ({typ}) = {self.v_lval.get()}")

    def _pack_live_value(self):
        """Pack the Value entry into big-endian bytes per the selected type."""
        typ = self.v_ltype.get(); s = self.v_lval.get().strip()
        if typ == "int32":
            return struct.pack(">I", int(s) & 0xFFFFFFFF)
        if typ == "float":
            return struct.pack(">f", float(s))
        if typ == "rgba":
            return struct.pack(">4f", *[float(x) for x in s.split(",")])
        if typ == "2int":
            return struct.pack(">2I", *[int(x) & 0xFFFFFFFF for x in s.split(",")])
        return bytes(int(b, 16) for b in s.split())

    def _live_write(self):
        if not self.handle:
            self._log("not attached"); return
        host = self._live_host()
        try:
            data = self._pack_live_value()
            ok = xm.write_bytes(self.handle, host, data)
            self._log(f"write @0x{host:X} ({self.v_ltype.get()}) {'OK' if ok else 'FAILED'}: {self.v_lval.get()}")
        except Exception as e:
            self._log(f"write error: {e}")

    def _xex_write(self):
        """Patch the same value PERMANENTLY into default.xex at the file offset for this guest VA."""
        if xp is None:
            self._log("xex_patch not available"); return
        if self.v_space.get() != "va":
            self._log("Apply to XEX needs a VA address (toggle VA), not Phys"); return
        xexp = self._gd() / "default.xex"
        if not xexp.exists():
            self._log(f"default.xex not found at {xexp}"); return
        try:
            va = int(self.v_addr.get(), 16)
            data = self._pack_live_value()
            off = xp.va_to_offset(str(xexp), va)
            if off is None:
                self._log(f"VA 0x{va:X} not patchable (out of range or zeroed BSS gap)"); return
            with open(xexp, "rb") as fh:
                fh.seek(off); old = fh.read(len(data))
            if not messagebox.askyesno("Apply to XEX",
                    f"Permanently patch default.xex:\n  VA 0x{va:X}  ->  file 0x{off:X}\n"
                    f"  old: {old.hex()}\n  new: {data.hex()}  ({self.v_lval.get()})\n\n"
                    "Takes effect every launch. Continue?"):
                return
            xp.patch_va(str(xexp), va, data, expect=old, log=self._log)
            self._log(f"  PERMANENT: default.xex VA 0x{va:X} -> file 0x{off:X} = {self.v_lval.get()}")
        except Exception as e:
            self._log(f"XEX patch error: {e}")

    # ---------- Raw poke ----------
    def _raw_read(self):
        try:
            dram, meta = oe.load_dram(self.v_riff.get(), self._gd())
            o = int(self.v_roff.get(), 16); typ = self.v_rtype.get()
            if typ == "float":
                self.v_rval.set(f"{struct.unpack_from('>f', dram, o)[0]:.4f}")
            elif typ == "int32":
                self.v_rval.set(str(struct.unpack_from('>I', dram, o)[0]))
            else:
                self.v_rval.set(" ".join(f"{b:02X}" for b in dram[o:o+16]))
            self._log(f"{self.v_riff.get()} DRAM @0x{o:X} ({typ}) = {self.v_rval.get()}")
        except Exception as e:
            self._log(f"raw read error: {e}")

    def _raw_poke(self):
        if not messagebox.askyesno("Poke", "Write to the IFF DRAM and relocate? (Restore-from-Clean to undo)"):
            return
        try:
            iff = self.v_riff.get()
            dram, meta = oe.load_dram(iff, self._gd())
            o = int(self.v_roff.get(), 16); typ = self.v_rtype.get(); s = self.v_rval.get().strip()
            if typ == "float":
                struct.pack_into(">f", dram, o, float(s))
            elif typ == "int32":
                struct.pack_into(">I", dram, o, int(s) & 0xFFFFFFFF)
            else:
                bs = bytes(int(b, 16) for b in s.split()); dram[o:o+len(bs)] = bs
            self._log(f"poke {iff} DRAM @0x{o:X} = {s}")
            res = oe.apply_dram(dram, meta, iff, self._gd(), self._log)
            self._log(f"  {res}")
        except Exception as e:
            self._log(f"raw poke error: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    # If not admin, try to relaunch elevated; if that succeeded, exit this (non-elevated) instance.
    import ctypes
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        is_admin = True
    if not is_admin:
        try:
            params = " ".join(f'"{a}"' for a in sys.argv)
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
            if int(rc) > 32:
                sys.exit(0)   # elevated instance launched; close this one
        except Exception:
            pass              # elevation declined/failed -> run non-elevated (file edits still work)
    DebugLab().mainloop()
