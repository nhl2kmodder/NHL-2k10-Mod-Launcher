"""arena_gui.py — the Arena tab: preview an arena WITH its textures and baked lighting, and
tweak all three of textures, lighting and models from the same place.

Everything the tab needs is offline: arena_model.scan_models finds every mesh in the asset's
DRAM blob by signature (so all 30 teams work with no capture), arena_materials.json says which
texture each material uses, and arena_preview rasterizes the result in numpy — the launcher is
tkinter, there is no GPU to draw with.

The lighting sliders are a PREVIEW until you press Apply: the arena bake is stored per vertex
at a different scale per material, so the edit is multiplicative (arena_model.relight) and the
preview can show it just by scaling the triangle colours — no rebuild. Apply re-encodes blob 0
size-preservingly; if the change doesn't fit the compressed slot nothing is written at all.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path

import numpy as np
from PIL import Image
from tkinter import (BOTH, BooleanVar, E, END, LEFT, RIGHT, StringVar, DoubleVar, W, X, Y,
                     VERTICAL, Label, filedialog, messagebox, ttk)

from . import archive_textures as A
from . import arena_model as AM
from . import arena_preview as AP
from .audio_names import CODE_TEAM

KIND_LABEL = {"arena": "Bowl / stands (arena_*)", "rink": "Rink + boards (rink_*)",
              "led": "LED boards (led_*)", "arena_presentation": "Presentation (arena_presentation_*)"}


def build_tab(app, frame):
    """Called once from the launcher; returns the controller (also stored as app._arena)."""
    tab = ArenaTab(app, frame)
    app._arena = tab
    return tab


class ArenaTab:
    def __init__(self, app, root):
        self.app = app
        self.blob = None            # pristine DRAM (never mutated)
        self.models = []
        self.scene = None
        self.texrecs = {}
        self._texcache = {}
        self._partmap = {}          # tree iid -> (model index, submesh dict)
        self.iff = None
        self.busy = False
        self._pending = None
        self._img = None            # keep a reference or tk drops the image
        self._g = None              # best cached G-buffer for the current camera
        self._gfast = None          # small G-buffer for the same camera: the live-slider path
        self._shownkey = None       # key of the buffer the displayed frame was shaded from
        self._lightref = {}         # iff -> per-model bake scale, so Apply visibly changes it
        self._roomcache = {}        # iff -> headroom(); ~20 s to compute, only changes on write
        self._flash_on = True
        self._flash_n = 0
        self._resize_job = None
        self._refine_job = None
        self.cam = dict(yaw=0.6, pitch=0.45, zoom=1.15, pan=[0.0, 0.0])
        self._drag = None

        # ── header ────────────────────────────────────────────────────────────
        head = ttk.Frame(root, padding=(12, 10, 12, 4)); head.pack(fill=X)
        ttk.Label(head, text="Arena", font=("Segoe UI", 13, "bold")).pack(side=LEFT)
        ttk.Label(head, text="   Team").pack(side=LEFT)
        self.v_team = StringVar(value="Vancouver Canucks")
        names = [CODE_TEAM[c] for c in sorted(CODE_TEAM, key=lambda k: CODE_TEAM[k])]
        cb = ttk.Combobox(head, textvariable=self.v_team, values=names, width=24,
                          state="readonly")
        cb.pack(side=LEFT, padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.load())
        ttk.Label(head, text="   Asset").pack(side=LEFT)
        self.v_kind = StringVar(value=KIND_LABEL["arena"])
        cb2 = ttk.Combobox(head, textvariable=self.v_kind,
                           values=[KIND_LABEL[k] for k in AM.KINDS], width=32, state="readonly")
        cb2.pack(side=LEFT, padx=4)
        cb2.bind("<<ComboboxSelected>>", lambda e: self.load())
        ttk.Button(head, text="Reload", command=self.load).pack(side=LEFT, padx=4)
        self.v_status = StringVar(value="")
        ttk.Label(head, textvariable=self.v_status, foreground="#999").pack(side=RIGHT)

        ttk.Label(root, foreground="#999", font=("Segoe UI", 8), justify=LEFT, wraplength=1000,
                  text="Every arena is four assets: the bowl, the rink, the LED boards and the "
                       "presentation props. Meshes are found straight in the file, so this works "
                       "for all 30 teams. Drag to orbit, right-drag to pan, wheel to zoom. "
                       "The lighting shown is the game's own baked per-vertex lighting."
                  ).pack(fill=X, padx=12)

        body = ttk.Frame(root); body.pack(fill=BOTH, expand=True, padx=12, pady=6)

        # The three columns are packed HERE, in this order, on purpose. The preview is an image
        # sized to its own container, so if the container took its size from the image the two
        # would chase each other and the viewport would grow without bound, squeezing the side
        # panels off screen. The fixed columns are packed first and claim their width; the
        # preview column gets the slack and has geometry propagation switched off, so the image
        # can never feed its size back into the layout.
        lf = ttk.Frame(body); lf.pack(side=LEFT, fill=Y)
        ctl = ttk.Frame(body, width=236); ctl.pack(side=RIGHT, fill=Y)
        ctl.pack_propagate(False)
        mid = ttk.Frame(body, padding=(10, 0)); mid.pack(side=LEFT, fill=BOTH, expand=True)
        self._mid, self._ctl = mid, ctl

        # ── left: model / submesh tree ────────────────────────────────────────
        cols = ("tris", "mat", "tex")
        tv = ttk.Treeview(lf, columns=cols, height=26, selectmode="browse")
        tv.heading("#0", text="Model / part"); tv.column("#0", width=170, anchor=W)
        for c, w, t in (("tris", 62, "Tris"), ("mat", 46, "Mat"), ("tex", 46, "Tex")):
            tv.heading(c, text=t); tv.column(c, width=w, anchor=E)
        sb = ttk.Scrollbar(lf, command=tv.yview, orient=VERTICAL)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y); tv.pack(side=LEFT, fill=Y)
        tv.bind("<<TreeviewSelect>>", lambda e: self._selected())
        self.tv = tv

        # ── centre: preview ───────────────────────────────────────────────────
        view = ttk.Frame(mid); view.pack(fill=BOTH, expand=True)
        view.pack_propagate(False)                  # the image must not drive the layout
        self.view = view
        self.canvas = Label(view, background="#0e0f12", cursor="fleur")
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.canvas.bind("<ButtonPress-1>", lambda e: self._down(e, "orbit"))
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._up)
        self.canvas.bind("<ButtonPress-3>", lambda e: self._down(e, "pan"))
        self.canvas.bind("<B3-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-3>", self._up)
        self.canvas.bind("<MouseWheel>", self._wheel)
        view.bind("<Configure>", self._resized)

        vbar = ttk.Frame(mid); vbar.pack(fill=X, pady=(4, 0))
        ttk.Button(vbar, text="Reset view", command=self.reset_view).pack(side=LEFT)
        ttk.Label(vbar, text="  Brightness").pack(side=LEFT)
        self.v_exp = DoubleVar(value=1.8)
        # brightness is a shading term only — it never re-rasterizes, so it is live
        ttk.Scale(vbar, from_=0.5, to=12.0, variable=self.v_exp, length=130,
                  command=lambda *_: self.reshade()).pack(side=LEFT, padx=4)
        ttk.Label(vbar, text="  Cutaway").pack(side=LEFT)
        self.v_cut = DoubleVar(value=0.35)          # default view is from inside the bowl
        ttk.Scale(vbar, from_=0.0, to=0.9, variable=self.v_cut, length=110,
                  command=lambda *_: self.render(quick=True)).pack(side=LEFT, padx=4)
        self.v_iso = BooleanVar(value=False)
        ttk.Checkbutton(vbar, text="Isolate", variable=self.v_iso,
                        command=self.render).pack(side=LEFT, padx=(8, 0))
        ttk.Button(vbar, text="Flash", width=6, command=self.flash).pack(side=LEFT, padx=4)
        self.v_info = StringVar(value="")
        ttk.Label(vbar, textvariable=self.v_info, foreground="#999",
                  font=("Segoe UI", 8)).pack(side=RIGHT)
        ttk.Label(mid, foreground="#888", font=("Segoe UI", 7), justify=LEFT,
                  text="Click a surface in the view to select that part in the list. Cutaway "
                       "removes the near half so you can see inside; Isolate shows the selected "
                       "part on its own.").pack(anchor=W, pady=(2, 0))

        # ── right: lighting + actions ─────────────────────────────────────────
        lg = ttk.LabelFrame(ctl, text="Baked lighting", padding=8); lg.pack(fill=X)
        self.v_gain = DoubleVar(value=1.0)
        self.v_amb = DoubleVar(value=1.0)
        self.v_r = DoubleVar(value=1.0)
        self.v_g = DoubleVar(value=1.0)
        self.v_b = DoubleVar(value=1.0)
        for lbl, var, lo, hi in (("Gain", self.v_gain, 0.1, 4.0), ("Ambient", self.v_amb, 0.0, 4.0),
                                 ("Red", self.v_r, 0.0, 2.0), ("Green", self.v_g, 0.0, 2.0),
                                 ("Blue", self.v_b, 0.0, 2.0)):
            row = ttk.Frame(lg); row.pack(fill=X, pady=1)
            ttk.Label(row, text=lbl, width=8).pack(side=LEFT)
            ttk.Scale(row, from_=lo, to=hi, variable=var, length=118,
                      command=lambda *_: self._light_changed()).pack(side=LEFT)
            v = StringVar(value="1.00")
            ttk.Label(row, textvariable=v, width=5, foreground="#aaa").pack(side=LEFT)
            var.trace_add("write", lambda *_a, vv=v, vr=var: vv.set(f"{vr.get():.2f}"))
        ttk.Label(lg, text="Multiplies the light already baked into every vertex. Ambient is a "
                           "separate stored term and is not shown in the preview.",
                  foreground="#888", font=("Segoe UI", 7), wraplength=196,
                  justify=LEFT).pack(anchor=W, pady=(3, 0))
        pr = ttk.Frame(lg); pr.pack(fill=X, pady=(4, 0))
        for name, args in (("Brighter", (1.6, 1.4, (1, 1, 1))), ("Darker", (0.6, 0.7, (1, 1, 1))),
                           ("Warm", (1.15, 1.0, (1.12, 1.0, 0.85))),
                           ("Cool", (1.15, 1.0, (0.88, 0.98, 1.15)))):
            ttk.Button(pr, text=name, width=8,
                       command=lambda a=args: self._preset(*a)).pack(side=LEFT, padx=1)
        ttk.Button(lg, text="Reset sliders", command=lambda: self._preset(1, 1, (1, 1, 1))
                   ).pack(fill=X, pady=(4, 0))

        act = ttk.LabelFrame(ctl, text="Apply", padding=8); act.pack(fill=X, pady=(8, 0))
        ttk.Button(act, text="Apply lighting to game files", style="Accent.TButton",
                   command=self.apply_light).pack(fill=X)
        ttk.Button(act, text="Restore original lighting/geometry",
                   command=self.restore).pack(fill=X, pady=(4, 0))
        ttk.Button(act, text="Check free space in the archive slot",
                   command=self.check_room).pack(fill=X, pady=(4, 0))
        self.v_room = StringVar(value="")
        ttk.Label(act, textvariable=self.v_room, foreground="#888", font=("Segoe UI", 7),
                  wraplength=196, justify=LEFT).pack(anchor=W, pady=(3, 0))

        tx = ttk.LabelFrame(ctl, text="Selected part", padding=8); tx.pack(fill=X, pady=(8, 0))
        self.v_sel = StringVar(value="(select a part on the left, or click one in the view)")
        ttk.Label(tx, textvariable=self.v_sel, foreground="#aaa", font=("Segoe UI", 8),
                  wraplength=200, justify=LEFT).pack(anchor=W)
        self.v_dims = StringVar(value="")
        ttk.Label(tx, textvariable=self.v_dims, foreground="#7fb0d8", font=("Segoe UI", 7),
                  wraplength=200, justify=LEFT).pack(anchor=W, pady=(2, 0))
        self.thumb = Label(tx, background="#141518")
        self.thumb.pack(pady=4)
        ttk.Button(tx, text="Flash it in the view", command=self.flash).pack(fill=X, pady=(0, 4))
        ttk.Button(tx, text="Replace this texture…", command=self.replace_texture).pack(fill=X)
        ttk.Button(tx, text="Export this texture…", command=self.export_texture
                   ).pack(fill=X, pady=(4, 0))

        ex = ttk.LabelFrame(ctl, text="Models", padding=8); ex.pack(fill=X, pady=(8, 0))
        ttk.Button(ex, text="Export whole arena for Blender…",
                   command=self.export_obj).pack(fill=X)
        ttk.Button(ex, text="Export selected MODEL (all parts)…",
                   command=self.export_model).pack(fill=X, pady=(4, 0))
        ttk.Button(ex, text="Replace selected MODEL from .obj…",
                   command=self.replace_model).pack(fill=X, pady=(4, 0))
        ttk.Button(ex, text="Export selected part (.obj)…",
                   command=self.export_part).pack(fill=X, pady=(4, 0))
        ttk.Button(ex, text="Replace selected part from .obj…",
                   command=self.replace_part).pack(fill=X, pady=(4, 0))
        self.v_budget = StringVar(value="")
        ttk.Label(ex, textvariable=self.v_budget, foreground="#888", font=("Segoe UI", 7),
                  wraplength=196, justify=LEFT).pack(anchor=W, pady=(3, 0))
        ttk.Label(ex, text="Writes one .obj + .mtl with the game's own UVs, the decoded "
                           "textures as PNGs and the bake as vertex colours — nothing to hook "
                           "up by hand in Blender.",
                  foreground="#888", font=("Segoe UI", 7), wraplength=196,
                  justify=LEFT).pack(anchor=W, pady=(3, 0))

        app.after(1200, self.load)

    # ────────────────────────────── helpers ──────────────────────────────
    def _code(self):
        for c, n in CODE_TEAM.items():
            if n == self.v_team.get():
                return c
        return "van"

    def _kind(self):
        for k, v in KIND_LABEL.items():
            if v == self.v_kind.get():
                return k
        return "arena"

    def _log(self, msg):
        try:
            self.app._log_q.put(f"[arena] {msg}")
        except Exception:
            pass

    def _root(self):
        return self.app._get_game_root()

    # ────────────────────────────── load ──────────────────────────────
    def load(self):
        iff = f"{self._kind()}_{self._code()}.iff"
        self.iff = iff
        self.v_status.set(f"Loading {iff} …")
        self.tv.delete(*self.tv.get_children())
        self.scene = None
        self._g = self._gfast = self._shownkey = None
        self._texcache.clear()

        # Three stages, so the tree appears long before the (few-second) scene build:
        # 1. parse the asset -> tree,  2. build the render scene -> preview,  3. headroom.
        def work():
            try:
                # the CURRENT game copy, so the preview shows edits already applied
                blob = AM.dram(iff, current=True) or AM.dram(iff)
                if not blob:
                    raise ValueError("not found in the game archives")
                models = AM.scan_models(blob)
                recs = {r["index"]: r for r in A.list_textures(iff)}
                err = None
            except Exception as e:
                blob, models, recs = None, [], {}
                err = str(e)
                self._log(f"{iff}: {traceback.format_exc()}")

            def done():
                if err:
                    self.v_status.set(f"{iff}: {err}")
                    self.v_info.set("")
                    self.v_room.set("")
                    self._show(None)
                    return
                self.blob, self.models, self.texrecs = blob, models, recs
                self._fill_tree()
                tris = sum(m["tris"] for m in models)
                self.v_status.set(f"{iff} — {len(models)} models, {tris:,} triangles, "
                                  f"{len(recs)} textures")
                if not models:
                    self.v_info.set("this asset holds textures only — no meshes")
                    self._show(None)
                    return
                self.v_info.set("building preview …")
                threading.Thread(target=scene_work, daemon=True).start()
            self.app.after(0, done)

        def scene_work():
            try:
                # Reuse the normalisation scale from the first time this asset was loaded.
                # Without it every model is re-normalised to its own 99th percentile, so a
                # global brightness change written to the file normalises straight back out
                # and "Apply lighting" looks like it did nothing.
                sc = AP.build_scene(self.blob, self.models,
                                    self._make_lookup(iff, self.texrecs),
                                    ref=self._lightref.get(iff))
                if sc is not None:
                    self._lightref.setdefault(iff, sc["ref"])
                e2 = None
            except Exception as e:
                sc, e2 = None, str(e)
                self._log(f"{iff} scene: {traceback.format_exc()}")

            def done2():
                if iff != self.iff:                       # the user moved on while we built it
                    return
                self.scene = sc
                self.v_info.set(f"{len(sc['tri']):,} triangles drawn" if sc else (e2 or ""))
                self._preset(1, 1, (1, 1, 1))
                self.reset_view()
                self.check_room(auto=True)
            self.app.after(0, done2)

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _runtime_swatch(rt):
        """Stand-in for a surface the game composites at runtime, so the screens and the ribbon
        read as screens in the preview instead of as untextured grey."""
        w, h = 64, 64
        im = np.zeros((h, w, 3), np.float32)
        if rt.get("kind") == "ribbon":
            im[:, :] = (0.05, 0.35, 0.16)
            im[:, ::8] = (0.02, 0.05, 0.03)                 # panel gaps
        else:
            yy = np.linspace(0, 1, h)[:, None]
            im[:] = np.stack([0.10 + 0.35 * yy + 0 * im[..., 0],
                              0.16 + 0.30 * yy + 0 * im[..., 0],
                              0.30 + 0.45 * yy + 0 * im[..., 0]], -1)
            im[::4, :] *= 0.55                              # scanlines
        return (np.clip(im, 0, 1) * 255).astype(np.uint8)

    def _make_lookup(self, iff, recs):
        def look(mat):
            rt = AM.material_runtime(iff, mat)
            if rt is not None:
                key = f"rt{mat}"
                if key not in self._texcache:
                    self._texcache[key] = self._runtime_swatch(rt)
                return self._texcache[key]
            ti = AM.material_texture(iff, mat)
            if ti is None or ti not in recs:
                return None
            if ti not in self._texcache:
                try:
                    im = A.decode_record(iff, recs[ti])
                    self._texcache[ti] = np.asarray(im.convert("RGB"))
                except Exception:
                    self._texcache[ti] = None
            return self._texcache[ti]
        return look

    def _fill_tree(self):
        tv = self.tv
        tv.delete(*tv.get_children())
        # pid must count submeshes in exactly the order arena_preview.build_scene does, because
        # that is what highlight_part indexes.
        self._partmap = {}
        self._modelparts = {}
        pid = 0
        for mi, m in enumerate(self.models):
            node = tv.insert("", END, iid=f"m{mi}", open=False,
                             text=f"Model {mi:02d}  ({m['nvtx']:,} v)",
                             values=(f"{m['tris']:,}", "", ""))
            for s in AM.submeshes(self.blob, m):
                pid += 1
                self._modelparts.setdefault(mi, []).append(pid)
                ti = AM.material_texture(self.iff, s["mat"])
                rt = AM.material_runtime(self.iff, s["mat"])
                iid = f"p{pid}"
                self._partmap[iid] = (mi, s)
                tv.insert(node, END, iid=iid,
                          text=f"  part {s['rec']:03d}" + (" ▶ live" if rt else ""),
                          values=(f"{s['tris']:,}", s["mat"],
                                  f"{rt['w']}×{rt['h']} live" if rt else
                                  ("" if ti is None else ti)))

    # ────────────────────────────── preview ──────────────────────────────
    # The renderer is split in two (see arena_preview): raster() builds a G-buffer for the
    # current camera, shade() turns it into an image. Only the camera, the viewport size and
    # the cutaway invalidate the G-buffer — brightness, tint, highlight and the flash are all
    # shading, so they re-use the cached one and come back in tens of milliseconds. That is why
    # the lighting sliders no longer re-render the arena on every tick.
    def reset_view(self):
        self.cam.update(yaw=0.6, pitch=0.45, zoom=1.15, pan=[0.0, 0.0])
        self.render()

    def _sel_part(self):
        """What flash/isolate act on: an int for a submesh, a frozenset for a whole model.

        Both are accepted by arena_preview's highlight_part and isolate, and both are hashable,
        which _camkey relies on.
        """
        s = self.tv.selection()
        if not s:
            return None
        if s[0].startswith("p"):
            return int(s[0][1:])
        if s[0].startswith("m"):
            pids = self._modelparts.get(int(s[0][1:]))
            return frozenset(pids) if pids else None
        return None

    def _sel_model(self):
        """The model index behind the current selection, whether a model or a part is picked."""
        s = self.tv.selection()
        if not s:
            return None
        if s[0].startswith("m"):
            return int(s[0][1:])
        if s[0] in self._partmap:
            return self._partmap[s[0]][0]
        return None

    def _vsize(self):
        return max(self.view.winfo_width(), 320), max(self.view.winfo_height(), 240)

    def _down(self, e, mode):
        self._drag = (mode, e.x, e.y, e.x, e.y)

    def _up(self, e):
        d, self._drag = self._drag, None
        if d and d[0] == "orbit" and abs(e.x - d[3]) <= 3 and abs(e.y - d[4]) <= 3:
            self._pick(e.x, e.y)          # a click, not a drag: select what is under the cursor
            return
        self.render()

    def _move(self, e):
        if not self._drag:
            return
        mode, x, y, x0, y0 = self._drag
        dx, dy = e.x - x, e.y - y
        self._drag = (mode, e.x, e.y, x0, y0)
        if mode == "orbit":
            self.cam["yaw"] += dx * 0.008
            self.cam["pitch"] = max(-1.5, min(1.5, self.cam["pitch"] + dy * 0.008))
        else:
            self.cam["pan"][0] += dx / max(self.canvas.winfo_width(), 1)
            self.cam["pan"][1] += dy / max(self.canvas.winfo_height(), 1)
        self.render(quick=True)

    def _wheel(self, e):
        self.cam["zoom"] = max(0.2, min(24.0, self.cam["zoom"] * (1.15 if e.delta > 0 else 1 / 1.15)))
        self.render(quick=True)

    def _resized(self, _e):
        # a window drag fires this dozens of times; coalesce or we queue dozens of renders
        if not self.scene:
            return
        if self._resize_job:
            self.app.after_cancel(self._resize_job)
        self._resize_job = self.app.after(140, lambda: (setattr(self, "_resize_job", None),
                                                        self.render(quick=True)))

    def _light_changed(self):
        self.reshade()

    def _preset(self, gain, amb, tint):
        self.v_gain.set(gain); self.v_amb.set(amb)
        self.v_r.set(tint[0]); self.v_g.set(tint[1]); self.v_b.set(tint[2])
        self.reshade()

    def _pick(self, x, y):
        """Click-to-select: the G-buffer already knows which triangle owns every pixel."""
        if not (self._g and self.scene):
            return
        vw, vh = self._vsize()
        ow, oh = self._g["out"]
        tid = AP.pick(self._g, x * ow / max(vw, 1), y * oh / max(vh, 1))
        if tid is None:
            return
        iid = f"p{int(self.scene['part'][tid])}"
        if iid not in self._partmap:
            return
        parent = self.tv.parent(iid)
        if parent:
            self.tv.item(parent, open=True)
        self.tv.selection_set(iid)
        self.tv.focus(iid)
        self.tv.see(iid)

    def flash(self, n=6):
        """Blink the selected part so you can tell at a glance which piece it is."""
        if self._sel_part() is None or not self._g:
            self._flash_on = True
            self.reshade()
            return
        self._flash_n = n
        self._flash_step()

    def _flash_step(self):
        if self._flash_n <= 0:
            self._flash_on = True
            self.reshade()
            return
        self._flash_n -= 1
        self._flash_on = not self._flash_on
        self.reshade()
        self.app.after(150, self._flash_step)

    def _shade_args(self):
        # gain/tint are multiplicative on the bake, so previewing them is just a colour scale
        exp = self.v_exp.get() * self.v_gain.get()
        tint = np.array([self.v_r.get(), self.v_g.get(), self.v_b.get()], np.float32) * exp
        return dict(exposure=tint,
                    highlight_part=self._sel_part() if self._flash_on else None)

    def _camkey(self):
        c = self.cam
        return (round(c["yaw"], 4), round(c["pitch"], 4), round(c["zoom"], 5),
                round(c["pan"][0], 4), round(c["pan"][1], 4), round(self.v_cut.get(), 3),
                self._sel_part() if self.v_iso.get() else None)

    def _gkey(self, W, H, ss):
        return (W, H, ss) + self._camkey()

    def _best(self):
        """Resolution/supersampling for the final, look-at-it-properly frame."""
        W, H = self._vsize()
        return W, H, (2 if W * H <= 1_000_000 else 1)

    def render(self, quick=False, ss=1):
        """Re-rasterize. quick = half resolution, used while dragging so the view keeps up."""
        if not self.scene:
            return
        W, H = self._vsize()
        if quick:
            W, H = W // 2, H // 2
        self._submit(("draw", W, H, ss))

    def reshade(self):
        """Re-run the shading pass only — no rasterization. This is the live-slider path.

        It deliberately prefers the small draft G-buffer when one is available for this camera:
        shading it costs ~40 ms against ~550 ms for the supersampled one, so the sliders track
        the mouse. The crisp version follows ~300 ms after you let go.
        """
        if not self.scene:
            return
        if self._g is None and self._gfast is None:
            return self.render(quick=True)
        self._submit(("shade",))

    def _submit(self, job):
        if self.busy:
            # keep the newest request only; a draw always beats a pending shade
            if not (self._pending and self._pending[0] == "draw" and job[0] == "shade"):
                self._pending = job
            return
        self.busy = True
        if self._refine_job:
            self.app.after_cancel(self._refine_job)
            self._refine_job = None
        scene, sh = self.scene, self._shade_args()
        cam = dict(self.cam, pan=tuple(self.cam["pan"]))
        cut, iso = self.v_cut.get(), (self._sel_part() if self.v_iso.get() else None)
        if job[0] == "draw":
            _, W, H, ss = job
            key = self._gkey(W, H, ss)
            g = self._g if (self._g is not None and self._g["key"] == key) else None
        else:
            W = H = ss = 0
            ck = self._camkey()
            g = self._gfast if (self._gfast is not None and self._gfast["key"][3:] == ck) \
                else self._g
            if g is None:
                self.busy = False
                return self.render(quick=True)
            key = g["key"]

        def work():
            try:
                gg = g
                if gg is None:
                    gg = AP.raster(scene, W, H, cam["yaw"], cam["pitch"], cam["zoom"],
                                   cam["pan"], cutaway=cut, isolate=iso, ss=ss)
                    gg["key"] = key
                im, err = AP.shade(scene, gg, **sh), None
            except Exception as e:
                gg, im, err = None, None, str(e)
                self._log(f"render: {traceback.format_exc()}")

            def done():
                self.busy = False
                if gg is not None:
                    if gg["W"] * gg["H"] <= 300_000:
                        self._gfast = gg          # cheap buffer to shade while sliders move
                    if self._g is None or gg["W"] * gg["H"] >= self._g["W"] * self._g["H"] \
                            or gg["key"][3:] != self._g["key"][3:]:
                        self._g = gg
                    self._shownkey = gg["key"]
                if im is not None:
                    self._show(im)
                elif err:
                    self.v_status.set(f"preview: {err}")
                nxt, self._pending = self._pending, None
                if nxt:
                    self._submit(nxt)
                elif not err:
                    self._schedule_refine()
            self.app.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _schedule_refine(self):
        """Once the user stops interacting, redraw crisp: full resolution and 2x supersampled.

        Drafts are half resolution so dragging stays responsive; this is what actually makes the
        picture worth judging lighting by. It is cancelled the moment anything else is queued.
        """
        W, H, ss = self._best()
        if self._shownkey == self._gkey(W, H, ss):
            return
        if self._refine_job:
            self.app.after_cancel(self._refine_job)
        self._refine_job = self.app.after(
            320, lambda: (setattr(self, "_refine_job", None), self.render(ss=ss)))

    def _show(self, im):
        from PIL import ImageTk
        if im is None:
            self.canvas.configure(image="")
            self._img = None
            return
        w, h = self._vsize()
        if im.size != (w, h):
            im = im.resize((w, h), Image.BILINEAR)
        self._img = ImageTk.PhotoImage(im)
        self.canvas.configure(image=self._img)

    def _selected(self):
        """Tree selection changed: update the side panel, then flash the part in the view so
        you can see WHICH piece of the arena it is without hunting for it."""
        self._show_sel()
        if self.v_iso.get():
            self.render()                 # isolate changes what is drawn, not just its colour
        else:
            self.flash()

    def _show_sel(self):
        s = self.tv.selection()
        if not (s and s[0].startswith("p")):
            self.thumb.configure(image=""); self.thumb.image = None
            mi = self._sel_model()
            if mi is None:
                self.v_sel.set("(select a part on the left, or click one in the view)")
                self.v_budget.set("")
                self.v_dims.set("")
                return
            m = self.models[mi]
            self.v_sel.set(f"model {mi:02d} — {m['recs']} parts, {m['nvtx']:,} vertices, "
                           f"{m['tris']:,} triangles, materials "
                           + ", ".join(str(x) for x in m["mats"]))
            self.v_budget.set("Export the whole model to get every part in one file; the group "
                              "names carry the part numbers, so Replace selected MODEL puts each "
                              "one back where it came from.")
            # the model's own bounding box, from the boxes of the parts under it
            boxes = [(self.scene or {}).get("pbox", {}).get(p)
                     for p in self._modelparts.get(mi, [])]
            boxes = [b for b in boxes if b is not None]
            if boxes:
                lo = np.min([b[0] for b in boxes], 0)
                hi = np.max([b[1] for b in boxes], 0)
                d, c = (hi - lo) / 100.0, (hi + lo) / 200.0
                self.v_dims.set(f"size {d[0]:.1f} × {d[1]:.1f} × {d[2]:.1f} m   "
                                f"centre ({c[0]:.1f}, {c[1]:.1f}, {c[2]:.1f}) m   "
                                f"floor at {lo[1] / 100.0:+.1f} m")
            else:
                self.v_dims.set("")
            return
        hit = self._partmap.get(s[0])
        # Size and placement in the real arena are the quickest way to tell what a part IS —
        # a 40 x 30 x 25 m box 20 m up is the jumbotron, a 0.1 m thick slab at floor level is
        # the ice. Units in the file are centimetres.
        box = (self.scene or {}).get("pbox", {}).get(int(s[0][1:]))
        if box is not None:
            lo, hi = box
            d = (hi - lo) / 100.0
            c = (hi + lo) / 200.0
            self.v_dims.set(f"size {d[0]:.1f} × {d[1]:.1f} × {d[2]:.1f} m   "
                            f"centre ({c[0]:.1f}, {c[1]:.1f}, {c[2]:.1f}) m   "
                            f"floor at {lo[1] / 100.0:+.1f} m")
        else:
            self.v_dims.set("")
        if hit:
            b = AM.part_budget(hit[1])
            self.v_budget.set(f"Part {hit[1]['rec']}: {b['cur_tris']:,} triangles now. A "
                              f"replacement may use up to {b['max_verts']:,} vertices and "
                              f"{b['max_tris']:,} triangles — the slot in the file is fixed, so "
                              "anything bigger is refused rather than half-written.")
        vals = self.tv.item(s[0], "values")
        mat = vals[1]
        ti = vals[2]
        rt = AM.material_runtime(self.iff, int(mat)) if str(mat).isdigit() else None
        if rt is not None:
            self.v_sel.set(
                f"material {mat} — {rt['label']}. The game rewrites these pixels every frame "
                f"and the mesh only supplies a 0→1 UV quad, so there is no texture here to "
                f"replace. Keep this part on material {mat} with 0→1 UVs and a new model keeps "
                "the live feed; the artwork it is built from lives in led_<team>.iff.")
            self.thumb.configure(image=""); self.thumb.image = None
            return
        if ti == "":
            self.v_sel.set(f"material {mat} — no texture binding known for this arena "
                           "(only the bowl's materials were mapped from a capture)")
            self.thumb.configure(image=""); self.thumb.image = None
            return
        r = self.texrecs.get(int(ti))
        self.v_sel.set(f"material {mat} → texture {ti}   {r['w']}×{r['h']} {r['fmt']}"
                       if r else f"material {mat} → texture {ti}")
        arr = self._texcache.get(int(ti))
        if arr is None:
            self.thumb.configure(image=""); self.thumb.image = None
            return
        from PIL import Image, ImageTk
        im = Image.fromarray(arr).resize((160, 160))
        self.thumb.image = ImageTk.PhotoImage(im)
        self.thumb.configure(image=self.thumb.image)

    # ────────────────────────────── model export / replace ──────────────────
    def _sel_model_part(self):
        s = self.tv.selection()
        hit = getattr(self, "_partmap", {}).get(s[0]) if s else None
        if not hit:
            messagebox.showinfo("Arena", "Select a part (open a model in the list on the left).")
            return None, None
        mi, sub = hit
        return self.models[mi], sub

    def export_part(self):
        m, sub = self._sel_model_part()
        if not m:
            return
        p = filedialog.asksaveasfilename(
            defaultextension=".obj", filetypes=[("Wavefront OBJ", "*.obj")],
            initialfile=f"{Path(self.iff).stem}_part{sub['rec']:03d}.obj")
        if not p:
            return
        try:
            AM.export_part_obj(self.iff, self.blob, m, sub, Path(p))
            self.v_status.set(f"exported part {sub['rec']} -> {p}")
        except Exception as e:
            messagebox.showerror("Arena", str(e))

    def export_model(self):
        """The whole selected model — every submesh — as one editable OBJ."""
        mi = self._sel_model()
        if mi is None:
            messagebox.showinfo("Arena", "Select a model (or any part of one) on the left.")
            return
        d = filedialog.askdirectory(title=f"Folder for model {mi:02d} (.obj + .mtl + textures)")
        if not d:
            return

        def work():
            try:
                p = AM.export_model_obj(self.iff, Path(d), mi, blob=self.blob,
                                        models=self.models, log=self._log)
                st, err = f"exported model {mi:02d} -> {p}", None
            except Exception as e:
                st, err = None, str(e)
                self._log(f"export model: {traceback.format_exc()}")
            self.app.after(0, lambda: (messagebox.showerror("Arena", err) if err else None,
                                       self.v_status.set(err or st)))
        self._run(work, f"exporting model {mi:02d} …")

    def replace_model(self):
        """Put an edited whole-model OBJ back: each group lands on the submesh it came from."""
        mi = self._sel_model()
        if mi is None:
            messagebox.showinfo("Arena", "Select a model (or any part of one) on the left.")
            return
        root = self._root()
        if not root:
            messagebox.showwarning("Arena", "Set the game files folder in Settings first.")
            return
        p = filedialog.askopenfilename(
            title=f"Replace model {mi:02d} — groups named m##_sub###_mat# map back to parts",
            filetypes=[("Wavefront OBJ", "*.obj")])
        if not p:
            return

        def work():
            try:
                buf = bytearray(AM.dram(self.iff, current=True) or self.blob)
                # force every group onto the model the user picked, so one LOD's export can be
                # pasted onto the other without editing the file
                msgs = AM.replace_model_obj(buf, self.models, p, mi=mi, log=self._log)
                st = f"{len(msgs)} parts; " \
                     f"{AM.write_dram(self.iff, bytes(buf), root, log=self._log)}"
                for msg in msgs:
                    self._log("  " + msg)
                err = None
            except Exception as e:
                st, err = None, str(e)
                self._log(f"replace model: {traceback.format_exc()}")
            self.app.after(0, lambda: (messagebox.showerror("Arena", err) if err else None,
                                       self._after_write(err or st, reload=not err)))
        self._run(work, f"replacing model {mi:02d} …")

    def replace_part(self):
        m, sub = self._sel_model_part()
        if not m:
            return
        root = self._root()
        if not root:
            messagebox.showwarning("Arena", "Set the game files folder in Settings first.")
            return
        bud = AM.part_budget(sub)
        p = filedialog.askopenfilename(title=f"Replace part {sub['rec']} — at most "
                                             f"{bud['max_verts']} vertices / "
                                             f"{bud['max_tris']} triangles",
                                       filetypes=[("Wavefront OBJ", "*.obj")])
        if not p:
            return

        def work():
            try:
                mesh = AM.read_obj(p)
                buf = bytearray(AM.dram(self.iff, current=True) or self.blob)
                st = AM.replace_part(buf, m, sub, mesh, log=self._log)
                st = f"{st}; {AM.write_dram(self.iff, bytes(buf), root, log=self._log)}"
                err = None
            except Exception as e:
                st, err = None, str(e)
                self._log(f"replace part: {traceback.format_exc()}")
            self.app.after(0, lambda: (messagebox.showerror("Arena", err) if err else None,
                                       self._after_write(err or st, reload=not err)))
        self._run(work, f"replacing part {sub['rec']} …")

    # ────────────────────────────── actions ──────────────────────────────
    def _tex_index(self):
        s = self.tv.selection()
        if not (s and s[0].startswith("p")):
            return None
        v = self.tv.item(s[0], "values")[2]
        return int(v) if v != "" else None

    def export_texture(self):
        ti = self._tex_index()
        if ti is None:
            messagebox.showinfo("Arena", "Select a part with a known texture first.")
            return
        p = filedialog.asksaveasfilename(defaultextension=".png",
                                         initialfile=f"{Path(self.iff).stem}_tex{ti:03d}.png",
                                         filetypes=[("PNG", "*.png")])
        if not p:
            return
        A.decode_record(self.iff, self.texrecs[ti]).save(p)
        self.v_status.set(f"exported texture {ti} -> {p}")

    def replace_texture(self):
        ti = self._tex_index()
        if ti is None:
            messagebox.showinfo("Arena", "Select a part with a known texture first.")
            return
        root = self._root()
        if not root:
            messagebox.showwarning("Arena", "Set the game files folder in Settings first.")
            return
        r = self.texrecs[ti]
        p = filedialog.askopenfilename(title=f"Replace texture {ti} ({r['w']}×{r['h']} {r['fmt']})",
                                       filetypes=[("Images", "*.png *.dds *.tga *.bmp *.jpg")])
        if not p:
            return

        def work():
            try:
                st = A.replace_many(self.iff, [dict(r, path=p)], root, log=self._log)
                err = None
            except Exception as e:
                st, err = None, str(e)
                self._log(f"replace tex {ti}: {traceback.format_exc()}")
            self.app.after(0, lambda: self._after_write(err or st, reload=not err))
        self._run(work, f"replacing texture {ti} …")

    def apply_light(self):
        root = self._root()
        if not root:
            messagebox.showwarning("Arena", "Set the game files folder in Settings first.")
            return
        if not self.models:
            return
        gain, amb = self.v_gain.get(), self.v_amb.get()
        tint = (self.v_r.get(), self.v_g.get(), self.v_b.get())
        if abs(gain - 1) < 1e-3 and abs(amb - 1) < 1e-3 and all(abs(t - 1) < 1e-3 for t in tint):
            messagebox.showinfo("Arena", "The sliders are all at 1.0 — nothing to apply.")
            return

        def work():
            try:
                # applied on top of what is in the game files now (so a model replacement made
                # earlier survives). relight is multiplicative, so two applies compound — the
                # sliders snap back to 1.0 afterwards to make that obvious.
                buf = bytearray(AM.dram(self.iff, current=True) or self.blob)
                n = AM.relight(buf, self.models, gain, tint, amb)
                st = AM.write_dram(self.iff, bytes(buf), root, log=self._log)
                st = f"{n:,} vertices relit — {st}"
                err = None
            except Exception as e:
                st, err = None, str(e)
                self._log(f"apply lighting: {traceback.format_exc()}")
            self.app.after(0, lambda: (messagebox.showerror("Arena", err) if err else None,
                                       self._after_write(err or st, reload=not err)))
        self._run(work, "applying lighting …")

    def restore(self):
        root = self._root()
        if not root:
            messagebox.showwarning("Arena", "Set the game files folder in Settings first.")
            return
        if not messagebox.askyesno("Arena", f"Restore the original {self.iff} geometry and "
                                            "lighting? Texture edits are not affected."):
            return

        def work():
            try:
                st, err = AM.restore(self.iff, root, log=self._log), None
            except Exception as e:
                st, err = None, str(e)
                self._log(f"restore: {traceback.format_exc()}")
            self.app.after(0, lambda: self._after_write(err or st, reload=not err))
        self._run(work, "restoring …")

    def export_obj(self):
        d = filedialog.askdirectory(title="Export arena OBJ into…")
        if not d:
            return

        def work():
            try:
                p = AM.export_obj(self.iff, Path(d), self.blob, self.models, log=self._log)
                st, err = f"exported {p}", None
            except Exception as e:
                st, err = None, str(e)
                self._log(f"export obj: {traceback.format_exc()}")
            self.app.after(0, lambda: self._after_write(err or st, reload=False))
        self._run(work, "exporting OBJ (this takes a few seconds) …")

    def check_room(self, auto=False):
        """Measure the spare bytes in the archive slot.

        headroom() re-compresses the whole 2.5 MB blob with the pure-Python LZ77 encoder — ~20 s,
        and because that loop holds the GIL it starves the preview of CPU for the whole time.
        So it runs only on demand (or free, straight from the cache after a reload).
        """
        iff = self.iff
        if not iff:
            return
        room = self._roomcache.get(iff, ...)
        if room is ...:
            if auto:                                # not cached and nobody asked: don't pay 20 s
                self.v_room.set("Archive slot space not measured — press “Check free space”. "
                                "An edit that doesn't fit is refused, never half-written.")
                return
            self.v_room.set("measuring …")

            def work():
                try:
                    r = AM.headroom(iff)
                except Exception as e:
                    r = None
                    self._log(f"headroom: {e}")
                self._roomcache[iff] = r
                self.app.after(0, lambda: self.check_room() if iff == self.iff else None)
            threading.Thread(target=work, daemon=True).start()
            return
        self.v_room.set(
            f"Compressed slot {room['slot']:,} B, currently {room['packed']:,} B — "
            f"{room['free']:,} B spare. An edit that doesn't fit is refused, never half-written."
            if room else "Could not measure the archive slot.")

    def _run(self, work, msg):
        self.v_status.set(msg)
        threading.Thread(target=work, daemon=True).start()

    def _after_write(self, msg, reload=False):
        self.v_status.set(msg or "")
        self._log(msg or "")
        if reload:
            self._texcache.clear()
            self._roomcache.pop(self.iff, None)     # the slot usage just changed
            self.load()
        elif msg:
            self._preset(1, 1, (1, 1, 1))
