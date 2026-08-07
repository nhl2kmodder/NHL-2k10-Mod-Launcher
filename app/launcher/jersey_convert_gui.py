"""jersey_convert_gui.py — the Jersey Editor tab: edit a 2K10 kit, optionally from NHL 23 art.

All of the image work is in `jersey_convert` (pure, no UI, CLI-testable). This module is the
tab around it: pick a kit, look at the four sheets it currently wears, change what you want to
change, and write it back.

Picking a kit LOADS IT. That is the default mode and it is deliberate: the tab is a jersey
editor first and an NHL 23 converter second, so "VAN home" means whatever VAN home is in the
game files right now, mods included. Conversion is an optional source of new art — until you
press Convert, every sheet you see is the kit's own and every edit lands on top of it
(`build_stamps(..., canvas=...)`, which edits a sheet instead of composing one).

Four things here are worth knowing before you read the code:

* **The stock kit is the layout template.** Slot rectangles come from the profile, but the art
  inside each slot is fitted to the STOCK sheet's own ink box, so a kit whose crest is small
  stays small. That is why the target picker is not optional in practice — convert with no
  target and every slot falls back to a generic inset.
* **Dragging a slot only rebuilds the stamps sheet.** A full convert re-warps a 2048² base and
  re-segments the font (a couple of seconds); the glyphs and lifted logos are kept on the last
  Result so a drag re-runs `build_stamps` alone, which is fast enough to feel live.
* **A stamps edit fans out.** The in-game uniform's `stamps` is byte-identical to the front-end
  team-select sheet's `decals` — Apply mirrors it through `jerseys.MIRRORED_INDICES`, exactly
  like the IFF tab does, or the jersey changes in-game and not on the team-select screen.
"""
from __future__ import annotations

import math
import re
import shutil
import tempfile
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import (BOTH, BOTTOM, E, END, HORIZONTAL, LEFT, RIGHT, StringVar, BooleanVar,
                     IntVar, VERTICAL, W, X, Y, filedialog, messagebox, ttk)

from PIL import Image, ImageTk

from . import archive_textures as archtex
from . import jersey_convert as J
from . import jersey_preview as JP
from . import jerseys
from . import normal_stitcher as NS
from . import stamp_shader as SS
from . import uniform_colors as UC

KIT_ORDER = ("Home", "Away", "Alt")
SHEETS = ("base", "stamps", "letters", "helmet")
SHEET_LABEL = {"base": "Base", "stamps": "Stamps", "letters": "Letters", "helmet": "Helmet"}
KIND_LABEL = {"base": "Base colour", "pant": "Pants", "sock": "Socks", "font": "Font sheet"}
HANDLE = 7                      # corner grab size, in SCREEN pixels

# Slots whose art is a literal, un-keyed mark: the shader substitutes nothing for these, so the
# colour stored IS the colour rendered and the tab paints them itself (see J.tint_art).
DEFAULT_TINT = "#FFFFFF"

# Two rows the slot table carries that are not stamps slots: the nameplate alphabet and the helmet
# digits live on their own sheets and have no rect, but they DO have a size, and a size control
# that lives anywhere other than the size column would be a second place to look.
#   (row id, label, which sheet a change rebuilds)
EXTRA_SIZE_ROWS = (
    ("letters", "name letters", "letters"),
    ("helmet_number", "helmet numbers", "helmet"),
)
EXTRA_SIZE_SHEET = {k: s for k, _, s in EXTRA_SIZE_ROWS}

# Columns of the slot table, and which of them can be typed into directly.
SLOT_COLS = ("on", "x", "y", "w", "h", "size")
EDIT_COLS = ("x", "y", "w", "h", "size")


def build_tab(app, frame):
    tab = JerseyConvertTab(app, frame)
    app._jersey_convert = tab
    return tab


# ── the preview canvas ────────────────────────────────────────────────────────────────────────

class SheetView:
    """One sheet on a zoom-to-fit canvas, optionally with draggable slot boxes.

    Boxes are drawn and hit-tested in SCREEN space but stored in TEXTURE space, so a slot keeps
    its pixel rect no matter how the pane is sized.
    """

    def __init__(self, parent, on_change=None, on_select=None):
        self.canvas = tk.Canvas(parent, bg="#141414", highlightthickness=0)
        self.canvas.pack(fill=BOTH, expand=True)
        self.on_change = on_change
        self.on_select = on_select
        self.img = None
        self.rects: dict = {}
        self.enabled: dict = {}
        self.selected = None
        self._tk = None
        self._scale = 1.0
        self._ox = self._oy = 0
        self._drag = None
        self._job = None
        self.canvas.bind("<Configure>", lambda e: self._later())
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._hover)

    # -- content ------------------------------------------------------------------------------
    def set_image(self, img):
        self.img = img
        self._later()

    def set_boxes(self, rects, enabled, selected):
        self.rects, self.enabled, self.selected = rects, enabled, selected
        self._later()

    def _later(self, ms=40):
        if self._job:
            self.canvas.after_cancel(self._job)
        self._job = self.canvas.after(ms, self.render)

    # -- drawing ------------------------------------------------------------------------------
    def render(self):
        self._job = None
        c = self.canvas
        c.delete("all")
        if self.img is None:
            c.create_text(20, 20, anchor="nw", fill="#888",
                          text="Nothing converted yet — pick the NHL 23 sources and press Convert.")
            return
        cw, ch = max(c.winfo_width(), 1), max(c.winfo_height(), 1)
        iw, ih = self.img.size
        s = min((cw - 16) / iw, (ch - 16) / ih)
        s = max(s, 0.02)
        self._scale = s
        dw, dh = max(1, int(iw * s)), max(1, int(ih * s))
        self._ox, self._oy = (cw - dw) // 2, (ch - dh) // 2
        shown = _on_checker(self.img).resize((dw, dh), Image.LANCZOS)
        self._tk = ImageTk.PhotoImage(shown)
        c.create_image(self._ox, self._oy, anchor="nw", image=self._tk)
        c.create_rectangle(self._ox, self._oy, self._ox + dw, self._oy + dh, outline="#3a3a3a")
        for name, r in self.rects.items():
            on = self.enabled.get(name, True)
            sel = name == self.selected
            col = "#ffd24a" if sel else ("#4ec9b0" if on else "#7a3b3b")
            x0, y0 = self._to_screen(r[0], r[1])
            x1, y1 = self._to_screen(r[0] + r[2], r[1] + r[3])
            c.create_rectangle(x0, y0, x1, y1, outline=col, width=2 if sel else 1,
                               dash=() if on else (3, 3))
            if sel:
                for hx, hy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                    c.create_rectangle(hx - HANDLE, hy - HANDLE, hx + HANDLE, hy + HANDLE,
                                       outline=col, fill="#3a2f10")
                c.create_text(x0 + 2, y0 - 12, anchor="w", fill=col, text=name,
                              font=("Segoe UI", 8))

    def _to_screen(self, x, y):
        return self._ox + x * self._scale, self._oy + y * self._scale

    def _to_tex(self, x, y):
        return (x - self._ox) / self._scale, (y - self._oy) / self._scale

    # -- interaction --------------------------------------------------------------------------
    def _corner_at(self, name, ex, ey):
        r = self.rects[name]
        x0, y0 = self._to_screen(r[0], r[1])
        x1, y1 = self._to_screen(r[0] + r[2], r[1] + r[3])
        for cx, cy, tag in ((x0, y0, "nw"), (x1, y0, "ne"), (x0, y1, "sw"), (x1, y1, "se")):
            if abs(ex - cx) <= HANDLE and abs(ey - cy) <= HANDLE:
                return tag
        return None

    def _hit(self, ex, ey):
        """(slot, corner-or-None) under the cursor. The SELECTED slot's handles win, then the
        smallest containing slot — the small sponsor marks sit inside no one, but a crest box is
        big enough to swallow a click meant for a digit if area is not the tie-break."""
        if self.selected in self.rects:
            k = self._corner_at(self.selected, ex, ey)
            if k:
                return self.selected, k
        tx, ty = self._to_tex(ex, ey)
        hits = [n for n, r in self.rects.items()
                if r[0] <= tx <= r[0] + r[2] and r[1] <= ty <= r[1] + r[3]]
        if not hits:
            return None, None
        hits.sort(key=lambda n: self.rects[n][2] * self.rects[n][3])
        return hits[0], None

    def _hover(self, e):
        if not self.rects:
            return
        name, corner = self._hit(e.x, e.y)
        self.canvas.config(cursor={"nw": "size_nw_se", "se": "size_nw_se",
                                   "ne": "size_ne_sw", "sw": "size_ne_sw"}.get(corner,
                           "fleur" if name else ""))

    def _press(self, e):
        if not self.rects:
            return
        name, corner = self._hit(e.x, e.y)
        if not name:
            return
        if name != self.selected and self.on_select:
            self.on_select(name)
        self.selected = name
        self._drag = (name, corner, e.x, e.y, list(self.rects[name]))
        self.render()

    def _motion(self, e):
        if not self._drag:
            return
        name, corner, sx, sy, r0 = self._drag
        dx = round((e.x - sx) / self._scale)
        dy = round((e.y - sy) / self._scale)
        x, y, w, h = r0
        if corner is None:
            x, y = x + dx, y + dy
        else:
            if "w" in corner:
                x, w = x + dx, w - dx
            else:
                w = w + dx
            if "n" in corner:
                y, h = y + dy, h - dy
            else:
                h = h + dy
        w, h = max(8, w), max(8, h)
        self.rects[name] = [x, y, w, h]
        self.render()

    def _release(self, e):
        if not self._drag:
            return
        name = self._drag[0]
        self._drag = None
        if self.on_change:
            self.on_change(name, list(self.rects[name]))


def _on_checker(img, cell=16):
    """Composite over a checkerboard so transparent slots read as empty, not as black."""
    w, h = img.size
    bg = Image.new("RGB", (w, h), (58, 58, 58))
    tile = Image.new("RGB", (cell, cell), (74, 74, 74))
    for y in range(0, h, cell * 2):
        for x in range(0, w, cell * 2):
            bg.paste(tile, (x, y))
            bg.paste(tile, (x + cell, y + cell))
    bg.paste(img, (0, 0), img)
    return bg


