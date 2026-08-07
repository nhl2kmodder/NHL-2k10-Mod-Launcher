"""
face_editor_gui.py — the head editor hung off the Players grid's "Edit head…" button.

This is the front end of the face pipeline that up to now only existed as scripts:

    face_shape.fit          reference photos -> fitted vertex positions
    face_shape.write_shape  those positions back into player_head_id_<id>.iff
    face_builder.build_multi photos -> colour + normal maps, projected through the fitted mesh
    face_builder.install    those maps into the same asset

and it adds the two things a script cannot: you see the head before you commit it, and the
knobs that were keyword arguments are sliders you can turn while looking at the result.

Three things here are deliberate and are not cosmetic:

  · BUILD NEVER WRITES.  "Build from photos" produces maps and positions in memory and previews
    them; nothing reaches the game files until "Install". So you can turn the sliders, rebuild,
    and compare, with the installed head untouched the whole time.

  · THE BUILD ALWAYS STARTS FROM THE ARTIST'S HEAD.  `fit` reads the pristine geometry and
    `base_maps` reads the pristine textures, so building twice gives the same head rather than
    fitting a fit and projecting onto a projection. That is what makes the sliders usable at all
    — without it every rebuild would drift and you could never go back.

  · A HEAD MAY NOT BE INSTALLED INTO A SHARED SLOT.  The asset is the face; every player pointing
    at that id gets it. Install refuses while the slot is shared and sends you to "Give this
    player his own head slot", which moves this row onto a head id no roster row uses.

The normal-intensity slider is the one control that does NOT need a rebuild: `build_multi` hands
back `base_normal` and `normal_weight` alongside the maps precisely so `detail_normal` can be
re-run on its own, which takes a moment rather than a minute.
"""
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np

from . import archive_textures as AT
from . import char_model as C
from . import arena_preview as AP
from . import face_builder as FB
from . import face_shape as FS
from . import facial_hair as FH
from .player_assign import EYE_COLORS

try:
    from PIL import Image, ImageTk
except ImportError:                                    # pragma: no cover
    Image = ImageTk = None

PHOTO_TYPES = [("Images", "*.jpg *.jpeg *.png *.webp *.bmp"), ("All files", "*.*")]
FRONT = 3.14                                           # camera yaw that looks at the face

# name, attribute, lo, hi, default, tooltip-ish one-liner shown under the slider block
SLIDERS = [
    ("Shape strength", "strength", 0.0, 1.5, 1.0,
     "how far the geometry moves from the artist's head toward the photographs"),
    ("Photo sharpness", "sharp", 0.5, 5.0, 2.0,
     "higher = each texel comes from fewer photos: crisper, harder joins"),
    ("Flatten shading", "flatten", 0.0, 1.0, 0.85,
     "divide the photographs' own lighting out — the engine lights the head itself"),
    ("De-light", "delight", 0.0, 1.5, 0.9, "strength of the same, on the fine scale"),
    ("Colour smoothing", "chroma", 0.0, 1.0, 0.7,
     "skin colour varies slowly; blotches in a photo are the room, not the face"),
    ("Normal intensity", "bump", 0.0, 2.0, 0.5,
     "relief baked into the normal map — re-bakes on its own, no rebuild needed"),
    ("Occlusion", "ao", 0.0, 2.0, 1.0,
     "the fitted mesh's own ambient occlusion, folded into the shipped map — 0 keeps the base head's"),
]

FH_AUTO = "Auto — from the photos"
FH_KEEP = "Keep the base head's"
FH_NONE = "Clean-shaven"
FH_PICK = "From another head…"


