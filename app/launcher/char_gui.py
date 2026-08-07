"""char_gui.py — the Players tab: preview, export and replace the character models.

Every character mesh in the game lives in ONE asset, `global.iff` — 50 models, of which the
skater and the goalie are the two anybody wants. char_model finds them by signature and decodes
them in real units, so this tab needs no capture and no per-team file.

Two things about character geometry are not like the arena:

* `+0x24` is a per-slot SELECTION BIT, not a LOD. The file holds every alternative body,
  glove and pad side by side and the engine turns one on per slot, so "the model" is a loadout
  picked across groups — the goalie draws 16 of its 49 records. "Drawn parts only" uses the set
  a capture actually measured (char_model.DRAWN); with no capture for a model, everything shows.
* There is no baked lighting to preview, so the shading here is a plain lambert off the vertex
  normals. Characters are textured by the uniform system, not by a material table in the asset.

Replacement is size-preserving, exactly as it is for arenas: a part's vertex and index slots are
fixed, and an OBJ that keeps the original topology keeps the artist's own index strips.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path

from PIL import Image
from tkinter import (BOTH, BooleanVar, E, END, LEFT, RIGHT, StringVar, DoubleVar, W, X, Y,
                     VERTICAL, Label, filedialog, messagebox, ttk)

from . import arena_preview as AP
from . import char_model as CM


def build_tab(app, frame):
    tab = CharTab(app, frame)
    app._chars = tab
    return tab


class CharTab:
    def __init__(self, app, root):
        self.app = app
        self.blob = None                # the CURRENT game copy of blob 0
        self.models = []
        self._srcmap = {}               # Source picker label -> asset name
        self.mi = None                  # model being shown
        self.scene = None
        self.parts = []
        self._partmap = {}              # tree iid -> submesh dict
        self.busy = False
        self._pending = None
        self._img = None
        self._g = self._gfast = self._shownkey = None
        self._room = None
        self._flash_on = True
        self._flash_n = 0
        self._resize_job = self._refine_job = None
        self.cam = dict(yaw=0.35, pitch=0.10, zoom=1.0, pan=[0.0, 0.0])
        self._drag = None

        head = ttk.Frame(root, padding=(12, 10, 12, 4)); head.pack(fill=X)
        ttk.Label(head, text="Players", font=("Segoe UI", 13, "bold")).pack(side=LEFT)
        ttk.Label(head, text="   Source").pack(side=LEFT)
        self.v_source = StringVar(value="")
        self.cb_src = ttk.Combobox(head, textvariable=self.v_source, width=44, state="readonly")
        self.cb_src.pack(side=LEFT, padx=4)
        self.cb_src.bind("<<ComboboxSelected>>", lambda e: self.load())
        ttk.Label(head, text="   Model").pack(side=LEFT)
        self.v_model = StringVar(value="")
        self.cb = ttk.Combobox(head, textvariable=self.v_model, width=52, state="readonly")
        self.cb.pack(side=LEFT, padx=4)
        self.cb.bind("<<ComboboxSelected>>", lambda e: self.show(self._cb_index()))
        ttk.Button(head, text="Reload", command=self.load).pack(side=LEFT, padx=4)
        self.v_status = StringVar(value="")
        ttk.Label(head, textvariable=self.v_status, foreground="#999").pack(side=RIGHT)

        ttk.Label(root, foreground="#999", font=("Segoe UI", 8), justify=LEFT, wraplength=1000,
                  text="global.iff holds the skater, the goalie, the sticks and the puck. The "
                       "Source picker also opens the 447 per-player face assets and the menu / "
                       "ceremony scenes (trophies, zambonis). Named models are identified from "
                       "captures; the rest are real geometry nobody has put a name to yet. "
                       "Drag to orbit, right-drag to pan, wheel to zoom."
                  ).pack(fill=X, padx=12)

        body = ttk.Frame(root); body.pack(fill=BOTH, expand=True, padx=12, pady=6)
        lf = ttk.Frame(body); lf.pack(side=LEFT, fill=Y)
        ctl = ttk.Frame(body, width=250); ctl.pack(side=RIGHT, fill=Y)
        ctl.pack_propagate(False)
        mid = ttk.Frame(body, padding=(10, 0)); mid.pack(side=LEFT, fill=BOTH, expand=True)
        self._mid, self._ctl = mid, ctl

        cols = ("tris", "verts", "mat", "slot")
        tv = ttk.Treeview(lf, columns=cols, height=26, selectmode="browse")
        tv.heading("#0", text="Part"); tv.column("#0", width=132, anchor=W)
        for c, w, t in (("tris", 62, "Tris"), ("verts", 58, "Verts"),
                        ("mat", 44, "Mat"), ("slot", 62, "Slot")):
            tv.heading(c, text=t); tv.column(c, width=w, anchor=E)
        sb = ttk.Scrollbar(lf, command=tv.yview, orient=VERTICAL)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y); tv.pack(side=LEFT, fill=Y)
        tv.bind("<<TreeviewSelect>>", lambda e: self._selected())
        self.tv = tv

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
        self.v_exp = DoubleVar(value=2.4)
        ttk.Scale(vbar, from_=0.5, to=8.0, variable=self.v_exp, length=120,
                  command=lambda *_: self.reshade()).pack(side=LEFT, padx=4)
        self.v_drawn = BooleanVar(value=True)
        ttk.Checkbutton(vbar, text="Drawn parts only", variable=self.v_drawn,
                        command=self.render).pack(side=LEFT, padx=(8, 0))
        self.v_iso = BooleanVar(value=False)
        ttk.Checkbutton(vbar, text="Isolate", variable=self.v_iso,
                        command=self.render).pack(side=LEFT, padx=(8, 0))
        ttk.Button(vbar, text="Flash", width=6, command=self.flash).pack(side=LEFT, padx=4)
        self.v_info = StringVar(value="")
        ttk.Label(vbar, textvariable=self.v_info, foreground="#999",
                  font=("Segoe UI", 8)).pack(side=RIGHT)
        ttk.Label(mid, foreground="#888", font=("Segoe UI", 7), justify=LEFT,
                  text="Click a surface to select that part. The file carries alternative "
                       "bodies, gloves and pads stacked in the same place — \"Drawn parts only\" "
                       "hides the ones a real frame does not use.").pack(anchor=W, pady=(2, 0))

        sel = ttk.LabelFrame(ctl, text="Selected part", padding=8); sel.pack(fill=X)
        self.v_sel = StringVar(value="(select a part, or click one in the view)")
        ttk.Label(sel, textvariable=self.v_sel, foreground="#aaa", font=("Segoe UI", 8),
                  wraplength=214, justify=LEFT).pack(anchor=W)
        self.v_dims = StringVar(value="")
        ttk.Label(sel, textvariable=self.v_dims, foreground="#7fb0d8", font=("Segoe UI", 7),
                  wraplength=214, justify=LEFT).pack(anchor=W, pady=(2, 0))
        ttk.Button(sel, text="Flash it in the view", command=self.flash).pack(fill=X, pady=(4, 0))

        ex = ttk.LabelFrame(ctl, text="Export / replace", padding=8); ex.pack(fill=X, pady=(8, 0))
        ttk.Button(ex, text="Export whole MODEL (.obj)…",
                   command=lambda: self.export_model(False)).pack(fill=X)
        ttk.Button(ex, text="Export drawn parts only (.obj)…",
                   command=lambda: self.export_model(True)).pack(fill=X, pady=(4, 0))
        ttk.Button(ex, text="Export selected part (.obj)…",
                   command=self.export_part).pack(fill=X, pady=(4, 0))
        ttk.Button(ex, text="Replace MODEL from .obj…", style="Accent.TButton",
                   command=self.replace_model).pack(fill=X, pady=(8, 0))
        ttk.Button(ex, text="Replace selected part from .obj…",
                   command=self.replace_part).pack(fill=X, pady=(4, 0))
        self.v_budget = StringVar(value="")
        ttk.Label(ex, textvariable=self.v_budget, foreground="#888", font=("Segoe UI", 7),
                  wraplength=210, justify=LEFT).pack(anchor=W, pady=(3, 0))

        act = ttk.LabelFrame(ctl, text="Game files", padding=8); act.pack(fill=X, pady=(8, 0))
        ttk.Button(act, text="Restore original characters",
                   command=self.restore).pack(fill=X)
        ttk.Button(act, text="Check free space in the archive slot",
                   command=self.check_room).pack(fill=X, pady=(4, 0))
        self.v_room = StringVar(value="")
        ttk.Label(act, textvariable=self.v_room, foreground="#888", font=("Segoe UI", 7),
                  wraplength=210, justify=LEFT).pack(anchor=W, pady=(3, 0))
        ttk.Label(act, text="Every edit here is written back into global.iff in place: nothing "
                            "may grow, and one slot holds all 50 models.",
                  foreground="#888", font=("Segoe UI", 7), wraplength=210,
                  justify=LEFT).pack(anchor=W, pady=(3, 0))

        app.after(1400, self.load)

    # ────────────────────────────── helpers ──────────────────────────────
    def _log(self, msg):
        try:
            self.app._log_q.put(f"[players] {msg}")
        except Exception:
            pass

    def _root(self):
        return self.app._get_game_root()

    def _label(self, mi, m):
        return (f"{mi:02d}  {m.get('name') or 'unidentified'} — {m['recs']} parts, "
                f"{m['nvtx']:,} v, {m['tris']:,} tris")

    def _cb_index(self):
        try:
            return int(self.v_model.get().split()[0])
        except (ValueError, IndexError):
            return None

    # ────────────────────────────── load ──────────────────────────────
    def _asset(self):
        """The asset the Source picker is on — global.iff until the list has been built."""
        return self._srcmap.get(self.v_source.get(), CM.ASSET)

    def load(self):
        asset = self._asset()
        self.v_status.set(f"Loading {asset} …")
        self.tv.delete(*self.tv.get_children())
        self.scene = self._g = self._gfast = self._shownkey = None
        self._room = None

        def work():
            srcs = None
            try:
                if not self._srcmap:
                    # the source list needs the TOC, so build it off the UI thread too
                    srcs = CM.sources(self._root())
                blob = CM.blob(current=True, asset=asset)
                models = CM.scan_models(blob, asset)
                err = None
            except Exception as e:
                blob, models, err = None, [], str(e)
                self._log(f"load: {traceback.format_exc()}")

            def done():
                if srcs:
                    self._srcmap = {s["label"]: s["asset"] for s in srcs}
                    self.cb_src.configure(values=[s["label"] for s in srcs])
                    if not self.v_source.get():
                        self.v_source.set(srcs[0]["label"])
                if err:
                    self.v_status.set(f"{asset}: {err}")
                    self._show(None)
                    return
                self.blob, self.models = blob, models
                self.cb.configure(values=[self._label(i, m) for i, m in enumerate(models)])
                tris = sum(m["tris"] for m in models)
                self.v_status.set(f"{asset} — {len(models)} models, {tris:,} triangles")
                if not models:
                    self.v_model.set("")
                    self.v_info.set("no geometry in this asset")
                    self._show(None)
                    return
                # open on the skater; it is what anyone comes to this tab for
                start = next((i for i, m in enumerate(models)
                              if (m.get("name") or "").startswith("Skater")), 0)
                self.v_model.set(self._label(start, models[start]))
                self.show(start)
            self.app.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def show(self, mi):
        if mi is None or not (0 <= mi < len(self.models)):
            return
        self.mi = mi
        m = self.models[mi]
        self.parts = CM.submeshes(self.blob, m)
        self._fill_tree()
        self.v_info.set("building preview …")
        self.scene = self._g = self._gfast = self._shownkey = None

        def work():
            try:
                sc, e = CM.build_scene(self.blob, m, asset=self._asset()), None
            except Exception as ex:
                sc, e = None, str(ex)
                self._log(f"model {mi}: {traceback.format_exc()}")

            def done():
                if self.mi != mi:
                    return
                self.scene = sc
                self.v_info.set(f"{len(sc['tri']):,} triangles" if sc else (e or ""))
                self.reset_view()
            self.app.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _fill_tree(self):
        tv = self.tv
        tv.delete(*tv.get_children())
        self._partmap = {}
        ds = CM.drawn_set(self.models[self.mi])
        for k, p in enumerate(self.parts):
            iid = f"p{k + 1}"                       # part ids match build_scene's numbering
            self._partmap[iid] = p
            drawn = ds is None or p["rec"] in ds
            tv.insert("", END, iid=iid,
                      text=f"part {p['rec']:03d}" + ("" if drawn else "   (unused)"),
                      values=(f"{p['tris']:,}", f"{p['n_vtx']:,}", p["mat"],
                              f"0x{p['lod']:X}"))

    # ────────────────────────────── preview ──────────────────────────────
    def reset_view(self):
        self.cam.update(yaw=0.35, pitch=0.10, zoom=1.0, pan=[0.0, 0.0])
        self.render()

    def _visible(self):
        """Part ids to draw — everything, or only the ones a capture says are used."""
        if not self.v_drawn.get() or self.mi is None:
            return None
        ds = CM.drawn_set(self.models[self.mi])
        if ds is None:
            return None
        return frozenset(k + 1 for k, p in enumerate(self.parts) if p["rec"] in ds)

    def _sel_part(self):
        s = self.tv.selection()
        return int(s[0][1:]) if s and s[0].startswith("p") else None

    def _isolate(self):
        vis = self._visible()
        if self.v_iso.get():
            sp = self._sel_part()
            return None if sp is None else frozenset({sp})
        return vis

    def _vsize(self):
        return max(self.view.winfo_width(), 320), max(self.view.winfo_height(), 240)

    def _down(self, e, mode):
        self._drag = (mode, e.x, e.y, e.x, e.y)

    def _up(self, e):
        d, self._drag = self._drag, None
        if d and d[0] == "orbit" and abs(e.x - d[3]) <= 3 and abs(e.y - d[4]) <= 3:
            self._pick(e.x, e.y)
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
        if not self.scene:
            return
        if self._resize_job:
            self.app.after_cancel(self._resize_job)
        self._resize_job = self.app.after(140, lambda: (setattr(self, "_resize_job", None),
                                                        self.render(quick=True)))

    def _pick(self, x, y):
        if not (self._g and self.scene):
            return
        vw, vh = self._vsize()
        ow, oh = self._g["out"]
        tid = AP.pick(self._g, x * ow / max(vw, 1), y * oh / max(vh, 1))
        if tid is None:
            return
        iid = f"p{int(self.scene['part'][tid])}"
        if iid in self._partmap:
            self.tv.selection_set(iid); self.tv.focus(iid); self.tv.see(iid)

    def flash(self, n=6):
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

    def _camkey(self):
        c = self.cam
        return (round(c["yaw"], 4), round(c["pitch"], 4), round(c["zoom"], 5),
                round(c["pan"][0], 4), round(c["pan"][1], 4), self._isolate())

    def _gkey(self, W, H, ss):
        return (W, H, ss) + self._camkey()

    def _best(self):
        W, H = self._vsize()
        return W, H, (2 if W * H <= 1_000_000 else 1)

    def render(self, quick=False, ss=1):
        if not self.scene:
            return
        W, H = self._vsize()
        if quick:
            W, H = W // 2, H // 2
        self._submit(("draw", W, H, ss))

    def reshade(self):
        if not self.scene:
            return
        if self._g is None and self._gfast is None:
            return self.render(quick=True)
        self._submit(("shade",))

    def _submit(self, job):
        if self.busy:
            if not (self._pending and self._pending[0] == "draw" and job[0] == "shade"):
                self._pending = job
            return
        self.busy = True
        if self._refine_job:
            self.app.after_cancel(self._refine_job)
            self._refine_job = None
        scene = self.scene
        sh = dict(exposure=float(self.v_exp.get()),
                  highlight_part=self._sel_part() if self._flash_on else None)
        cam = dict(self.cam, pan=tuple(self.cam["pan"]))
        iso = self._isolate()
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
                                   cam["pan"], isolate=iso, ss=ss)
                    gg["key"] = key
                im, err = AP.shade(scene, gg, **sh), None
            except Exception as e:
                gg, im, err = None, None, str(e)
                self._log(f"render: {traceback.format_exc()}")

            def done():
                self.busy = False
                if gg is not None:
                    if gg["W"] * gg["H"] <= 300_000:
                        self._gfast = gg
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
        self._show_sel()
        if self.v_iso.get():
            self.render()
        else:
            self.flash()

    def _show_sel(self):
        p = self._partmap.get((self.tv.selection() or [""])[0])
        if not p:
            self.v_sel.set("(select a part, or click one in the view)")
            self.v_dims.set(""); self.v_budget.set("")
            return
        ds = CM.drawn_set(self.models[self.mi])
        used = "" if ds is None else ("  — drawn in game" if p["rec"] in ds
                                      else "  — an alternative the shipped loadout does not use")
        self.v_sel.set(f"part {p['rec']:03d}, material {p['mat']}, slot 0x{p['lod']:X}{used}")
        box = (self.scene or {}).get("pbox", {}).get(self._sel_part())
        if box is not None:
            lo, hi = box
            d, c = hi - lo, (hi + lo) / 2
            self.v_dims.set(f"size {d[0]:.1f} × {d[1]:.1f} × {d[2]:.1f} cm   "
                            f"centre ({c[0]:.0f}, {c[1]:.0f}, {c[2]:.0f}) cm")
        else:
            self.v_dims.set("")
        b = CM.part_budget(p)
        # max_tris is deliberately pessimistic (n_idx / 1.6) because it assumes a generic
        # stripifier; the shipped strips pack tighter, which is why cur_tris can exceed it. Say so,
        # or the panel reads as "this part is already over budget".
        self.v_budget.set(f"Part {p['rec']}: {b['cur_tris']:,} triangles in "
                          f"{b['max_indices']:,} indices. Move the vertices about as you like — "
                          f"the original strips are kept and it always fits. Change the topology "
                          f"and it must restrip into the same {b['max_indices']:,} indices "
                          f"({b['max_verts']:,} vertices max, roughly {b['max_tris']:,} triangles "
                          f"for a mesh that strips less well than the shipped one).")

    # ────────────────────────────── actions ──────────────────────────────
    def _busy(self, msg):
        self.v_status.set(msg)
        self.app.update_idletasks()

    def export_model(self, drawn_only):
        if self.mi is None:
            return
        d = filedialog.askdirectory(title="Where to write the .obj")
        if not d:
            return
        try:
            p = CM.export_model_obj(d, self.mi, b=self.blob, models=self.models,
                                    drawn_only=drawn_only, log=self._log,
                                    asset=self._asset())
            self.v_status.set(f"wrote {p.name}")
            messagebox.showinfo("Exported", f"{p}\n\nGroups are named m<model>_sub<part>_mat<n>; "
                                            "keep them and \"Replace MODEL from .obj\" puts every "
                                            "part back where it came from.")
        except Exception as e:
            self._log(traceback.format_exc())
            messagebox.showerror("Export failed", str(e))

    def export_part(self):
        p = self._partmap.get((self.tv.selection() or [""])[0])
        if self.mi is None or not p:
            return messagebox.showinfo("Players", "Select a part first.")
        dest = filedialog.asksaveasfilename(defaultextension=".obj",
                                            initialfile=f"{Path(self._asset()).stem}_{self.mi:02d}_part{p['rec']:03d}.obj",
                                            filetypes=[("Wavefront OBJ", "*.obj")])
        if not dest:
            return
        try:
            CM.export_part_obj(self.blob, self.models[self.mi], p, dest,
                               asset=self._asset())
            self.v_status.set(f"wrote {Path(dest).name}")
        except Exception as e:
            self._log(traceback.format_exc())
            messagebox.showerror("Export failed", str(e))

    def replace_model(self):
        if self.mi is None:
            return
        path = filedialog.askopenfilename(title="Edited .obj", filetypes=[("Wavefront OBJ", "*.obj")])
        if not path:
            return
        self._apply(lambda buf: CM.replace_model_obj(buf, self.models, path, mi=self.mi,
                                                     log=self._log))

    def replace_part(self):
        p = self._partmap.get((self.tv.selection() or [""])[0])
        if self.mi is None or not p:
            return messagebox.showinfo("Players", "Select a part first.")
        path = filedialog.askopenfilename(title="Edited .obj", filetypes=[("Wavefront OBJ", "*.obj")])
        if not path:
            return
        self._apply(lambda buf: [CM.replace_part_obj(buf, self.models[self.mi], p, path,
                                                     log=self._log)])

    def _apply(self, edit):
        """Run an edit on a copy of blob 0 and write it back — the re-encode takes a while, so
        it happens off the UI thread."""
        root = self._root()
        if not root:
            return messagebox.showinfo("Players", "Set the game files folder in Settings first.")
        asset = self._asset()
        self._busy("applying …")

        def work():
            try:
                buf = bytearray(self.blob)
                msgs = edit(buf)
                msgs.append(CM.write(buf, root, log=self._log, asset=asset))
                err = None
            except Exception as e:
                msgs, err = [], str(e)
                self._log(traceback.format_exc())

            def done():
                if err:
                    self.v_status.set("nothing written")
                    messagebox.showerror("Replace failed", err)
                    return
                for m in msgs:
                    self._log(m)
                self.v_status.set(msgs[-1])
                messagebox.showinfo("Done", "\n".join(msgs))
                self.load()
            self.app.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def restore(self):
        root = self._root()
        if not root:
            return messagebox.showinfo("Players", "Set the game files folder in Settings first.")
        if not messagebox.askyesno("Restore", "Put the shipped character geometry back?\n\n"
                                              "Every model in global.iff returns to stock."):
            return
        self._busy("restoring …")

        def work():
            try:
                msg, err = CM.restore(root, log=self._log, asset=self._asset()), None
            except Exception as e:
                msg, err = None, str(e)
                self._log(traceback.format_exc())

            def done():
                self.v_status.set(err or msg)
                self.load()
            self.app.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def check_room(self):
        """How much of the compressed slot the asset uses. The re-encode is slow (~2 min for a
        22 MB blob), so it runs once and is remembered until something is written."""
        if self._room:
            return self.v_room.set(self._room)
        self.v_room.set("measuring … (this takes a couple of minutes)")

        def work():
            try:
                h = CM.headroom(asset=asset)
                if not h:
                    raise ValueError("global.iff: no pristine copy to measure against")
                txt = (f"{h['packed']:,} of {h['slot']:,} bytes used — {h['free']:,} free. "
                       "An edit has to re-compress into that; a whole-model reshape typically "
                       "costs a couple of kilobytes.")
            except Exception as e:
                txt = str(e)
            self.app.after(0, lambda: (setattr(self, "_room", txt), self.v_room.set(txt)))
        threading.Thread(target=work, daemon=True).start()