def _tooltip(widget, text):
    """A hover label. Used where a number needs a sentence of explanation but the panel has no
    room for one per row."""
    tip = {"w": None}

    def show(_e=None):
        if tip["w"] is not None:
            return
        w = tk.Toplevel(widget)
        w.wm_overrideredirect(True)
        w.wm_geometry(f"+{widget.winfo_rootx() + 20}+{widget.winfo_rooty() + 24}")
        tk.Label(w, text=text, bg="#2b2b2b", fg="#ddd", relief="solid", borderwidth=1,
                 padx=6, pady=3, wraplength=260, justify=LEFT).pack()
        tip["w"] = w

    def hide(_e=None):
        if tip["w"] is not None:
            tip["w"].destroy()
            tip["w"] = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)
    widget.bind("<Destroy>", hide)


# ── the tab ───────────────────────────────────────────────────────────────────────────────────

class JerseyConvertTab:
    def __init__(self, app, root):
        self.app = app
        self.root = root
        self.profile = J.load_profile()
        self.regions = J.load_regions()
        self.res: J.Result | None = None
        self.sources: dict[str, Path] = {}
        self.stock: dict[str, Image.Image] = {}
        self.busy = False
        self._stamps_job = None
        self.touched: set = set()
        self._stitched: set = set()      # which normals this session actually regenerated
        # Colours tab state: the RosFile is held open across edits so unsaved picks show on the
        # model, and Save is a single write of that same object.
        self._col_ros = None
        self._col_row = None
        self._col_buttons: dict = {}
        self._col_sel: set = set()       # palette slots the next colour applies to
        self._col_flash = None           # (slots, saved hexes, step) while a flash is running
        self._size_job = None
        self._size_dirty: set = set()

        cfg = self.profile["stamps"]
        self.slot_names = [n for n in cfg["slots"] if not n.startswith("_")]
        self.rects = {n: list(cfg["slots"][n]["rect"]) for n in self.slot_names}
        self.enabled = {n: True for n in self.slot_names}
        # SIZE lives in the same table as the rects, keyed the same way, plus the two rows that
        # have a size but no rect (see EXTRA_SIZE_ROWS). 1.0 == whatever the kit already draws.
        self.sizes = {n: 1.0 for n in self.slot_names}
        self.sizes.update({k: 1.0 for k, _, _ in EXTRA_SIZE_ROWS})
        self.art_cfg = {k: v for k, v in cfg.get("art", {}).items() if not k.startswith("_")}
        # EMPTY, not the profile's bundled replacements: a slot the user hasn't touched keeps
        # whatever it already has. (A conversion still gets the bundled marks -- build_stamps
        # merges them in itself when it composes a sheet from scratch.)
        self.art: dict = {}
        self.helmet_logo: dict = {}
        # True while the previews are the KIT's own sheets rather than a conversion's output.
        # Everything downstream keys off this: edits are composited onto the kit, and Apply only
        # writes the sheets that were actually touched.
        self.edit_mode = False
        self.touched: set = set()

        self.targets: dict[str, tuple[str, str]] = {}   # label -> (base iff, uniform iff)
        self.team_kits: dict[str, list[str]] = {}       # team code -> its variations

        # -- header -----------------------------------------------------------------------
        head = ttk.Frame(root, padding=(12, 10, 12, 4))
        head.pack(fill=X)
        ttk.Label(head, text="Jersey Editor", font=("Segoe UI", 13, "bold")).pack(side=LEFT)
        ttk.Label(head, text="   edit a kit — or convert one in from NHL 23",
                  foreground="#888").pack(side=LEFT)
        self.v_status = StringVar(value="")
        ttk.Label(head, textvariable=self.v_status, foreground="#4ec9b0").pack(side=RIGHT)

        pane = ttk.PanedWindow(root, orient=HORIZONTAL)
        pane.pack(fill=BOTH, expand=True, padx=12, pady=(0, 10))
        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=0)
        pane.add(right, weight=1)

        self._build_controls(left)
        self._build_preview(right)
        self.refresh_targets()

    # ── controls ─────────────────────────────────────────────────────────────────────────
    def _build_controls(self, parent):
        parent.configure(width=430)
        nb = ttk.Notebook(parent)
        self.nb_ctl = nb
        nb.pack(fill=BOTH, expand=True)
        p_src = ttk.Frame(nb, padding=10)
        p_slot = ttk.Frame(nb, padding=10)
        p_art = ttk.Frame(nb, padding=10)
        nb.add(p_src, text="  Sources  ")
        nb.add(p_slot, text="  Stamp slots  ")
        nb.add(p_art, text="  Patch art  ")
        # Colours is a CONTROL, not a preview: it belongs on this side so the model stays visible
        # while you pick, which is the only honest read on a palette edit.
        nb.bind("<<NotebookTabChanged>>", lambda e: self._ctl_tab_changed())

        # -- sources ----------------------------------------------------------------------
        g = ttk.LabelFrame(p_src, text="NHL 23 textures", padding=8)
        g.pack(fill=X)
        row = ttk.Frame(g); row.pack(fill=X, pady=(0, 6))
        self.v_folder = StringVar(value="")
        ttk.Entry(row, textvariable=self.v_folder).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(row, text="Folder…", width=9, command=self.pick_folder).pack(side=LEFT, padx=4)
        self.v_src = {}
        for kind, _ in J.SOURCE_KINDS:
            r = ttk.Frame(g); r.pack(fill=X, pady=1)
            ttk.Label(r, text=KIND_LABEL[kind], width=12).pack(side=LEFT)
            self.v_src[kind] = StringVar(value="—")
            ttk.Label(r, textvariable=self.v_src[kind], foreground="#8ab",
                      width=28, anchor=W).pack(side=LEFT, fill=X, expand=True)
            ttk.Button(r, text="…", width=3,
                       command=lambda k=kind: self.pick_source(k)).pack(side=LEFT)

        g2 = ttk.LabelFrame(p_src, text="Kit to edit", padding=8)
        g2.pack(fill=X, pady=(10, 0))
        # Team and variation are separate pickers: one flat list means scrolling every team's
        # home/away/alt to reach a team, which is 119 rows on a stock install and worse with
        # expansion clubs.
        r = ttk.Frame(g2); r.pack(fill=X)
        self.v_team = StringVar(value="")
        self.v_kit = StringVar(value="")
        self.v_target = StringVar(value="")
        self.cb_team = ttk.Combobox(r, textvariable=self.v_team, state="readonly", width=18)
        self.cb_team.pack(side=LEFT, fill=X, expand=True)
        self.cb_team.bind("<<ComboboxSelected>>", lambda e: self._team_picked())
        self.cb_kit = ttk.Combobox(r, textvariable=self.v_kit, state="readonly", width=10)
        self.cb_kit.pack(side=LEFT, padx=(6, 0))
        self.cb_kit.bind("<<ComboboxSelected>>", lambda e: self._kit_picked())
        ttk.Button(r, text="↻", width=3, command=self.refresh_targets).pack(side=LEFT, padx=4)
        ttk.Label(g2, foreground="#888", wraplength=380, justify=LEFT,
                  text="Picking a kit loads what it wears RIGHT NOW, including anything you have "
                       "already modded, and every edit goes on top of that. It also supplies the "
                       "layout for a conversion: new art is fitted to this jersey's own "
                       "stamp/letter ink boxes, so per-team metrics come for free.").pack(
            fill=X, pady=(6, 0))

        g3 = ttk.LabelFrame(p_src, text="Options", padding=8)
        g3.pack(fill=X, pady=(10, 0))
        # Layer count is MEASURED off the source font, per sheet -- the jersey number, the helmet
        # number and the name letters are three different designs and often carry different
        # numbers of outlines. There is no override any more: it was a checkbox that turned the
        # measurement off and a spinbox that then had to guess one number for three sheets, which
        # is strictly worse than measuring each. Detection is simply always on.
        self.v_strip = BooleanVar(value=True)
        self.v_helm_jersey = BooleanVar(value=True)
        self.v_helm_logo = BooleanVar(value=True)
        ttk.Checkbutton(g3, text="Strip logos/collar off the base (and lift them to stamps)",
                        variable=self.v_strip).pack(anchor=W, pady=(4, 0))
        ttk.Checkbutton(g3, text="Helmet number copied from the jersey number",
                        variable=self.v_helm_jersey).pack(anchor=W)
        ttk.Checkbutton(g3, text="Keep the kit's helmet logo",
                        variable=self.v_helm_logo).pack(anchor=W)

        b = ttk.Frame(p_src); b.pack(fill=X, pady=(12, 0))
        ttk.Button(b, text="Convert", command=self.convert).pack(side=LEFT)
        ttk.Button(b, text="Save PNGs…", command=self.save_pngs).pack(side=LEFT, padx=6)
        ttk.Button(b, text="Apply to game", command=self.apply).pack(side=LEFT)

        self.txt_warn = tk.Text(p_src, height=7, wrap="word", bg="#1b1b1b", fg="#c9a26d",
                                relief="flat")
        self.txt_warn.pack(fill=BOTH, expand=True, pady=(10, 0))

        # -- stamp slots ------------------------------------------------------------------
        ttk.Label(p_slot, foreground="#888", wraplength=390, justify=LEFT,
                  text="Drag a box on the Stamps preview to move it, or grab a corner to resize. "
                       "Turn a slot off to leave it empty — 2K10 stamps a jersey from whichever "
                       "slots carry art, so an unused shoulder patch simply isn't drawn.\n\n"
                       "There are TWO shoulder slots. shoulder_patch is the left one; leave "
                       "shoulder_right empty and the game puts the left mark on both shoulders. "
                       "Only fill it for a kit whose shoulders genuinely differ (Calgary's "
                       "Canada / Alberta flags are the shipped example).\n\n"
                       "These boxes are the sheet side of a stamp — where its art is CUT FROM. "
                       "Where it lands ON THE BODY is not authorable: it comes from the mesh's "
                       "own UVs and the pixel shader's source rects, and the uniform .iff carries "
                       "no placement table (its blob is the slot list, a kerning table, then "
                       "26 KB of fill). Move a crest on the jersey by moving it inside its slot "
                       "art.\n\n"
                       "SIZE is how big the mark is drawn INSIDE its cell, in percent — the only "
                       "size the format allows, for the same reason. Click any number to type a "
                       "new one; click ON to turn a slot off.").pack(
            fill=X, pady=(0, 8))
        self.tree = ttk.Treeview(p_slot, columns=SLOT_COLS, show="tree headings", height=18,
                                 selectmode="browse")
        self.tree.heading("#0", text="Slot")
        self.tree.column("#0", width=128, stretch=False)
        for c, wd in zip(SLOT_COLS, (30, 44, 44, 44, 44, 50)):
            self.tree.heading(c, text=c.upper())
            self.tree.column(c, width=wd, anchor="e", stretch=False)
        self.tree.pack(fill=BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._push_boxes())
        # A single click is what people try first, on the cell they can see. Double-click keeps
        # working for anyone in the habit of it, and space still toggles from the keyboard.
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<Double-1>", self._tree_click)
        self.tree.bind("<space>", self._toggle_slot)
        self._cell_edit = None

        b = ttk.Frame(p_slot); b.pack(fill=X, pady=(8, 0))
        ttk.Button(b, text="Reset slot", command=lambda: self.reset_slots(False)).pack(side=LEFT)
        ttk.Button(b, text="Reset all", command=lambda: self.reset_slots(True)).pack(side=LEFT,
                                                                                     padx=6)
        ttk.Button(b, text="Enable all", command=lambda: self.set_all(True)).pack(side=LEFT)
        ttk.Button(b, text="Disable all", command=lambda: self.set_all(False)).pack(side=LEFT,
                                                                                    padx=6)
        self._fill_tree()

        # -- patch art --------------------------------------------------------------------
        ttk.Label(p_art, foreground="#888", wraplength=390, justify=LEFT,
                  text="2K10 shipped 2009 sponsor marks. These slots take modern art instead. "
                       "Nothing in the game recolours them — whatever colour is stored is what "
                       "renders — so 'Colour' paints the art here, and what you see is what the "
                       "jersey wears.").pack(fill=X, pady=(0, 10))
        self.v_art_file = {}
        self.v_art_tint = {}
        self.b_art_tint = {}
        self.cb_art = {}
        for name, spec in self.art_cfg.items():
            self._art_group(p_art, name, spec)

        # Helmet decal. Same panel because it is the same job -- upload a mark, tint it, done --
        # even though it lands on the helmet sheet rather than the stamps sheet.
        self._art_group(p_art, "helmet_logo", {}, "Helmet logo",
                        "Replaces the helmet decal. Leave empty and the kit keeps its own "
                        "(or, with 'Keep the kit's helmet logo' off, borrows the crest).")

        self._build_colour_tab(nb)
        self._build_normals_tab(nb)

    # ── normals ──────────────────────────────────────────────────────────────────────────
    def _build_normals_tab(self, nb):
        """Re-cut the kit's seams for the art it is wearing now.

        A 2K10 normal map is stitching: raised seam ridges where two panels meet, a dashed thread
        row inset from each seam, and a twill weave inside the panels. All of that is keyed to the
        STOCK stripe layout, so the moment a kit is recoloured or re-striped its relief is wrong —
        seams run through the middle of a solid panel and the new panel edges are glassy.

        There is no team/kit picker here. This page operates on the kit the tab already has open,
        which is the whole reason the stitcher moved off the Textures tab: it used to ask for a
        team a second time and hand back a file you could not see on anything.

        Three sheets carry a normal, and all three are stitchable — see `NS.SHEET_SPECS`.
        """
        p = ttk.Frame(nb, padding=10)
        nb.add(p, text="  Normals  ")
        self._normals_tab = p
        ttk.Label(p, foreground="#888", wraplength=390, justify=LEFT,
                  text="Stitching is generated FROM the colour art: panel edges become seams, "
                       "seams get a thread row, panels get a weave. Stitch after the colours are "
                       "settled — the relief follows whatever the sheets look like at the time.\n\n"
                       "'Heal the stock stitching first' blurs the kit's existing seams out before "
                       "cutting new ones. Leave it on when the stripe layout changed; turn it off "
                       "to keep the original relief and only ADD to it.").pack(fill=X, pady=(0, 8))

        g = ttk.LabelFrame(p, text="Sheets", padding=8)
        g.pack(fill=X)
        self.v_ns_sheet = {}
        for name in NS.SHEET_ORDER:
            spec = NS.SHEET_SPECS[name]
            v = BooleanVar(value=True)
            self.v_ns_sheet[name] = v
            ttk.Checkbutton(g, text=f"{_pretty(name)}  →  {spec['label']}",
                            variable=v).pack(anchor=W)
        self.v_ns_heal = BooleanVar(value=True)
        ttk.Checkbutton(g, text="Heal the stock stitching first",
                        variable=self.v_ns_heal).pack(anchor=W, pady=(6, 0))

        g2 = ttk.LabelFrame(p, text="Stitch style", padding=8)
        g2.pack(fill=X, pady=(10, 0))
        # Typed numbers, not sliders: these are physical measurements in pixels and degrees, and
        # every one of them has a value you can read off the stock kit with "Sample from stock".
        rows = (("color_thr", "panel threshold", "colour distance that counts as a new panel"),
                ("edge_k", "edge sensitivity", "× threshold to call it a panel EDGE (lower = more)"),
                ("ridge_w", "seam width px", "half-width of the raised seam"),
                ("ridge_off", "seam inset px", "how far the seam's crest sits inside the colour "
                                               "edge (the stock art is ~2)"),
                ("ridge_h", "seam height", "how proud the seam sits"),
                ("stitch_off", "thread inset px", "how far the thread row sits from the seam"),
                ("stitch_w", "thread width px", ""),
                ("stitch_spc", "stitch spacing px", "period between stitches"),
                ("dash_len", "dash length px", "must be shorter than the spacing"),
                ("stitch_h", "thread height", ""),
                ("twill_ang", "weave angle °", ""),
                ("twill_prd", "weave period px", ""),
                ("twill_h", "weave height", "keep this subtle"),
                ("strength", "overall relief", "height → normal gain. 0 = match the stock kit's "
                                               "own thread depth (measured)"),
                ("heal_blur", "heal radius px", "only used when healing"),
                ("pre_blur", "art smoothing px", "blurs the colour before edge-finding, so DXT "
                                                 "block ringing does not become relief"),
                ("relief_blur", "relief smoothing px", "takes the pixel staircase off the seams"))
        self.v_ns = {}
        grid = ttk.Frame(g2); grid.pack(fill=X)
        for i, (key, label, tip) in enumerate(rows):
            r, c = divmod(i, 2)
            cell = ttk.Frame(grid)
            cell.grid(row=r, column=c, sticky="ew", padx=(0, 8), pady=1)
            grid.columnconfigure(c, weight=1)
            ttk.Label(cell, text=label, width=17).pack(side=LEFT)
            v = StringVar(value=f"{NS.DEFAULTS[key]:g}")
            self.v_ns[key] = v
            e = ttk.Entry(cell, textvariable=v, width=7, justify="right")
            e.pack(side=LEFT)
            if tip:
                _tooltip(e, tip)

        b = ttk.Frame(p); b.pack(fill=X, pady=(10, 0))
        ttk.Button(b, text="Stitch", command=self.stitch_normals).pack(side=LEFT)
        ttk.Button(b, text="Sample from stock", command=self.sample_normals).pack(side=LEFT,
                                                                                  padx=6)
        ttk.Button(b, text="Defaults", command=self.reset_normals).pack(side=LEFT)
        ttk.Button(b, text="Revert to stock", command=self.revert_normals).pack(side=LEFT, padx=6)
        self.v_ns_status = StringVar(value="")
        ttk.Label(p, textvariable=self.v_ns_status, foreground="#4ec9b0",
                  wraplength=390, justify=LEFT).pack(fill=X, pady=(8, 0))

    def _ns_params(self):
        """The typed style numbers, falling back per-field to the default on anything unparseable
        (an empty box while you are mid-edit should not abort a stitch)."""
        p = dict(NS.DEFAULTS)
        for k, v in self.v_ns.items():
            try:
                p[k] = float(v.get())
            except ValueError:
                pass
        return p

    def reset_normals(self):
        for k, v in self.v_ns.items():
            v.set(f"{NS.DEFAULTS[k]:g}")

    def sample_normals(self):
        """Measure this kit's own thread style off its stock base pair and load it into the boxes.

        The base sheet is the one to measure: it is the largest, it always exists, and it is the
        only one whose stitching is real garment seams rather than embroidery around a decal.
        """
        if not (self.stock.get("base") is not None and self.stock.get("base_normal") is not None):
            self.v_ns_status.set("no stock base normal to measure — load a kit first")
            return
        got = NS.sample_style(NS.to_color(self.stock["base"]),
                              NS.to_color(self.stock["base_normal"]), self._ns_params())
        for k, v in self.v_ns.items():
            # `strength` is deliberately NOT loaded. It is the one number that must not be shared
            # between sheets: measured on the shipped Calgary home kit, the seam relief in excess of
            # the panel's own cloth is 1.2x on the base sheet, 10x on letters and 28x on stamps.
            # Pinning the box to the base sheet's gain drove the embroidery sheets with it, and 0
            # already means "measure this sheet's own", which is what the game's art shows.
            v.set("0" if k == "strength" else f"{float(got[k]):g}")
        self.v_ns_status.set("sampled ridge width, stitch spacing and weave period off the stock "
                             "base normal — depth stays per-sheet (strength 0 = measured)")

    def stitch_normals(self):
        """Regenerate the enabled sheets' normals from the CURRENT colour art."""
        if not (self.res and self.res.sheets):
            self.v_ns_status.set("load a kit first")
            return
        want = [n for n in NS.SHEET_ORDER if self.v_ns_sheet[n].get()]
        have = [n for n in want
                if self.stock.get(n) is not None and self.stock.get(NS.normal_key(n)) is not None
                and self.res.sheets.get(n) is not None]
        if not have:
            self.v_ns_status.set("nothing to stitch — those sheets have no stock colour/normal pair")
            return
        self.v_ns_status.set("stitching…")
        self.root.update_idletasks()
        try:
            out = NS.stitch_kit(self.stock, self.res.sheets, self._ns_params(),
                                heal=bool(self.v_ns_heal.get()), sheets=have)
        except Exception as e:
            self.v_ns_status.set(f"stitch failed: {e}")
            return
        for sheet, img in out.items():
            key = NS.normal_key(sheet)
            self.res.sheets[key] = img.convert("RGBA")
            self.touched.add(key)
            self._stitched.add(key)
        self.model_refresh()
        self.v_ns_status.set("stitched " + ", ".join(NS.normal_key(s) for s in out)
                             + " — Apply writes them alongside the colour sheets")

    def revert_normals(self):
        """Put the kit's own normals back, and stop Apply from writing them."""
        n = 0
        for sheet in NS.SHEET_ORDER:
            key = NS.normal_key(sheet)
            if self.stock.get(key) is not None:
                self.res.sheets[key] = self.stock[key].copy()
                self.touched.discard(key)
                self._stitched.discard(key)
                n += 1
        self.model_refresh()
        self.v_ns_status.set(f"restored {n} stock normal(s)")

    # ── size ─────────────────────────────────────────────────────────────────────────────
    def _scales(self):
        """The table's SIZE column, as the per-slot dict the engine takes.

        Size here is how big a mark is drawn INSIDE ITS CELL — the only size the format allows.
        The uniform .iff carries no placement table: where a crest or a number lands on the body
        comes from the mesh UVs and the shader's fixed source rects, so the art inside the cell is
        the whole of what is authorable.
        """
        moved = lambda s: abs(s - 1.0) > 1e-3
        out = {}
        stamps = {n: self.sizes[n] for n in self.slot_names if moved(self.sizes[n])}
        if stamps:
            out["stamps"] = stamps
        for key, _, sheet in EXTRA_SIZE_ROWS:
            if moved(self.sizes[key]):
                out[sheet] = {"*": self.sizes[key]}
        return out

    def _size_changed(self, row):
        """A SIZE cell was typed into — rebuild whichever sheet owns that row, debounced."""
        self._size_dirty.add(EXTRA_SIZE_SHEET.get(row, "stamps"))
        if self._size_job:
            self.root.after_cancel(self._size_job)
        self._size_job = self.root.after(180, self._do_size)

    def _do_size(self):
        self._size_job = None
        dirty, self._size_dirty = self._size_dirty, set()
        if "stamps" in dirty:
            self._do_rebuild_stamps()
        if "letters" in dirty:
            self._rebuild_letters()
        if "helmet" in dirty:
            self._rebuild_helmet(focus=False)

    def _art_group(self, parent, name, spec, title=None, blurb=None):
        """One art slot: a file picker plus the literal colour it is painted with."""
        gf = ttk.LabelFrame(parent, text=title or _pretty(name), padding=8)
        gf.pack(fill=X, pady=(0, 8))
        if blurb:
            ttk.Label(gf, foreground="#888", wraplength=360, justify=LEFT,
                      text=blurb).pack(fill=X, pady=(0, 6))
        choices = list(spec.get("choices") or [])
        cur = spec.get("file", "")
        if cur and cur not in choices:
            choices.insert(0, cur)
        choices = ["(keep the kit's own mark)"] + choices
        v = StringVar(value=choices[0])
        self.v_art_file[name] = v
        r = ttk.Frame(gf); r.pack(fill=X)
        cb = ttk.Combobox(r, textvariable=v, values=choices, state="readonly", width=28)
        self.cb_art[name] = cb
        cb.pack(side=LEFT, fill=X, expand=True)
        cb.bind("<<ComboboxSelected>>", lambda ev, n=name: self._art_changed(n))
        ttk.Button(r, text="…", width=3,
                   command=lambda n=name, c=cb: self._pick_art(n, c)).pack(side=LEFT, padx=4)

        r2 = ttk.Frame(gf); r2.pack(fill=X, pady=(6, 0))
        tv = StringVar(value=str(spec.get("tint") or ""))
        self.v_art_tint[name] = tv
        b = tk.Button(r2, width=10, relief="ridge", font=("Consolas", 8),
                      command=lambda n=name: self._pick_tint(n))
        b.pack(side=LEFT)
        self.b_art_tint[name] = b
        ttk.Button(r2, text="No colour", width=10,
                   command=lambda n=name: self._clear_tint(n)).pack(side=LEFT, padx=6)
        ttk.Label(r2, text="the mark renders exactly this", foreground="#888").pack(side=LEFT)
        self._paint_tint(name)

    def _paint_tint(self, name):
        b = self.b_art_tint[name]
        hx = self.v_art_tint[name].get()
        if not hx:
            b.config(text="as-is", bg="SystemButtonFace", fg="#000",
                     activebackground="SystemButtonFace")
            return
        from . import colorpick
        r, g, bl = colorpick.to_rgb(hx)
        b.config(text=hx.lstrip("#"), bg=hx, activebackground=hx,
                 fg="#000" if (r * 299 + g * 587 + bl * 114) / 1000 > 140 else "#FFF")

    def _pick_tint(self, name):
        from . import colorpick
        hx = colorpick.ask_color(self.root, self.v_art_tint[name].get() or DEFAULT_TINT,
                                 f"Colour for {_pretty(name)}", swatches=self._kit_swatches())
        if not hx:
            return
        self.v_art_tint[name].set(hx)
        self._paint_tint(name)
        self._art_changed(name)

    def _clear_tint(self, name):
        self.v_art_tint[name].set("")
        self._paint_tint(name)
        self._art_changed(name)

    def _build_preview(self, parent):
        self.nb_prev = ttk.Notebook(parent)
        self.nb_prev.pack(fill=BOTH, expand=True)
        self.views = {}
        for s in SHEETS:
            f = ttk.Frame(self.nb_prev)
            self.nb_prev.add(f, text=f"  {SHEET_LABEL[s]}  ")
            self.views[s] = SheetView(f,
                                      on_change=self._box_changed if s == "stamps" else None,
                                      on_select=self._box_selected if s == "stamps" else None)
        self._build_model_tab()
        self._build_helmet_tab()
        self.nb_prev.select(1)
        self.nb_prev.bind("<<NotebookTabChanged>>", lambda e: self._tab_render())
        self._push_boxes()

    def _tab_render(self):
        self._model_maybe_render()
        self._helmet_maybe_render()

    # ── model preview ────────────────────────────────────────────────────────────────────
    def _build_model_tab(self):
        """A live render of the kit on the game's own player mesh.

        The sheets are a garment atlas and a decal library; neither shows where anything ends up.
        This does, using the captured mesh and the measured decal placement (jersey_preview).
        It renders on demand — showing the tab, releasing a drag, pressing Refresh — because a
        frame costs about a third of a second and the sheet views are the live surface.
        """
        self.cv_model = None
        if not JP.available():
            return
        f = ttk.Frame(self.nb_prev)
        self.nb_prev.add(f, text="  Model  ")
        self._model_tab = f
        self._model_scene = None
        self._model_dirty = True
        self._model_yaw = 0.0
        self._model_pitch = 0.0
        self._model_zoom = 1.0
        self._model_drag = None
        self._model_photo = None

        bar = ttk.Frame(f, padding=(6, 4))
        bar.pack(fill=X)
        ttk.Label(bar, text="Letter").pack(side=LEFT)
        self.v_model_letter = StringVar(value="—")
        cb = ttk.Combobox(bar, textvariable=self.v_model_letter, state="readonly", width=4,
                          values=("—", "A", "C"))
        cb.pack(side=LEFT, padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self.model_refresh())
        # Name and number are what a jersey is judged by, and both come out of the kit's own
        # sheets (the letters sheet and the stamps digit cells), so previewing them is just
        # compositing -- no art needed from the user beyond the text.
        ttk.Label(bar, text="Name").pack(side=LEFT)
        self.v_model_name = StringVar(value="PLAYER")
        e = ttk.Entry(bar, textvariable=self.v_model_name, width=10)
        e.pack(side=LEFT, padx=(4, 8))
        e.bind("<Return>", lambda ev: self.model_refresh())
        e.bind("<FocusOut>", lambda ev: self.model_refresh())
        ttk.Label(bar, text="#").pack(side=LEFT)
        self.v_model_number = StringVar(value="88")
        e = ttk.Entry(bar, textvariable=self.v_model_number, width=4)
        e.pack(side=LEFT, padx=(4, 12))
        e.bind("<Return>", lambda ev: self.model_refresh())
        e.bind("<FocusOut>", lambda ev: self.model_refresh())
        # Front numbers have two placements, not two sizes: 'small' sits on the upper-left chest,
        # 'big' in the centre. The ROS uniform +0x18 enum picks which, per uniform.
        ttk.Label(bar, text="Front #").pack(side=LEFT)
        # Defaults to 'none' and is then taken from the kit's own ROS flag by _sync_front_number:
        # every shipped 2K10 uniform reads 'none', so 'small' put a number on the chest that the
        # game never draws.
        self.v_model_front = StringVar(value="none")
        cb = ttk.Combobox(bar, textvariable=self.v_model_front, state="readonly", width=6,
                          values=("small", "big", "none"))
        cb.pack(side=LEFT, padx=(4, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self.model_refresh())
        # Relief is the normal maps, lit. On by default because that is what the game draws; the
        # toggle is there so a flat read of the artwork is one click away.
        self.v_model_normals = BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Relief", variable=self.v_model_normals,
                        command=self.model_refresh).pack(side=LEFT, padx=(0, 12))
        for label, yaw in (("Front", 0.0), ("¾", 0.9), ("Side", math.pi / 2), ("Back", math.pi)):
            ttk.Button(bar, text=label, width=6,
                       command=lambda y=yaw: self._model_view(y)).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Refresh", width=8, command=self.model_refresh).pack(side=RIGHT)

        self.cv_model = tk.Canvas(f, bg="#101216", highlightthickness=0)
        self.cv_model.pack(fill=BOTH, expand=True)
        self.cv_model.bind("<ButtonPress-1>", self._model_press)
        self.cv_model.bind("<B1-Motion>", self._model_motion)
        self.cv_model.bind("<ButtonRelease-1>", lambda e: self._model_render(ss=2))
        self.cv_model.bind("<MouseWheel>", self._model_wheel)
        self.cv_model.bind("<Configure>", lambda e: self._model_render(ss=1))

        miss = ", ".join(n.replace("_", " ") for n in JP.unplaced())
        ttk.Label(f, foreground="#888", wraplength=560, justify=LEFT, padding=(6, 2, 6, 6),
                  text="Drag to turn, wheel to zoom. The crest, captaincy letter, front and back "
                       "numbers and both sleeve numbers are drawn with the game's own pixel-shader "
                       "math — an affine on the base UV for the crest, the mesh's baked decal "
                       "quads for the glyphs — so their position and size match the game exactly, "
                       "with nothing stretched to fit. Front numbers have two placements, not two "
                       "sizes: small sits high on the left chest, big in the centre; the uniform's "
                       f"own setting picks which. Still approximate or not drawn: {miss}.").pack(
        fill=X)

    def _model_view(self, yaw):
        self._model_yaw, self._model_pitch = yaw, 0.0
        self._model_render(ss=2)

    def _model_press(self, ev):
        self._model_drag = (ev.x, ev.y, self._model_yaw, self._model_pitch)

    def _model_motion(self, ev):
        if not self._model_drag:
            return
        x0, y0, yaw, pitch = self._model_drag
        self._model_yaw = yaw + (ev.x - x0) * 0.012
        self._model_pitch = max(-1.2, min(1.2, pitch + (ev.y - y0) * 0.008))
        self._model_render(ss=1)

    def _model_wheel(self, ev):
        self._model_zoom = max(0.5, min(6.0, self._model_zoom * (1.12 if ev.delta > 0 else 1 / 1.12)))
        self._model_render(ss=1)

    def model_refresh(self):
        """The sheets changed — drop the cached scene and redraw whichever tab is showing.

        The helmet skin rides the same refresh: it reads the same number field and the same
        palette, and its sheet is rebuilt by the same edits.
        """
        self._model_dirty = True
        self._helm_dirty = True
        self._model_maybe_render()
        self._helmet_maybe_render()

    def _ctl_tab_changed(self):
        """Showing Colours puts the model up in the other pane — they are one workflow.

        Defensive about its own widgets: a Notebook fires this while it is still being built.
        """
        tab = getattr(self, "_colour_tab", None)
        if tab is None:
            return
        try:
            if self.nb_ctl.nametowidget(self.nb_ctl.select()) is not tab:
                return
        except Exception:
            return
        if self._col_ros is None:
            self.colours_load()
        else:
            self._col_build_swatches()    # the sheets may have changed since it was built
        if getattr(self, "_model_tab", None) is not None:
            self.nb_prev.select(self._model_tab)

    def _model_maybe_render(self):
        if self.cv_model is None:
            return
        try:
            showing = self.nb_prev.nametowidget(self.nb_prev.select()) is self._model_tab
        except Exception:
            return
        if showing:
            self._model_render(ss=2)

    # ── helmet skin ──────────────────────────────────────────────────────────────────────
    def _build_helmet_tab(self):
        """The helmet as the game skins it: the shell colour, the team mark, the number.

        It gets its own tab rather than riding the Model render because the helmet is its own
        material with its own UV space, and a flat view of that space is the only way to see the
        whole skin at once -- the mark sits high on each side of the shell, so any single 3D angle
        hides half of it. The same skin IS painted onto the 3D helmet on the Model tab; this tab is
        the unwrapped view of it, not a substitute for it.
        """
        self.view_helm = None
        if not JP.available() or "helmet" not in SS.atlas_pieces():
            return
        f = ttk.Frame(self.nb_prev)
        self.nb_prev.add(f, text="  Helmet skin  ")
        self._helm_tab = f
        self._helm_dirty = True
        self.view_helm = SheetView(f)
        ttk.Label(f, foreground="#888", wraplength=560, justify=LEFT, padding=(6, 2, 6, 6),
                  text="The helmet draws from its OWN sheet — index 2 of the uniform .iff, 1024×256 "
                       "— not from the stamps sheet, which is why the number and the team mark "
                       "never showed up on the body. Shell colour comes from palette slot 47 and "
                       "the digits from slots 57–59, both on the Colours tab; the number is the one "
                       "on the Model tab. Everything here is placed by the game's measured decal "
                       "quads.").pack(fill=X)

    def _helmet_maybe_render(self):
        if getattr(self, "view_helm", None) is None:
            return
        try:
            showing = self.nb_prev.nametowidget(self.nb_prev.select()) is self._helm_tab
        except Exception:
            return
        if showing:
            self._helmet_render()

    def _helmet_render(self):
        if getattr(self, "view_helm", None) is None or not self._helm_dirty:
            return
        sheet = (self.res.sheets if self.res else {}).get("helmet")
        if sheet is None:
            return
        pal = self._target_palette()
        shell = (0.5, 0.5, 0.5)
        if pal:
            try:
                shell = tuple(JP._rgb(pal[UC.ELEMENT_SLOTS["helmet_shell"]]) / 255.0)
            except (IndexError, KeyError, ValueError):
                pass
        try:
            arr = JP.composite_helmet(sheet, number=self.v_model_number.get().strip(),
                                      shell=shell, palette=pal)
        except Exception as e:
            self.v_status.set(f"helmet preview: {e}")
            return
        if arr is None:
            return
        self.view_helm.set_image(Image.fromarray(arr, "RGB"))
        self._helm_dirty = False

    def _target_row(self):
        """(RosFile, row) for the uniform the target picker is pointing at, or None.

        The row is found by the same two keys the filename is built from: the asset key and the
        slot suffix, which is exactly what `uniform_<key>_<slot>.iff` encodes, so this works for
        expansion and relocated teams without a team-name table.
        """
        pair = self.targets.get(self.v_target.get())
        if not pair:
            return None
        m = re.match(r"^uniform_([a-z0-9]{2,4})_(home|away|alt)\.iff$", pair[1])
        if not m:
            return None
        path = self.app._current_roster_path()
        if not path or not Path(path).is_file():
            return None
        from . import ros_file as RF
        from . import uniform_colors as ucol
        ros = RF.RosFile(path)
        code, kit = m.group(1), m.group(2)
        for i in range(ucol.row_count(ros)):
            if (ucol.asset_key(ros, i).lower() == code
                    and ucol.slot_suffix(ucol.slot(ros, i)) == kit):
                return ros, i
        return None

    # ── colours ──────────────────────────────────────────────────────────────────────────
    def _build_colour_tab(self, nb):
        """The kit's 60 palette slots, as a control tab beside Stamp slots and Patch art.

        These are not in any texture: the lettering, the helmet shell and all sixteen glove zones
        live in the uniform's own Roster.ROS record, so no amount of editing the sheets touches
        them. It sits on the CONTROL side so the model stays on screen while you pick — a palette
        edit shows up nowhere else, and picking blind against a swatch grid is not editing.

        Edits go into the RosFile in memory and are written on Save, so Revert is just a reload.
        """
        f = ttk.Frame(nb)
        nb.add(f, text="  Colours  ")
        self._colour_tab = f

        bar = ttk.Frame(f, padding=(6, 4)); bar.pack(fill=X)
        self.v_col_head = StringVar(value="")
        ttk.Label(bar, textvariable=self.v_col_head).pack(side=LEFT)
        # Reload re-reads the roster from disk, so it doubles as Revert.
        ttk.Button(bar, text="Reload", width=8,
                   command=self.colours_load).pack(side=RIGHT, padx=(6, 0))
        self.b_col_save = ttk.Button(bar, text="Save", width=8, command=self.colours_save)
        self.b_col_save.pack(side=RIGHT)

        # SELECTION toolbar. Forty-four glove zones are usually two or three colours, and picking
        # each one on its own is the wrong shape of work: click builds a selection, and the colour
        # lands on all of it at once. A single slot is still one click plus one pick.
        sel = ttk.Frame(f, padding=(6, 0)); sel.pack(fill=X)
        self.v_col_sel = StringVar(value="nothing selected")
        ttk.Button(sel, text="Set colour…", width=12,
                   command=self.colours_apply_pick).pack(side=LEFT)
        ttk.Button(sel, text="Flash", width=7,
                   command=self.colours_flash).pack(side=LEFT, padx=(4, 0))
        ttk.Button(sel, text="None", width=6,
                   command=lambda: self._col_select(())).pack(side=LEFT, padx=(4, 0))
        ttk.Label(sel, textvariable=self.v_col_sel,
                  foreground="#888").pack(side=LEFT, padx=8)

        # The kit's own colours, one click each. Same list the picker offers.
        self._col_swatch_bar = ttk.Frame(f, padding=(6, 2))
        self._col_swatch_bar.pack(fill=X)

        cv = tk.Canvas(f, highlightthickness=0)
        sb = ttk.Scrollbar(f, orient=VERTICAL, command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        inner = ttk.Frame(cv)
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=inner, anchor="nw")
        cv.pack(side=LEFT, fill=BOTH, expand=True); sb.pack(side=RIGHT, fill=Y)
        cv.bind("<MouseWheel>", lambda e: cv.yview_scroll(int(-e.delta / 120), "units"))
        self._col_inner = inner

    def _col_paint(self, i):
        from . import colorpick
        from . import uniform_colors as ucol
        b = self._col_buttons.get(i)
        if b is None or self._col_ros is None:
            return
        c = ucol.get_colors(self._col_ros, self._col_row)[i]
        unset = ucol.is_unset(self._col_ros, self._col_row, i)
        bg = "#f0f0f0" if unset else c
        r, g, bl = colorpick.to_rgb(bg)
        on = i in self._col_sel
        b.config(bg=bg, activebackground=bg,
                 fg="#000" if (r * 299 + g * 587 + bl * 114) / 1000 > 140 else "#FFF",
                 # Selection has to read at a glance on a grid where every cell is a different
                 # colour, so it is the BORDER that changes, not the fill: a sunken 3px edge
                 # survives on white, on black and on the pink "unused" sentinel alike.
                 relief="sunken" if on else "ridge", borderwidth=3 if on else 1,
                 text=("• " if on else "") + f"{ucol.color_label(i)}\n"
                      f"{'unset' if unset else ('unused' if ucol.is_sentinel(c) else c[1:])}")

    def _col_click(self, i):
        """Click toggles selection; the colour is chosen once, for everything selected."""
        self._col_select(self._col_sel ^ {i})

    def _col_select(self, slots):
        was = set(self._col_sel)
        self._col_sel = set(slots)
        for i in was | self._col_sel:
            self._col_paint(i)
        n = len(self._col_sel)
        self.v_col_sel.set("nothing selected" if not n else
                           f"{n} field{'s' if n > 1 else ''} selected")

    def _col_group_select(self, key):
        from . import uniform_colors as ucol
        idx = {i for i in range(ucol.NSLOTS) if ucol.group_of(i) == key}
        # Clicking a group header that is already fully selected clears it -- one control, both
        # directions, which is what the button says ("Group") rather than two half-buttons.
        self._col_select(self._col_sel - idx if idx <= self._col_sel else self._col_sel | idx)

    def _col_pick(self, i):
        """Double-click: edit exactly this one field, whatever else is selected."""
        self._col_apply([i])

    def colours_apply_pick(self):
        if not self._col_sel:
            messagebox.showinfo("Colours", "Click the fields you want first — then Set colour.",
                                parent=self.root)
            return
        self._col_apply(sorted(self._col_sel))

    def _col_apply(self, slots):
        from . import colorpick
        from . import uniform_colors as ucol
        if self._col_ros is None or not slots:
            return
        cur = ucol.get_colors(self._col_ros, self._col_row)[slots[0]]
        title = (ucol.color_label(slots[0]) if len(slots) == 1
                 else f"{len(slots)} fields")
        hx = colorpick.ask_color(self.root, cur, title, swatches=self._kit_swatches())
        if not hx:
            return
        for i in slots:
            ucol.set_color(self._col_ros, self._col_row, i, hx)
            self._col_paint(i)
        self.model_refresh()          # the model is the only honest read on a palette edit

    # ── kit colours (quick-pick) ──────────────────────────────────────────────────────────
    def _kit_swatches(self):
        """The colours the current sheets are made of — cached per set of sheets."""
        sheets = (self.res.sheets if self.res else None) or self.stock
        key = id(sheets.get("base")), id(sheets.get("stamps"))
        if getattr(self, "_col_swatch_key", None) != key:
            try:
                self._col_swatch_cache = J.discover_colors(
                    [sheets.get("base"), sheets.get("stamps")], limit=12)
            except Exception:
                self._col_swatch_cache = []
            self._col_swatch_key = key
        return self._col_swatch_cache

    def _col_build_swatches(self):
        from . import colorpick
        bar = getattr(self, "_col_swatch_bar", None)
        if bar is None:
            return
        for w in bar.winfo_children():
            w.destroy()
        sw = self._kit_swatches()
        if not sw:
            return
        ttk.Label(bar, text="On this kit:", foreground="#888").pack(side=LEFT, padx=(0, 4))
        for hx, share in sw:
            r, g, b = colorpick.to_rgb(hx)
            lb = tk.Label(bar, text=share, bg=hx, width=5, relief="ridge", borderwidth=1,
                          font=("Segoe UI", 7),
                          fg="#000" if (r * 299 + g * 587 + b * 114) / 1000 > 140 else "#FFF")
            lb.pack(side=LEFT, padx=1)
            lb.bind("<Button-1>", lambda _e, h=hx: self._col_apply_hex(h))

    def _col_apply_hex(self, hx):
        from . import uniform_colors as ucol
        if self._col_ros is None or not self._col_sel:
            self.v_col_sel.set("select fields first, then click a kit colour")
            return
        for i in sorted(self._col_sel):
            ucol.set_color(self._col_ros, self._col_row, i, hx)
            self._col_paint(i)
        self.model_refresh()

    # ── flash ─────────────────────────────────────────────────────────────────────────────
    def colours_flash(self):
        """Blink the selected fields on the model so you can see WHERE they are.

        A palette slot's name ("love cuff tri") does not tell you what part of the glove it is.
        This drives the selection to magenta and back on the real model twice, which answers it
        in about a second and leaves the stored colours exactly as they were.
        """
        from . import uniform_colors as ucol
        if self._col_ros is None or not self._col_sel or self._col_flash:
            return
        slots = sorted(self._col_sel)
        cur = ucol.get_colors(self._col_ros, self._col_row)
        # Save the UNSET flag too, not just the hex: an accent left at ARGB 0 must go back to 0,
        # or a flash would quietly promote it to a real black.
        saved = [(cur[i], ucol.is_unset(self._col_ros, self._col_row, i)) for i in slots]
        self._col_flash = (slots, saved, 0)
        self._col_flash_step()

    def _col_flash_step(self):
        from . import uniform_colors as ucol
        if not self._col_flash:
            return
        slots, saved, step = self._col_flash
        on = step % 2 == 0
        for n, i in enumerate(slots):
            hx, unset = saved[n]
            if on:
                ucol.set_color(self._col_ros, self._col_row, i, "#FF00A8")
            elif unset:
                ucol.clear_color(self._col_ros, self._col_row, i)
            else:
                ucol.set_color(self._col_ros, self._col_row, i, hx)
        self.model_refresh()
        if step >= 3:                       # on, off, on, off -- then stop, colours restored
            self._col_flash = None
            for i in slots:
                self._col_paint(i)
            return
        self._col_flash = (slots, saved, step + 1)
        self.root.after(320, self._col_flash_step)

    def colours_load(self):
        """(Re)bind the tab to whatever the target picker points at."""
        from . import uniform_colors as ucol
        try:
            found = self._target_row()
        except Exception:
            found = None
        if not found:
            self._col_ros = self._col_row = None
            self.v_col_head.set("Pick a target jersey — colours live in its Roster.ROS record.")
            for w in self._col_inner.winfo_children():
                w.destroy()
            self._col_buttons.clear()
            self._col_select(())
            return
        ros, row = found
        self._col_ros, self._col_row = ros, row
        self.v_col_head.set(f"{self.v_target.get()}   ·   uniform row {row}")
        for w in self._col_inner.winfo_children():
            w.destroy()
        self._col_buttons.clear()
        self._col_select(())
        self._col_build_swatches()
        for key, heading, blurb in self.app._UNI_GROUPS:
            idx = [i for i in range(ucol.NSLOTS) if ucol.group_of(i) == key]
            lf = ttk.LabelFrame(self._col_inner, text=f"{heading}  ({len(idx)})", padding=6)
            lf.pack(fill=X, expand=True, pady=4)
            ttk.Label(lf, text=blurb, foreground="#999", font=("Segoe UI", 8),
                      wraplength=460, justify=LEFT).grid(row=0, column=0, columnspan=3,
                                                         sticky=W, pady=(0, 4))
            ttk.Button(lf, text="Group", width=7,
                       command=lambda k=key: self._col_group_select(k)
                       ).grid(row=0, column=3, sticky=E, pady=(0, 4))
            for n, i in enumerate(idx):
                # Four wide buttons, not eleven narrow ones: these carry a real element name over
                # two or three wrapped lines plus its hex, and a name clipped to "love cuff tri"
                # is worse than no name. height=4 is what the longest label needs without cutting
                # the hex off the bottom.
                b = tk.Button(lf, width=18, height=4, relief="ridge", font=("Consolas", 7),
                              wraplength=100, justify="center",
                              command=lambda i=i: self._col_click(i))
                # Double-click still edits the one field under the cursor, so the old one-slot
                # workflow costs the same two clicks it always did.
                b.bind("<Double-Button-1>", lambda _e, i=i: self._col_pick(i))
                b.grid(row=1 + n // 4, column=n % 4, padx=1, pady=1)
                self._col_buttons[i] = b
                self._col_paint(i)

    def colours_save(self):
        if self._col_ros is None:
            messagebox.showinfo("Colours", "Nothing loaded.", parent=self.root); return
        if self._col_flash:               # mid-blink the row holds the marker colour, not yours
            messagebox.showinfo("Colours", "Flashing — try again in a second.",
                                parent=self.root); return
        try:
            self._col_ros.save()
        except Exception as e:
            messagebox.showerror("Colours", f"Write failed:\n{e}", parent=self.root); return
        self.app._log(f"[jersey] colours saved to uniform row {self._col_row}")
        self.model_refresh()
        self.app._prompt_restart_if_running()

    def _target_palette(self):
        """The selected kit's own 60 palette slots, or None.

        Name, number and sleeve-number colours and the gloves and helmet are NOT in the textures —
        they come out of the uniform record in Roster.ROS, so a preview built from the sheets alone
        gets them wrong. The row is found by the same two keys the filenames are built from: the
        asset key and the slot suffix, which is exactly what `uniform_<key>_<slot>.iff` encodes, so
        this works for expansion and relocated teams without a team-name table.

        Any failure here is silent and returns None: the palette is a refinement of the preview,
        and no roster loaded is a normal state for this tab.
        """
        try:
            from . import uniform_colors as ucol
            # Unsaved edits on the Colours tab count: the model is what you judge them by.
            if getattr(self, "_col_ros", None) is not None:
                return ucol.get_colors(self._col_ros, self._col_row)
            found = self._target_row()
            if not found:
                return None
            ros, row = found
            return ucol.get_colors(ros, row)
        except Exception:
            return None

    def _sync_front_number(self):
        """Take the chest-number placement from the TARGET's own uniform record.

        The game reads it out of the ROS uniform flags (+0x18) and encodes "not worn" by pushing
        the digit's sheet origin outside its own mask window -- captured in draw 1147, where the
        two digit slots carry S.z 1.09912/1.03687 against an M window that tops out at 0.74365, so
        nothing is ever drawn. Every shipped kit checked reads `none`, which is why a preview that
        defaulted to 'small' showed a chest number the game does not draw.
        """
        try:
            from . import uniform_colors as ucol
            found = self._target_row()
            if found:
                self.v_model_front.set(ucol.front_number(*found))
        except Exception:
            pass

    def _model_render(self, ss=2):
        if self.cv_model is None:
            return
        try:
            if self._model_dirty or self._model_scene is None:
                sheets = {k: v for k, v in (self.res.sheets if self.res else {}).items()
                          if k in ("base", "stamps", "letters", "helmet",
                                   "base_normal", "normal", "letters_normal")}
                if not sheets.get("base"):
                    return
                letter = self.v_model_letter.get()
                # The digit advance and ink rects are per-KIT (uniform DRAM 0x540/0x550), so the
                # preview has to read the TARGET's own table or the numbers come out the wrong
                # size for every kit but the one the shader constants were captured from.
                pair = self.targets.get(self.v_target.get())
                metrics = SS.kit_metrics(pair[1]) if pair else None
                self._model_scene = JP.build_scene(
                    sheets, letter=None if letter == "—" else letter,
                    name=self.v_model_name.get().strip(),
                    number=self.v_model_number.get().strip(),
                    front_style=self.v_model_front.get(),
                    palette=self._target_palette(), metrics=metrics,
                    normals=bool(self.v_model_normals.get()))
                self._model_dirty = False
            W = max(120, self.cv_model.winfo_width())
            H = max(160, self.cv_model.winfo_height())
            im = JP.render(self._model_scene, W=W, H=H, yaw=self._model_yaw,
                           pitch=self._model_pitch, zoom=self._model_zoom, ss=ss)
        except Exception as e:
            self.v_status.set(f"model preview: {e}")
            return
        if im is None:
            return
        self._model_photo = ImageTk.PhotoImage(im)
        self.cv_model.delete("all")
        self.cv_model.create_image(0, 0, anchor="nw", image=self._model_photo)

    # ── sources / target ─────────────────────────────────────────────────────────────────
    def pick_folder(self):
        d = filedialog.askdirectory(title="Folder holding the NHL 23 jersey textures")
        if not d:
            return
        self.v_folder.set(d)
        found = J.find_sources(d)
        # `helmet_logo` is a decal, not a garment sheet: an NHL 23 kit folder ships
        # helmetlogo_adidas_<team>_<kit>_0_color next to the garments, and it belongs in the
        # helmet-logo art slot rather than in the conversion's source set.
        helm = found.pop("helmet_logo", None)
        self.sources = dict(found)
        for k, _ in J.SOURCE_KINDS:
            self.v_src[k].set(found[k].name if k in found else "—")
        if helm is not None:
            cb = self.cb_art.get("helmet_logo")
            if cb is not None:
                cb["values"] = list(cb["values"]) + [str(helm)]
            self.v_art_file["helmet_logo"].set(str(helm))
            self._art_changed("helmet_logo")
        miss = [KIND_LABEL[k] for k, _ in J.SOURCE_KINDS if k not in found]
        self.v_status.set(f"{len(found)} source(s) found"
                          + (f" + helmet logo {helm.name}" if helm is not None else "")
                          + (f" — missing {', '.join(miss)}" if miss else ""))

    def pick_source(self, kind):
        p = filedialog.askopenfilename(title=f"NHL 23 {KIND_LABEL[kind]}",
                                       filetypes=[("Textures", "*.dds *.png *.tga"),
                                                  ("All files", "*.*")])
        if not p:
            return
        self.sources[kind] = Path(p)
        self.v_src[kind].set(Path(p).name)

    def refresh_targets(self):
        """Every kit on this install, paired base ↔ uniform. Built from the catalog rather than
        a fixed team list so expansion/relocated teams show up too."""
        self.targets = {}
        self.team_kits = {}
        try:
            names = {r["iff"] for r in archtex.load_catalog() if r.get("iff")}
        except Exception as e:
            self.v_status.set(f"catalog unavailable: {e}")
            return
        pat = re.compile(r"^uniform_base_([a-z0-9]{2,4})_(home|away|alt)\.iff$")
        for n in sorted(names):
            m = pat.match(n)
            if not m:
                continue
            code, kit = m.group(1), m.group(2)
            uni = f"uniform_{code}_{kit}.iff"
            if uni not in names:
                continue
            self.targets[f"{code.upper()} — {kit.title()}"] = (n, uni)
            self.team_kits.setdefault(code.upper(), []).append(kit.title())
        for code, kits in self.team_kits.items():
            kits.sort(key=lambda k: (KIT_ORDER.index(k) if k in KIT_ORDER else 9, k))
        teams = sorted(self.team_kits)
        self.cb_team["values"] = teams
        if teams and self.v_team.get() not in self.team_kits:
            self.v_team.set(teams[0])
        self._team_picked()

    def _team_picked(self):
        """Team changed: re-stock the variation list, keeping the same variation if it exists."""
        kits = self.team_kits.get(self.v_team.get(), [])
        self.cb_kit["values"] = kits
        if kits and self.v_kit.get() not in kits:
            self.v_kit.set(kits[0])
        self._kit_picked()

    def _kit_picked(self):
        team, kit = self.v_team.get(), self.v_kit.get()
        label = f"{team} — {kit}"
        if label not in self.targets:
            return
        self.v_target.set(label)
        self._col_ros = self._col_row = None       # a different kit means a different palette row
        self._sync_front_number()
        self.load_stock()
        self.colours_load()

    def load_stock(self):
        """Load the target kit's four sheets and SHOW them — this is the editor's default state.

        Both assets are read: the stamps/helmet/letters sheets live in `uniform_<key>_<slot>.iff`
        and the body texture in `uniform_base_<key>_<slot>.iff`, and until Convert replaces them
        these images are the working set. `edit_mode` records that, so a slot change composites
        onto the kit's own sheet instead of onto a blank one.

        Read LIVE, not pristine. Everywhere else in the launcher a preview means "the shipped
        design", so archive_textures defaults to the `.orig` backups — but an editor that reopens
        a kit you already applied and silently hands you the factory art is a data-loss trap.
        `live=True` reads the current archives, so loading TOR Home after editing TOR Home gives
        back the edit.
        """
        self.stock = {}
        pair = self.targets.get(self.v_target.get())
        if not pair:
            return
        base_iff, uni_iff = pair
        try:
            recs = archtex.list_textures(uni_iff, live=True)
            for idx, key in J.UNIFORM_TEX.items():
                if key in ("stamps", "helmet", "letters", "normal", "letters_normal") \
                        and idx < len(recs):
                    img = archtex.decode_record(uni_iff, recs[idx], live=True)
                    if img is not None:
                        self.stock[key] = img.convert("RGBA")
        except Exception as e:
            self.v_status.set(f"could not read {uni_iff}: {e}")
            return
        try:
            brecs = archtex.list_textures(base_iff, live=True)
            # Record 2 is the body's own normal map. It is loaded with the colour because the
            # stitcher needs BOTH — it reads the stock pair to learn this kit's thread style, then
            # re-cuts the seams for the new colour.
            for idx, key in ((0, "base"), (2, "base_normal")):
                if idx < len(brecs):
                    img = archtex.decode_record(base_iff, brecs[idx], live=True)
                    if img is not None:
                        self.stock[key] = img.convert("RGBA")
        except Exception as e:
            self.app._log_q.put(f"[jersey] {base_iff}: {e}")

        self._stitched = set()
        self.edit_mode = True
        self.touched = set()
        self.res = J.Result(sheets={k: v.copy() for k, v in self.stock.items()})
        for sheet in SHEETS:
            self.views[sheet].set_image(self.res.sheets.get(sheet))
        self.model_refresh()
        self.txt_warn.delete("1.0", END)
        self.v_status.set(f"{self.v_target.get()}: loaded {len(self.stock)} sheet(s) from the game "
                          f"— editing the current kit")

    # ── slot table ───────────────────────────────────────────────────────────────────────
    def _row_values(self, name):
        """One table row. The two EXTRA_SIZE_ROWS have a size but no rect, so their box columns
        read '—' rather than a zero that looks editable."""
        pct = f"{round(self.sizes[name] * 100)}%"
        if name in EXTRA_SIZE_SHEET:
            return ("", "—", "—", "—", "—", pct)
        return ("✔" if self.enabled[name] else "", *self.rects[name], pct)

    def _fill_tree(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for n in self.slot_names:
            self.tree.insert("", END, iid=n, text=_pretty(n), values=self._row_values(n))
        for key, label, _sheet in EXTRA_SIZE_ROWS:
            self.tree.insert("", END, iid=key, text=label, values=self._row_values(key))
        if sel and sel[0] in self.sizes:
            self.tree.selection_set(sel[0])

    def _update_row(self, name):
        self.tree.item(name, values=self._row_values(name))

    def _sel(self):
        """The selected STAMP slot, or None — the extra size rows aren't slots and have no box."""
        s = self.tree.selection()
        return s[0] if s and s[0] in self.rects else None

    def _slot_selected(self):
        self._push_boxes()

    def _toggle_slot(self, _e=None):
        n = self._sel()
        if not n:
            return "break"
        self.enabled[n] = not self.enabled[n]
        self._update_row(n)
        self._push_boxes()
        self._rebuild_stamps()
        return "break"

    # ── click-to-edit ────────────────────────────────────────────────────────────────────
    def _tree_click(self, ev):
        """Edit the number you can see, where you can see it.

        A click in ON toggles the slot; a click in any editable column floats an Entry exactly over
        that cell. There used to be a row of spinboxes under the table instead, which meant reading
        a value in one place and changing it in another.
        """
        self._end_edit(commit=True)
        if self.tree.identify_region(ev.x, ev.y) != "cell":
            return
        row = self.tree.identify_row(ev.y)
        col = self.tree.identify_column(ev.x)          # '#1' == first value column
        try:
            name = SLOT_COLS[int(col[1:]) - 1]
        except (ValueError, IndexError):
            return
        if not row:
            return
        self.tree.selection_set(row)
        if name == "on":
            if row in self.rects:
                self._toggle_slot()
            return "break"
        if name not in EDIT_COLS or (name != "size" and row not in self.rects):
            return
        box = self.tree.bbox(row, col)
        if not box:
            return
        x, y, w, h = box
        cur = self.sizes[row] * 100 if name == "size" else self.rects[row]["xywh".index(name)]
        var = StringVar(value=f"{round(cur)}")
        e = ttk.Entry(self.tree, textvariable=var, justify="right")
        e.place(x=x, y=y, width=w, height=h)
        e.focus_set()
        e.selection_range(0, END)
        self._cell_edit = (e, row, name, var)
        e.bind("<Return>", lambda _e: self._end_edit(commit=True))
        e.bind("<KP_Enter>", lambda _e: self._end_edit(commit=True))
        e.bind("<Escape>", lambda _e: self._end_edit(commit=False))
        e.bind("<FocusOut>", lambda _e: self._end_edit(commit=True))
        return "break"

    def _end_edit(self, commit):
        if not self._cell_edit:
            return "break"
        e, row, col, var = self._cell_edit
        self._cell_edit = None
        raw = var.get()
        e.destroy()
        if not commit:
            return "break"
        try:
            val = float(raw.strip().rstrip("%"))
        except ValueError:
            return "break"
        if col == "size":
            new = max(0.1, min(4.0, val / 100.0))
            if abs(new - self.sizes[row]) < 1e-4:
                return "break"
            self.sizes[row] = new
            self._update_row(row)
            self._size_changed(row)
        else:
            self.rects[row]["xywh".index(col)] = max(0, int(round(val)))
            self._update_row(row)
            self._push_boxes()
            self._rebuild_stamps()
        return "break"

    def _box_selected(self, name):
        self.tree.selection_set(name)
        self.tree.see(name)

    def _box_changed(self, name, rect):
        self.rects[name] = [max(0, int(v)) for v in rect]
        self._update_row(name)
        self._rebuild_stamps()

    def reset_slots(self, everything):
        slots = self.profile["stamps"]["slots"]
        names = (self.slot_names + [k for k, _, _ in EXTRA_SIZE_ROWS]) if everything \
            else [self.tree.selection()[0]] if self.tree.selection() else []
        for n in names:
            if n in self.rects:
                self.rects[n] = list(slots[n]["rect"])
            self.sizes[n] = 1.0
            self._size_dirty.add(EXTRA_SIZE_SHEET.get(n, "stamps"))
        self._fill_tree()
        self._push_boxes()
        self._do_size()

    def set_all(self, on):
        for n in self.slot_names:
            self.enabled[n] = on
        self._fill_tree()
        self._push_boxes()
        self._rebuild_stamps()

    def _push_boxes(self):
        self.views["stamps"].set_boxes(self.rects, self.enabled, self._sel())

    def _art_changed(self, name):
        f = self.v_art_file[name].get()
        spec = {"file": "" if f.startswith("(") else f,
                "tint": self.v_art_tint[name].get() or None}
        if name == "helmet_logo":
            self.helmet_logo = spec
            self._rebuild_helmet()
            return
        self.art[name] = spec
        self._rebuild_stamps()

    def _pick_art(self, name, combo):
        p = filedialog.askopenfilename(title=f"Art for {_pretty(name)}",
                                       filetypes=[("Images", "*.png *.dds *.tga"),
                                                  ("All files", "*.*")])
        if not p:
            return
        combo["values"] = list(combo["values"]) + [p]
        self.v_art_file[name].set(p)
        self._art_changed(name)

    # ── conversion ───────────────────────────────────────────────────────────────────────
    def _opts(self):
        # A slot with neither art nor a colour falls back to the kit's own mark, which is what
        # build_stamps does with no entry at all — so drop it. A tint on its own is kept: that
        # means "repaint the mark already there", not "leave it alone".
        art = {k: v for k, v in self.art.items() if v.get("file") or v.get("tint")}
        return J.Options(layers=None, strip_logos=bool(self.v_strip.get()),
                         helmet_from_jersey=bool(self.v_helm_jersey.get()),
                         helmet_keep_logo=bool(self.v_helm_logo.get()),
                         enabled=dict(self.enabled), rects=dict(self.rects), art=art,
                         helmet_logo=dict(self.helmet_logo)
                         if (self.helmet_logo.get("file")
                             or self.helmet_logo.get("tint")) else {},
                         scales=self._scales())

    def convert(self):
        if self.busy:
            return
        if not self.sources:
            messagebox.showwarning("Jersey Conversion", "Pick the NHL 23 source textures first.")
            return
        self.busy = True
        self.v_status.set("converting…")
        opts, stock = self._opts(), dict(self.stock)
        paths = dict(self.sources)

        def work():
            try:
                srcs = {k: J.load_image(p) for k, p in paths.items()}
                res = J.convert(srcs, stock, self.profile, opts, self.regions)
                self.root.after(0, lambda: self._converted(res, None))
            except Exception as e:
                tb = traceback.format_exc()
                self.root.after(0, lambda: self._converted(None, (e, tb)))

        threading.Thread(target=work, daemon=True).start()

    def _converted(self, res, err):
        self.busy = False
        if err:
            e, tb = err
            self.v_status.set(f"conversion failed: {e}")
            self.txt_warn.delete("1.0", END)
            self.txt_warn.insert(END, tb)
            self.app._log_q.put(f"[jersey] convert failed: {e}")
            return
        self.res = res
        self.edit_mode = False                         # the previews are the conversion's now
        self.touched = set(res.sheets)
        # The conversion builds colour only. Carry the kit's own normals across so the model keeps
        # its relief and the Normals page has something to compare against — untouched, so Apply
        # leaves them alone until they are actually stitched.
        for sheet in NS.SHEET_ORDER:
            key = NS.normal_key(sheet)
            if key not in res.sheets and self.stock.get(key) is not None:
                res.sheets[key] = self.stock[key].copy()
                self.touched.discard(key)
        self._stitched = set()
        for s in SHEETS:
            self.views[s].set_image(res.sheets.get(s))
        self.model_refresh()
        self.txt_warn.delete("1.0", END)
        if res.warnings:
            self.txt_warn.insert(END, "\n".join(res.warnings))
        lifted = ", ".join(lg.name for lg in res.logos) or "none"
        lay = "  ".join(f"{k} {v}" for k, v in sorted(res.layers.items()))
        self.v_status.set(f"{len(res.sheets)} sheet(s) built — logos lifted: {lifted}"
                          + (f" — layers: {lay}" if lay else ""))

    def _rebuild_stamps(self, delay=220):
        """Re-run build_stamps only. Debounced, because a drag fires this on every commit."""
        if self.res is None or not self.res.sheets.get("stamps"):
            return
        if self._stamps_job:
            self.root.after_cancel(self._stamps_job)
        self._stamps_job = self.root.after(delay, self._do_rebuild_stamps)

    def _do_rebuild_stamps(self):
        self._stamps_job = None
        o = self._opts()
        kc = self.profile["key_colors"]
        import numpy as np
        colors = [np.array(kc[k], np.float32) for k in ("fill", "edge1", "edge2")]
        try:
            self.res.sheets["stamps"] = J.build_stamps(
                self.res.glyphs, self.res.logos, self.profile, self.stock.get("stamps"),
                colors, self.res.layers.get("stamps", o.layers or 2), o.enabled, o.rects, o.art,
                canvas=self.stock.get("stamps") if self.edit_mode else None,
                scales=o.scales.get("stamps"))
            self.views["stamps"].set_image(self.res.sheets["stamps"])
            self.model_refresh()
            self.touched.add("stamps")
        except Exception as e:
            self.v_status.set(f"stamps rebuild failed: {e}")

    def _rebuild_letters(self):
        """The nameplate alphabet. Only the size sliders drive this."""
        if self.res is None:
            return
        canvas = self.stock.get("letters") if self.edit_mode else None
        if canvas is None and not self.res.glyphs:
            return
        o = self._opts()
        kc = self.profile["key_colors"]
        import numpy as np
        colors = [np.array(kc[k], np.float32) for k in ("fill", "edge1", "edge2")]
        try:
            self.res.sheets["letters"] = J.build_letters(
                self.res.glyphs, self.profile, self.stock.get("letters"), colors,
                self.res.layers.get("letters", o.layers or 2), o.scales.get("letters"),
                canvas=canvas)
            self.views["letters"].set_image(self.res.sheets["letters"])
            self.model_refresh()
            self.touched.add("letters")
        except Exception as e:
            self.v_status.set(f"letters rebuild failed: {e}")

    def _rebuild_helmet(self, focus=True):
        """The helmet decal and the helmet-number size drive this; cheap enough to run at once."""
        if self.res is None:
            return
        o = self._opts()
        kc = self.profile["key_colors"]
        import numpy as np
        colors = [np.array(kc[k], np.float32) for k in ("fill", "edge1", "edge2")]
        try:
            self.res.sheets["helmet"] = J.build_helmet(
                self.res.glyphs, self.res.logos, self.profile, self.stock.get("helmet"),
                colors, self.res.layers.get("helmet", o.layers or 2), o.helmet_keep_logo,
                o.helmet_from_jersey, o.helmet_logo,
                canvas=self.stock.get("helmet") if self.edit_mode else None,
                scales=o.scales.get("helmet"))
            self.views["helmet"].set_image(self.res.sheets["helmet"])
            self.model_refresh()
            self.touched.add("helmet")
            if focus:
                self.nb_prev.select(3)
        except Exception as e:
            self.v_status.set(f"helmet rebuild failed: {e}")

    # ── output ───────────────────────────────────────────────────────────────────────────
    def save_pngs(self):
        if not (self.res and self.res.sheets):
            messagebox.showwarning("Jersey Conversion", "Convert something first.")
            return
        d = filedialog.askdirectory(title="Write the converted sheets here")
        if not d:
            return
        for name, img in self.res.sheets.items():
            img.save(Path(d) / f"{name}.png")
        self.v_status.set(f"wrote {len(self.res.sheets)} PNG(s) to {d}")

    def apply(self):
        if not (self.res and self.res.sheets):
            messagebox.showwarning("Jersey Conversion", "Convert something first.")
            return
        pair = self.targets.get(self.v_target.get())
        if not pair:
            messagebox.showwarning("Jersey Conversion", "Pick the 2K10 kit to replace.")
            return
        root = self.app._get_game_root()
        if not root:
            messagebox.showerror("Jersey Conversion", "Set the game-files folder in Settings.")
            return
        if self.app._op_busy():
            return
        # Editing a kit, write back only what was touched. Re-encoding an untouched sheet would
        # cost quality for nothing -- these came out of the archive already compressed.
        names = sorted(self.touched & set(self.res.sheets)) if self.edit_mode \
            else sorted(self.res.sheets)
        # A normal is only written if it was actually stitched. After a conversion the working set
        # carries the kit's own normals so the model keeps its relief; re-encoding those would be a
        # DXN round-trip for no change.
        normals = {NS.normal_key(s) for s in NS.SHEET_ORDER}
        names = [n for n in names if n not in normals or n in self._stitched]
        if not names:
            messagebox.showinfo("Jersey Editor", "Nothing has been changed yet.")
            return
        if not messagebox.askyesno(
                "Apply jersey", f"Write {', '.join(names)} into {pair[0]} and {pair[1]}?\n\n"
                                "The stamps sheet is also mirrored to this jersey's front-end "
                                "team-select art."):
            return
        sheets = {k: self.res.sheets[k].copy() for k in names}
        self.app._run_in_thread(self._apply_worker, pair, sheets, root,
                                op_label="Applying converted jersey…")

    def _apply_worker(self, pair, sheets, root):
        log = self.app._log_q.put
        base_iff, uni_iff = pair
        tmp = Path(tempfile.mkdtemp(prefix="jerseyconv_"))
        try:
            paths = {}
            for name, img in sheets.items():
                p = tmp / f"{name}.png"
                img.save(p)
                paths[name] = p

            # Normals ride along with their colour sheet. `_edits_for` drops any the caller didn't
            # hand over, so an unstitched kit writes exactly what it always did. They stay DXN:
            # replace_many is already called with prefer_lossless=False, which keeps the record's
            # own format and its size, so a normal is an in-place swap with no grow.
            plan = [(base_iff, [(0, "base"), (2, "base_normal")]),
                    (uni_iff, [(0, "stamps"), (2, "helmet"), (3, "letters"),
                               (1, "normal"), (4, "letters_normal")])]
            for iff, wants in plan:
                edits = self._edits_for(iff, wants, paths, log)
                if not edits:
                    continue
                log(f"[jersey] {iff}: {len(edits)} texture(s)")
                archtex.ensure_clean(iff, root, log)
                log("  " + str(archtex.replace_many(iff, edits, root, log,
                                                    prefer_lossless=False)))

            # The front-end team-select sheet holds a byte-identical copy of `stamps`; without
            # this the jersey changes in-game but not on the team-select screen.
            if "stamps" in paths:
                for twin in jerseys.twins(uni_iff):
                    edits = self._edits_for(twin, [(0, "stamps")], paths, log)
                    if not edits:
                        continue
                    log(f"[jersey] mirror → {twin}")
                    archtex.ensure_clean(twin, root, log)
                    log("  " + str(archtex.replace_many(twin, edits, root, log,
                                                        prefer_lossless=False)))
            log("[jersey] done.")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _edits_for(iff, wants, paths, log):
        """[(texture index, sheet name)] -> replace_many edits, skipping anything this asset
        doesn't actually have."""
        try:
            recs = archtex.list_textures(iff)
        except Exception as e:
            log(f"[jersey] {iff}: unreadable ({e})")
            return []
        out = []
        for idx, key in wants:
            if key not in paths:
                continue
            if idx >= len(recs):
                log(f"[jersey] {iff}: no texture #{idx} — skipped {key}")
                continue
            out.append({**recs[idx], "path": str(paths[key])})
        return out


def _pretty(name: str) -> str:
    return name.replace("_", " ")