class HeadEditor(tk.Toplevel):
    """One player's head: preview, build from photos, install, restore."""

    def __init__(self, master, table, row, game_root=None, on_change=None):
        super().__init__(master)
        self.table, self.row = table, row
        self.game_root = str(game_root) if game_root else None
        self.on_change = on_change                     # tell the Players grid a field moved
        if self.game_root:
            AT.set_game_dir(self.game_root)

        fn, ln = table.name(row)
        self.player = (fn + " " + ln).strip() or f"row {row}"
        self.title(f"NHL 2K10 — head editor: {self.player}")
        self.geometry("1180x760")
        try:
            self.transient(master.winfo_toplevel())
        except Exception:
            pass

        self.refs = []                                 # Path list
        self.built = None                              # build_multi result, not yet installed
        self._hair_over = {}                           # {submesh record: mesh} for the beard shells
        self.newP = None                               # fitted positions, not yet installed
        self._fit = None                               # (blob, model, M) that newP belongs to
        self.scene = None
        self._busy = False
        self._photo = None
        # The head asset faces -Z in model space, so the camera's zero yaw looks at the back of
        # his head; open on the face, which is the only view anyone wants first.
        self.cam = dict(yaw=FRONT, pitch=0.0, zoom=1.0)
        self._drag = None

        self._build_ui()
        self._refresh_slot()
        self.after(60, lambda: self._reload_scene(from_disk=True))

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        pane = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(pane, padding=6)
        pane.add(left, weight=0)
        right = ttk.Frame(pane)
        pane.add(right, weight=1)

        # ── references ──
        rf = ttk.LabelFrame(left, text="Reference photos", padding=6)
        rf.pack(fill=tk.X)
        self._reflist = tk.Listbox(rf, height=6, activestyle="none")
        self._reflist.pack(fill=tk.X)
        rb = ttk.Frame(rf)
        rb.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(rb, text="Add…", command=self._add_refs).pack(side=tk.LEFT)
        ttk.Button(rb, text="Remove", command=self._del_ref).pack(side=tk.LEFT, padx=4)
        ttk.Button(rb, text="Add folder…", command=self._add_folder).pack(side=tk.LEFT)
        ttk.Label(rf, text="Front plus both three-quarter views is the useful minimum;\n"
                           "profiles buy the ears and the jaw line.",
                  foreground="#888", justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))

        # ── slot ──
        sf = ttk.LabelFrame(left, text="Head slot", padding=6)
        sf.pack(fill=tk.X, pady=(8, 0))
        self._slot_lbl = ttk.Label(sf, text="", justify=tk.LEFT)
        self._slot_lbl.pack(anchor=tk.W)
        self._slot_btn = ttk.Button(sf, text="Give this player his own head slot",
                                    command=self._claim_slot)
        self._slot_btn.pack(anchor=tk.W, pady=(6, 0))

        ef = ttk.Frame(sf)
        ef.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(ef, text="Eye colour:").pack(side=tk.LEFT)
        self._eye = tk.StringVar(value=self.table.eye_color_name(self.row))
        cb = ttk.Combobox(ef, textvariable=self._eye, values=list(EYE_COLORS), width=10,
                          state="readonly")
        cb.pack(side=tk.LEFT, padx=6)
        cb.bind("<<ComboboxSelected>>", self._set_eye)

        # ── build knobs ──
        bf = ttk.LabelFrame(left, text="Build", padding=6)
        bf.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.v = {}
        for name, key, lo, hi, dflt, note in SLIDERS:
            r = ttk.Frame(bf)
            r.pack(fill=tk.X, pady=(4, 0))
            var = tk.DoubleVar(value=dflt)
            self.v[key] = var
            head = ttk.Frame(r)
            head.pack(fill=tk.X)
            ttk.Label(head, text=name).pack(side=tk.LEFT)
            val = ttk.Label(head, text=f"{dflt:.2f}", foreground="#4a9", width=5,
                            anchor=tk.E)
            val.pack(side=tk.RIGHT)
            s = ttk.Scale(r, from_=lo, to=hi, variable=var, orient=tk.HORIZONTAL)
            s.pack(fill=tk.X)
            # The normal is the one knob that can answer immediately, so it does; the rest only
            # take effect on the next build and say so rather than pretending to be live.
            live = self._rebake_normal if key == "bump" else None
            s.configure(command=lambda _e, v=var, l=val, f=live: self._slid(v, l, f))
            ttk.Label(r, text=note, foreground="#777", wraplength=300,
                      justify=tk.LEFT).pack(anchor=tk.W)
        self._hair = tk.BooleanVar(value=True)
        ttk.Checkbutton(bf, text="Fill hair from the photographs",
                        variable=self._hair).pack(anchor=tk.W, pady=(6, 0))

        # ── facial hair ──
        # The beard is GEOMETRY — a separate shell on the head asset, selected by the submesh
        # record's +0x24 bit. No amount of texture work removes it, which is why a clean-shaven
        # reference set used to come out bearded on a bearded base head.
        ff = ttk.LabelFrame(left, text="Facial hair", padding=6)
        ff.pack(fill=tk.X, pady=(8, 0))
        self._fh_mode = tk.StringVar(value=FH_AUTO)
        fc = ttk.Combobox(ff, textvariable=self._fh_mode, state="readonly", width=26,
                          values=[FH_AUTO, FH_KEEP, FH_NONE, FH_PICK])
        fc.pack(anchor=tk.W)
        fc.bind("<<ComboboxSelected>>", lambda _e: self._fh_changed())
        self._fh_note = ttk.Label(ff, text="decided from the photographs on the next build",
                                  foreground="#888", wraplength=300, justify=tk.LEFT)
        self._fh_note.pack(anchor=tk.W, pady=(4, 0))

        pk = ttk.Frame(ff)
        pk.pack(fill=tk.X, pady=(4, 0))
        self._fh_list = tk.Listbox(pk, height=7, activestyle="none", exportselection=False,
                                   font=("Consolas", 8))
        psb = ttk.Scrollbar(pk, command=self._fh_list.yview)
        self._fh_list.configure(yscrollcommand=psb.set, state=tk.DISABLED)
        psb.pack(side=tk.RIGHT, fill=tk.Y)
        self._fh_list.pack(fill=tk.X)
        self._fh_list.bind("<<ListboxSelect>>", lambda _e: self._fh_changed())
        self._fh_rows, self._fh_for = [], None     # the list is per destination head — see _fh_fill_list

        ab = ttk.Frame(left)
        ab.pack(fill=tk.X, pady=(8, 0))
        self._build_btn = ttk.Button(ab, text="Build from photos", style="Accent.TButton",
                                     command=self._do_build)
        self._build_btn.pack(side=tk.LEFT)
        self._install_btn = ttk.Button(ab, text="Install", command=self._do_install,
                                       state=tk.DISABLED)
        self._install_btn.pack(side=tk.LEFT, padx=4)
        self._restore_btn = ttk.Button(ab, text="Restore stock head", command=self._do_restore)
        self._restore_btn.pack(side=tk.LEFT)

        # ── preview + log ──
        pv = ttk.LabelFrame(right, text="Preview", padding=2)
        pv.pack(fill=tk.BOTH, expand=True, padx=6, pady=(6, 0))
        self._canvas = tk.Label(pv, background="#1b1b1b", text="loading…", foreground="#888")
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._canvas.bind("<ButtonPress-1>", self._down)
        self._canvas.bind("<B1-Motion>", self._move)
        self._canvas.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag", None))
        self._canvas.bind("<MouseWheel>", self._wheel)

        vb = ttk.Frame(right)
        vb.pack(fill=tk.X, padx=6)
        ttk.Label(vb, text="drag to turn · wheel to zoom").pack(side=tk.LEFT)
        for txt, yaw in (("Front", FRONT), ("3/4", FRONT - 0.8), ("Side", FRONT - 1.57),
                         ("Back", 0.0)):
            ttk.Button(vb, text=txt, width=6,
                       command=lambda y=yaw: self._set_yaw(y)).pack(side=tk.LEFT, padx=2)
        self._view = tk.StringVar(value="installed")
        ttk.Radiobutton(vb, text="Built", variable=self._view, value="built",
                        command=self._switch_view).pack(side=tk.RIGHT)
        ttk.Radiobutton(vb, text="In game files", variable=self._view, value="installed",
                        command=self._switch_view).pack(side=tk.RIGHT, padx=4)

        lf = ttk.LabelFrame(right, text="Log", padding=2)
        lf.pack(fill=tk.X, padx=6, pady=6)
        self._log_t = tk.Text(lf, height=8, font=("Consolas", 8), wrap=tk.WORD)
        lsb = ttk.Scrollbar(lf, command=self._log_t.yview)
        self._log_t.configure(yscrollcommand=lsb.set)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_t.pack(fill=tk.X)

    # ── facial hair ──────────────────────────────────────────────────────────
    def _fh_fill_list(self):
        """Load the shell catalogue — every beard in the game — into the picker, once.

        It is a scan of 447 assets, so it happens off the UI thread and only when the picker is
        actually opened. Sorted by how far the shell hangs below the chin, so the list reads
        small to large rather than by an id that means nothing.

        The list is filtered to what fits THIS head (see facial_hair.compatible), so it is
        rebuilt whenever the head changes, and the count of what was left out is logged rather
        than quietly swallowed.
        """
        if self._busy or self._fh_for == self.head_id:
            return
        self.log("reading every facial-hair shell in the game (one-off, then cached)…")
        G, HID = self.game_root, self.head_id

        def work():
            try:
                rows = [r for r in FH.compatible(HID, G, log=self.log)
                        if r["bit"] in FH.FACIAL_BITS]
            except Exception as e:
                self.log(f"facial-hair catalogue: {e}")
                return
            rows.sort(key=lambda r: (r["drop"], r["head"]))

            def done():
                self._fh_rows, self._fh_for = rows, HID
                self._fh_list.config(state=tk.NORMAL)
                self._fh_list.delete(0, tk.END)
                for r in rows:
                    self._fh_list.insert(
                        tk.END, f"{r['head']:>5}  {FH.SLOT_NAME[r['bit']]:<14} "
                                f"hangs {r['drop']:+.1f} cm")
                self.log(f"  {len(rows)} shells across {len({r['head'] for r in rows})} heads")
            try:
                self.after(0, done)
            except tk.TclError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _fh_wanted(self):
        """The per-slot wish this UI expresses — see facial_hair.plan for the vocabulary."""
        mode = self._fh_mode.get()
        if mode == FH_KEEP:
            return {}
        bits = sorted(b for b in FH.hair_slots(self._fh_head_model()) if b in FH.FACIAL_BITS)
        if mode == FH_NONE:
            return {b: None for b in bits}
        if mode == FH_PICK:
            sel = self._fh_list.curselection()
            if not sel or not self._fh_rows:
                return {}
            r = self._fh_rows[sel[0]]
            # The chosen shell goes into ITS OWN slot and the other two come off, so what you
            # see is the one beard you picked rather than it stacked on the base head's.
            return {b: ((r["head"], r["bit"]) if b == r["bit"] else None) for b in bits}
        return None                                    # auto — decided from the built map

    def _fh_head_model(self):
        return (self._fit[2] if self._fit is not None
                else FH.load_head(self.head_id, self.game_root)[2])

    def _fh_plan(self):
        """Turn the wish into {submesh record: mesh}. Runs on a worker thread — it warps meshes."""
        want = self._fh_wanted()
        if want is None:
            if not self.built:
                # Auto reads the jaw off the finished colour map, so there is nothing to read yet.
                self.log("facial hair: auto decides once the face is built — "
                         "the base head's own beard is showing until then")
                return {}
            want = FH.recommend(self.built, self.head_id, self.game_root, log=self.log)
        return FH.plan(self.head_id, want, self.game_root,
                       dst_pos=self.newP, log=self.log) if want else {}

    def _fh_changed(self):
        mode = self._fh_mode.get()
        self._fh_list.config(state=tk.NORMAL if mode == FH_PICK else tk.DISABLED)
        if mode == FH_PICK:
            self._fh_fill_list()
        self._fh_note.config(
            text={FH_AUTO: "decided from the photographs: the jaw is compared against the "
                           "cheek on the finished map",
                  FH_KEEP: "the base head's own beard is left exactly as the artist made it",
                  FH_NONE: "every beard shell is collapsed — only what the photos paint remains",
                  FH_PICK: "pick a shell; it is warped onto this head's jaw"}[mode])
        if not self._busy:
            self._fh_apply()          # a beard is worth seeing before there is a build to see it on

    def _fh_apply(self):
        """Re-plan the shells and refresh the preview. Cheap enough to run on every change.

        Serialised: clicking down a long list starts a plan per click, and a slow one landing
        after a fast one would leave the preview showing a beard nobody has selected.
        """
        self._lock(True)
        self._fh_seq = getattr(self, "_fh_seq", 0) + 1
        seq = self._fh_seq

        def work():
            try:
                over = self._fh_plan()
            except Exception as e:
                over = {}
                self.log(f"facial hair: {e}")

            def done():
                if seq != self._fh_seq:               # a newer pick already won
                    self._lock(False)
                    return
                self._hair_over = over
                self._lock(False)
                if self.built:
                    self._view.set("built")
                self._reload_scene(from_disk=self.built is None)
            try:
                self.after(0, done)
            except tk.TclError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _slid(self, var, lbl, live):
        lbl.config(text=f"{var.get():.2f}")
        if live is not None:
            self._debounce(live)

    def _debounce(self, fn, ms=350):
        job = getattr(self, "_deb", None)
        if job:
            self.after_cancel(job)
        self._deb = self.after(ms, fn)

    def log(self, msg):
        def put():
            self._log_t.insert(tk.END, str(msg).rstrip() + "\n")
            self._log_t.see(tk.END)
        try:
            self.after(0, put)
        except tk.TclError:
            pass

    # ── head slot ────────────────────────────────────────────────────────────
    @property
    def head_id(self):
        return self.table.head(self.row)

    def _shared_rows(self):
        return [r for r in self.table.head_usage().get(self.head_id, []) if r != self.row]

    def _refresh_slot(self):
        hid, others = self.head_id, self._shared_rows()
        if others:
            names = ", ".join((" ".join(self.table.name(r)).strip() or f"row {r}")
                              for r in others[:3])
            more = f" +{len(others) - 3} more" if len(others) > 3 else ""
            self._slot_lbl.config(
                text=f"Head {hid} — SHARED with {len(others)} other player(s)\n{names}{more}\n"
                     f"Installing here would repaint all of them.", foreground="#e0a030")
            self._slot_btn.config(state=tk.NORMAL)
        else:
            self._slot_lbl.config(text=f"Head {hid} — used by this player only.",
                                  foreground="#8c8")
            self._slot_btn.config(state=tk.DISABLED)

    def _claim_slot(self):
        """Move this row onto a head id no roster row points at.

        `face_builder.free_slots` re-reads the roster off disk, which would miss the head edits
        sitting unsaved in this very table — so the same subtraction is done against the live
        table instead: assets that exist, minus every id in use."""
        try:
            have = set(C.head_ids(self.game_root))
        except Exception as e:
            return messagebox.showerror("Head slot", str(e), parent=self)
        free = sorted(have - set(self.table.head_usage()))
        if not free:
            return messagebox.showwarning(
                "Head slot",
                "Every head asset in the game is pointed at by at least one player.\n\n"
                "Free one by moving another player onto a shared head, or add a new head asset "
                "(not wired up yet).", parent=self)
        hid = free[0]
        if not messagebox.askyesno(
                "Head slot", f"Give {self.player} head slot {hid}?\n\n"
                             f"{len(free)} unused head assets exist; this takes the lowest. "
                             f"Nothing is written until you save the roster in the Players tab.",
                parent=self):
            return
        self.table.set_head(self.row, hid, game_dir=self.game_root)
        self.log(f"head slot: {self.player} -> {hid} (roster edit, unsaved)")
        self.built = self.newP = self._fit = None      # a build belongs to the slot it was for
        self._hair_over = {}
        self._fh_rows, self._fh_for = [], None         # a different head fits a different set
        self._fh_list.delete(0, tk.END)
        self._install_btn.config(state=tk.DISABLED)
        self._view.set("installed")
        self._refresh_slot()
        if self.on_change:
            self.on_change()
        self._reload_scene(from_disk=True)

    def _set_eye(self, _=None):
        try:
            self.table.set_eye_color(self.row, self._eye.get())
        except Exception as e:
            return messagebox.showerror("Eye colour", str(e), parent=self)
        self.log(f"eye colour -> {self._eye.get()} (roster edit, unsaved)")
        if self.on_change:
            self.on_change()

    # ── references ───────────────────────────────────────────────────────────
    def _add_refs(self):
        for p in filedialog.askopenfilenames(title="Reference photos", filetypes=PHOTO_TYPES,
                                             parent=self):
            self._add_one(Path(p))

    def _add_folder(self):
        d = filedialog.askdirectory(title="Folder of reference photos", parent=self)
        if not d:
            return
        exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
        for p in sorted(Path(d).iterdir()):
            if p.suffix.lower() in exts:
                self._add_one(p)

    def _add_one(self, p):
        if p in self.refs:
            return
        self.refs.append(p)
        self._reflist.insert(tk.END, p.name)

    def _del_ref(self):
        for i in reversed(self._reflist.curselection()):
            self._reflist.delete(i)
            del self.refs[i]

    # ── preview ──────────────────────────────────────────────────────────────
    def _down(self, e):
        self._drag = (e.x, e.y, self.cam["yaw"], self.cam["pitch"])

    def _move(self, e):
        if not self._drag:
            return
        x, y, yaw, pitch = self._drag
        self.cam["yaw"] = yaw + (e.x - x) * 0.012
        self.cam["pitch"] = max(-1.2, min(1.2, pitch - (e.y - y) * 0.012))
        self._debounce(self._redraw, 40)

    def _wheel(self, e):
        self.cam["zoom"] = max(0.4, min(4.0, self.cam["zoom"] * (1.1 if e.delta > 0 else 1 / 1.1)))
        self._debounce(self._redraw, 40)

    def _set_yaw(self, y):
        self.cam["yaw"], self.cam["pitch"] = y, 0.0
        self._redraw()

    def _switch_view(self):
        self._reload_scene(from_disk=self._view.get() == "installed")

    def _reload_scene(self, from_disk=True):
        """Rebuild the draw list. from_disk = what the game would load right now; otherwise the
        fitted positions and the built maps that have not been installed."""
        if self._busy:
            return
        hid = self.head_id
        nm = C.HEAD_FMT.format(hid)
        use_built = (not from_disk) and self.built is not None
        # Read ONCE: _fh_apply publishes this from the UI thread, and a worker that read it
        # twice could take the no-shells branch and then find shells to apply with no model.
        over = dict(self._hair_over or {})

        def work():
            try:
                if use_built and self._fit is not None:
                    b, m, _M = self._fit
                    # The facial-hair edits go in at the BLOB level, exactly as Install writes
                    # them, because a removed or transplanted shell changes triangles and not
                    # just positions — splicing them into the finished scene would not show the
                    # new topology. The fitted positions still arrive via _apply_built below.
                    b = bytes(b)
                else:
                    # No build (or showing what is on disk): the shells still apply, so a beard
                    # can be picked and judged on the untouched head without paying for a build.
                    b, m, _M = FH.load_head(self.head_id, self.game_root, current=True) if over \
                        else (C.blob(True, self.game_root, nm), None, None)
                    if m is None:
                        m = C.scan_models(b, nm)[0]
                if over:
                    buf = bytearray(b)
                    recs = {p["rec"]: p for p in _M["parts"]}
                    for rec, mesh in over.items():
                        C.replace_part(buf, m, recs[rec], mesh, log=lambda *_a, **_k: None)
                    b = bytes(buf)
                # Draw the beard slots as untextured silhouettes so a pick can be judged; the
                # skin cut would otherwise drop them (see build_scene's `also`).
                if _M is None:
                    _M = C.read_model(b, m)
                also = {p["rec"] for bit, p in FH.hair_slots(_M).items()
                        if bit in FH.FACIAL_BITS}
                sc = C.build_scene(b, m, asset=nm, game_dir=self.game_root, also=also)
                if sc is None:
                    raise ValueError(f"{nm}: nothing to draw")
                C.head_light(sc)          # rake it across the face; see char_model.HEAD_LIGHT
                if use_built:
                    self._apply_built(sc)
                err = None
            except Exception as e:
                sc, err = None, str(e)
                self.log(traceback.format_exc())

            def done():
                if sc is not None:
                    self.scene = sc
                    self._redraw()
                else:
                    self._canvas.config(image="", text=f"preview: {err}")
            try:
                self.after(0, done)
            except tk.TclError:
                pass
        self._canvas.config(text="loading…")
        threading.Thread(target=work, daemon=True).start()

    def _apply_built(self, sc):
        """Swap the fitted geometry and the uninstalled maps into a scene built from disk.

        The maps go on exactly as `char_model._asset_maps` would have loaded them — occlusion
        multiplied into the albedo — so the built preview and the installed preview are shaded
        by the same rules and are honestly comparable."""
        if self.newP is not None and len(self.newP) == len(sc["pos"]):
            P = np.asarray(self.newP, np.float32).copy()
            # …but not over the hair shells. `newP` is the fitted FACE, and the shells were
            # already written into this scene's blob above; overwriting them here would put the
            # base head's beard straight back.
            if self._hair_over and self._fit is not None:
                for p in self._fit[2]["parts"]:
                    mesh = self._hair_over.get(p["rec"])
                    if mesh is None:
                        continue
                    lo = p["first_vtx"]
                    q = np.asarray(mesh["pos"], np.float32)
                    P[lo:lo + len(q)] = q
            sc["pos"] = P
            sc["nrm"] = C._vertex_normals(sc["pos"].astype(np.float64),
                                          sc["tri"]).astype(np.float32)
            sc["vcol"] = C.lambert(sc["nrm"], len(sc["pos"]))
        col = np.asarray(self.built["color"].convert("RGB"), np.float32)
        ao = self.built.get("occlusion")
        if ao is not None:
            a = np.asarray(ao.convert("L").resize(self.built["color"].size), np.float32)
            col = col * (a[..., None] / 255.0)
        col = np.clip(col, 0, 255).astype(np.uint8)
        nrm = self.built.get("normal")
        nrm = np.asarray(nrm.convert("RGB"), np.uint8) if nrm is not None else None
        for mid in list(sc["tex"]):
            sc["tex"][mid] = col
            if nrm is not None:
                sc["ntex"][mid] = nrm
        if nrm is not None:
            sc["tan"] = C.tangents(sc["pos"], sc["uv"], sc["tri"], sc["nrm"])

    def _redraw(self):
        if self.scene is None or ImageTk is None:
            return
        W = max(320, self._canvas.winfo_width())
        H = max(240, self._canvas.winfo_height())
        cam = dict(self.cam)
        scene = self.scene

        def work():
            try:
                im = AP.render(scene, W, H, cam["yaw"], cam["pitch"], cam["zoom"])
            except Exception as e:
                im, err = None, str(e)
            else:
                err = None

            def done():
                if im is not None:
                    self._photo = ImageTk.PhotoImage(im)
                    self._canvas.config(image=self._photo, text="")
                else:
                    self._canvas.config(image="", text=f"preview: {err}")
            try:
                self.after(0, done)
            except tk.TclError:
                pass
        threading.Thread(target=work, daemon=True).start()

    # ── build / install / restore ────────────────────────────────────────────
    def _lock(self, on):
        self._busy = on
        st = tk.DISABLED if on else tk.NORMAL
        for b in (self._build_btn, self._restore_btn, self._slot_btn):
            b.config(state=st)
        if not on:
            self._refresh_slot()
            self._install_btn.config(state=tk.NORMAL if self.built else tk.DISABLED)
        else:
            self._install_btn.config(state=tk.DISABLED)

    def _do_build(self):
        if not self.refs:
            return messagebox.showinfo("Build", "Add some reference photographs first.",
                                       parent=self)
        hid = self.head_id
        refs, G = list(self.refs), self.game_root
        args = {k: float(v.get()) for k, v in self.v.items()}
        hair = bool(self._hair.get())
        self._lock(True)
        self.log(f"building head {hid} from {len(refs)} photo(s) — nothing is written yet")

        def work():
            out = err = None
            try:
                b, m, M, newP, info = FS.fit(hid, refs, game_dir=G,
                                             strength=args["strength"], log=self.log)
                self.log(f"  ears held to {info['ear_moved']:.3f} cm "
                         f"while the face moved {info['moved']:.2f} cm")
                maps = FB.build_multi(refs, hid, game_dir=G, positions=newP,
                                      sharp=args["sharp"], delight=args["delight"],
                                      flatten=args["flatten"], chroma=args["chroma"],
                                      bump=args["bump"], ao=args.get("ao", 1.0),
                                      fill_hair=hair, log=self.log)
                out = (b, m, M, newP, maps)
            except Exception as e:
                err = str(e)
                self.log(traceback.format_exc())

            def done():
                self._lock(False)
                if out is None:
                    self.log(f"build failed: {err}")
                    return messagebox.showerror("Build", err, parent=self)
                b, m, M, newP, maps = out
                self._fit, self.newP, self.built = (b, m, M), newP, maps
                self.log("build done — previewing it; press Install to write it into the game")
                self._view.set("built")
                self._fh_apply()                       # decides the beard, then draws
            try:
                self.after(0, done)
            except tk.TclError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _rebake_normal(self):
        """Re-derive the normal map at the slider's intensity, without re-projecting anything."""
        if not self.built or self.built.get("base_normal") is None or self._busy:
            return
        bump = float(self.v["bump"].get())

        def work():
            try:
                nm = FB.detail_normal(self.built["color"], self.built["base_normal"],
                                      self.built["normal_weight"], bump=bump,
                                      hair=self.built.get("hair_weight"))
                img = Image.fromarray(nm)
            except Exception as e:
                self.log(f"normal re-bake: {e}")
                return

            def done():
                self.built["normal"] = img
                self.log(f"normal map re-baked at intensity {bump:.2f}")
                if self._view.get() == "built":
                    self._reload_scene(from_disk=False)
            try:
                self.after(0, done)
            except tk.TclError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _do_install(self):
        if not self.built:
            return
        others = self._shared_rows()
        if others:
            return messagebox.showwarning(
                "Install", f"Head {self.head_id} is shared with {len(others)} other player(s) — "
                           f"installing would give every one of them this face.\n\n"
                           f"Use \"Give this player his own head slot\" first.", parent=self)
        hid = self.head_id
        if not messagebox.askyesno(
                "Install", f"Write this head into player_head_id_{hid:04d}.iff?\n\n"
                           f"The geometry and the colour/normal maps are replaced in the game "
                           f"files. \"Restore stock head\" puts the artist's head back.",
                parent=self):
            return
        b, m, M = self._fit
        newP, maps, G = self.newP, self.built, self.game_root
        over = dict(self._hair_over or {})
        self._lock(True)

        def work():
            err = None
            try:
                FS.write_shape(b, m, M, newP, hid, game_dir=G, log=self.log,
                                     parts_override=over)
                # All three. This used to write colour and normal only, and it was right to: the
                # occlusion map was the base head's own, passed through the build untouched, so
                # installing it was a lossy DXT re-encode of the bytes already there. It is now
                # built from the fitted mesh (face_builder.mesh_occlusion) and is the channel that
                # carries the mandible and the shadow under the chin, so it has to go in.
                self.log(FB.install(hid, maps, G, log=self.log))
            except Exception as e:
                err = str(e)
                self.log(traceback.format_exc())

            def done():
                self._lock(False)
                if err:
                    return messagebox.showerror("Install", err, parent=self)
                self.log(f"head {hid} installed")
                self._view.set("installed")
                self._reload_scene(from_disk=True)
                messagebox.showinfo(
                    "Install", f"Head {hid} written.\n\nRemember to press Save in the Players "
                               f"tab as well — the head id and eye colour are roster edits and "
                               f"are still unsaved.", parent=self)
            try:
                self.after(0, done)
            except tk.TclError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _do_restore(self):
        hid = self.head_id
        if not messagebox.askyesno("Restore", f"Put the artist's head {hid} back?\n\n"
                                              f"This undoes the geometry and the maps in the "
                                              f"game files. Roster edits are not touched.",
                                   parent=self):
            return
        G = self.game_root
        self._lock(True)

        def work():
            err = None
            try:
                # One archive splice of the pristine asset, not "stock geometry + re-installed
                # stock maps": installing maps re-encodes them, and DXT never lands on the same
                # block endpoints twice, so that route leaves a head that LOOKS stock while its
                # bytes say it was edited. The Mod Pack tab reads exactly those bytes to decide
                # which heads are yours to share, so a lossy restore would keep offering this one.
                if AT.ensure_clean(FB.HEAD_FMT.format(hid), Path(G), self.log):
                    self.log(f"  head {hid}: the artist's face is back")
                else:
                    self.log(f"  head {hid} was already the artist's — nothing to restore")
            except Exception as e:
                err = str(e)
                self.log(traceback.format_exc())

            def done():
                self._lock(False)
                if err:
                    return messagebox.showerror("Restore", err, parent=self)
                self._view.set("installed")
                self._reload_scene(from_disk=True)
            try:
                self.after(0, done)
            except tk.TclError:
                pass
        threading.Thread(target=work, daemon=True).start()


def open_head_editor(master, table, row, game_root=None, on_change=None):
    return HeadEditor(master, table, row, game_root, on_change)
