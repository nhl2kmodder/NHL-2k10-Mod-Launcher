"""
NHL 2k10 — Jersey Normal Stitcher  (standalone tool, launchable from the Mod Launcher)

Problem it solves
-----------------
When you author a NEW base-jersey colour (uniform_base_*.iff "base" texture) with your own
striping, the matching DXN "base_normal" still carries the *stock* stitching relief — the fine
sewn-on look (edge ridges + dashed stitches + twill infill) that traces the OLD stripes. This
tool regenerates that relief so it follows YOUR new striping, reusing the garment's wrinkles and
construction seams (which never change — same mesh/UV) as an untouched base.

How it works (all in the decoded-normal domain: R=X, G=Y, B=Z, flat = 128,128,255)
  1. CLEAN BASE  — heal the stock stripe-stitching out of the stock normal (found from the stock
                   colour's stripe edges), leaving only wrinkles + seams.
  2. DETECT      — find YOUR stripe edges/bands from your new colour (colour delta vs base fabric).
  3. SYNTHESISE  — stamp edge ridges + periodic dashed stitches + subtle twill as a height field,
                   convert to a detail normal.  Orientation-aware, so it follows any stripe angle.
  4. COMPOSITE   — partial-derivative (whiteout) blend of clean-base ⊕ stitch-detail.
  5. SAMPLE      — measure ridge width / stitch spacing / twill period off the stock normal to seed
                   sensible slider DEFAULTS; every value stays overridable by a slider.

Delivery: its own window (Toplevel under the launcher, or a standalone Tk root).  Game I/O reuses
the launcher's proven archive_textures codec (DXN in-place, no grow — uniform_base is load-assigned).
"""
from __future__ import annotations
import os, sys, tempfile, threading, traceback
import numpy as np
from PIL import Image, ImageTk
from scipy.ndimage import gaussian_filter, distance_transform_edt, sobel

# ── optional launcher integration (game extract/apply) ───────────────────────
try:
    from launcher import archive_textures as archtex
except Exception:
    try:
        import archive_textures as archtex           # running from inside launcher/
    except Exception:
        archtex = None

# The 30 team codes + kits, matching uniform_base_<team>_<kit>.iff naming.
TEAMS = ["ana","atl","bos","buf","cgy","car","chi","col","cbj","dal","det","edm","fla","lak",
         "min","mtl","nsh","njd","nyi","nyr","ott","phi","phx","pit","sjs","stl","tbl","tor",
         "van","wsh"]
KITS = ["home", "away"]

# ─────────────────────────────────────────────────────────────────────────────
#  CORE  (numpy)  — importable & unit-testable without any GUI
# ─────────────────────────────────────────────────────────────────────────────
DEFAULTS = dict(
    color_thr   = 40.0,   # colour distance (0-255) that counts as "not base fabric"
    edge_k      = 2.0,    # colour-change (× color_thr) that counts as a stripe edge (lower = more)
    ridge_w     = 1.6,    # px — half-width of the raised seam ridge at a stripe edge
    ridge_h     = 0.55,   # relief height of the edge ridge
    stitch_off  = 2.0,    # px — inset of the stitch row from the stripe edge
    stitch_w    = 1.4,    # px — width of the stitch row band
    stitch_spc  = 6.0,    # px — period between stitches along the edge
    dash_len    = 3.0,    # px — length of each stitch dash (must be < stitch_spc)
    stitch_h    = 0.40,   # relief height of the dashes
    twill_ang   = 45.0,   # deg — direction of the in-band weave
    twill_prd   = 3.0,    # px — weave period
    twill_h     = 0.06,   # relief height of the weave (subtle!)
    strength    = 1.6,    # overall height->normal gain
    heal_blur   = 4.0,    # px — blur radius used to heal out the stock stitching
)

def rgb_to_n(img: np.ndarray) -> np.ndarray:
    n = img.astype(np.float32) / 127.5 - 1.0
    nx, ny = n[..., 0], n[..., 1]
    nz = np.sqrt(np.clip(1 - nx * nx - ny * ny, 1e-6, 1))
    return np.stack([nx, ny, nz], -1)

def n_to_rgb(n: np.ndarray) -> np.ndarray:
    n = n / np.linalg.norm(n, axis=-1, keepdims=True).clip(1e-6)
    rgb = (n + 1.0) * 127.5
    return np.clip(rgb, 0, 255).astype(np.uint8)

def _height_to_n(h: np.ndarray, strength: float) -> np.ndarray:
    gy = sobel(h, axis=0, mode="nearest"); gx = sobel(h, axis=1, mode="nearest")
    n = np.stack([-gx * strength, -gy * strength, np.ones_like(h)], -1)
    return n / np.linalg.norm(n, axis=-1, keepdims=True).clip(1e-6)

def _blend(base: np.ndarray, det: np.ndarray) -> np.ndarray:
    """Partial-derivative (whiteout) detail-normal blend of two unit-normal fields."""
    bxy = base[..., :2] / base[..., 2:3].clip(1e-3)
    dxy = det[..., :2] / det[..., 2:3].clip(1e-3)
    g = bxy + dxy
    n = np.concatenate([g, np.ones(g.shape[:2] + (1,), np.float32)], -1)
    return n / np.linalg.norm(n, axis=-1, keepdims=True).clip(1e-6)

def base_fabric_rgb(color: np.ndarray) -> np.ndarray:
    """Dominant fabric colour = median (robust to stripes/logos)."""
    return np.median(color.reshape(-1, 3), axis=0)

def stripe_fields(color: np.ndarray, base_rgb: np.ndarray, thr: float, edge_k: float = 2.0):
    """From a base-colour texture -> (edge_mask, region_mask, tangent_x, tangent_y).

    Edges are taken from COLOUR transitions anywhere in the texture (per-channel gradient), so
    stripe↔stripe boundaries (e.g. black↔gold↔white bands stacked together) each get relief — not
    only where a stripe meets the base fabric.  `region` (far from the base fabric colour) is still
    used to gate the in-band twill.  `edge_k` scales the colour-change needed to count as an edge
    (lower = more sensitive)."""
    c = color.astype(np.float32)
    dist = np.linalg.norm(c - base_rgb, axis=-1)
    region = dist > thr
    # per-channel colour gradient -> magnitude captures ANY stripe boundary, incl. stripe↔stripe
    gX = np.stack([sobel(c[..., i], axis=1, mode="nearest") for i in range(3)], -1)
    gY = np.stack([sobel(c[..., i], axis=0, mode="nearest") for i in range(3)], -1)
    mag_per = np.hypot(gX, gY)                     # H×W×3
    edge = mag_per.sum(-1) > max(edge_k * thr, 1.0)
    # tangent from the strongest-changing channel (⟂ to that gradient) = the stripe's run direction
    ch = np.argmax(mag_per, axis=-1)[..., None]
    gx = np.take_along_axis(gX, ch, -1)[..., 0]
    gy = np.take_along_axis(gY, ch, -1)[..., 0]
    mag = np.hypot(gx, gy)
    with np.errstate(invalid="ignore", divide="ignore"):
        tx = np.where(mag > 1e-6, -gy / (mag + 1e-6), 0.0)   # tangent ⟂ gradient
        ty = np.where(mag > 1e-6,  gx / (mag + 1e-6), 0.0)
    return edge, region, tx, ty

def synth_stitch_height(color: np.ndarray, base_rgb: np.ndarray, p: dict):
    """Build the stripe-following relief height field from a colour texture."""
    edge, region, tx, ty = stripe_fields(color, base_rgb, p["color_thr"], p.get("edge_k", 2.0))
    H, W = region.shape
    d = distance_transform_edt(~edge)
    h = np.zeros((H, W), np.float32)
    # 1) edge ridge
    h += np.exp(-(d / max(p["ridge_w"], 1e-3)) ** 2) * p["ridge_h"]
    # 2) dashed stitches, in a band inset from the edge, periodic along the local tangent
    tsx = gaussian_filter(tx, 2.0); tsy = gaussian_filter(ty, 2.0)
    ys, xs = np.mgrid[0:H, 0:W]
    along = xs * tsx + ys * tsy
    band = (d >= p["stitch_off"]) & (d <= p["stitch_off"] + p["stitch_w"])
    duty = np.mod(along, max(p["stitch_spc"], 1e-3)) < p["dash_len"]
    dash = gaussian_filter((band & duty).astype(np.float32), 0.6) * p["stitch_h"]
    h += dash
    # 3) subtle in-band twill
    a = np.deg2rad(p["twill_ang"])
    twill = np.sin((xs * np.cos(a) + ys * np.sin(a)) / max(p["twill_prd"], 1e-3)) * p["twill_h"]
    h += np.where(region, twill, 0.0)
    return h, region, edge

def clean_base_normal(orig_normal_rgb: np.ndarray, orig_color: np.ndarray,
                      base_rgb: np.ndarray, p: dict) -> np.ndarray:
    """Heal the stock stripe-stitching out of the stock normal -> wrinkles + seams only."""
    old_edge, _, _, _ = stripe_fields(orig_color, base_rgb, p["color_thr"], p.get("edge_k", 2.0))
    reach = p["ridge_w"] + p["stitch_off"] + p["stitch_w"] + 2.0
    heal = distance_transform_edt(~old_edge) < reach
    base_n = rgb_to_n(orig_normal_rgb)
    smooth = np.stack([gaussian_filter(base_n[..., i], p["heal_blur"]) for i in range(3)], -1)
    smooth = smooth / np.linalg.norm(smooth, axis=-1, keepdims=True).clip(1e-6)
    return np.where(heal[..., None], smooth, base_n)

def generate_normal(orig_normal_rgb: np.ndarray, orig_color: np.ndarray,
                    new_color: np.ndarray, p: dict, heal: bool = True) -> np.ndarray:
    """Full pipeline -> new normal-map RGB (uint8).  All inputs same HxW."""
    old_base = base_fabric_rgb(orig_color)
    new_base = base_fabric_rgb(new_color)
    clean = (clean_base_normal(orig_normal_rgb, orig_color, old_base, p) if heal
             else rgb_to_n(orig_normal_rgb))
    h, _, _ = synth_stitch_height(new_color, new_base, p)
    det = _height_to_n(h, p["strength"])
    return n_to_rgb(_blend(clean, det))

def sample_style(orig_color: np.ndarray, orig_normal_rgb: np.ndarray, p: dict) -> dict:
    """Measure ridge width / stitch spacing / twill period off the stock normal near its
    stripe edges, to seed slider DEFAULTS.  Degrades gracefully to DEFAULTS on any failure."""
    out = dict(p)
    try:
        base_rgb = base_fabric_rgb(orig_color)
        edge, region, tx, ty = stripe_fields(orig_color, base_rgb, p["color_thr"], p.get("edge_k", 2.0))
        if edge.sum() < 50:
            return out
        n = rgb_to_n(orig_normal_rgb)
        # high-pass magnitude of the XY normal = where relief detail lives
        detail = np.hypot(n[..., 0], n[..., 1])
        hp = detail - gaussian_filter(detail, 6.0)
        d = distance_transform_edt(~edge)
        # ridge width: spread of |hp| vs distance from edge (first bin where it falls to ~1/e)
        near = hp[(d > 0) & (d < 8)]
        if near.size:
            prof = [np.mean(np.abs(hp[(d >= k) & (d < k + 1)]) ) for k in range(8)]
            prof = np.array(prof); pk = prof.max() + 1e-6
            w = next((k for k, v in enumerate(prof) if v < pk / np.e), 2)
            out["ridge_w"] = float(np.clip(w * 0.9, 1.0, 4.0))
        # stitch spacing: dominant period of |hp| in the stitch band along the edge tangent (FFT)
        band = region & (d > 1) & (d < 5)
        vals = np.abs(hp[band])
        if vals.size > 64:
            sig = vals - vals.mean()
            spec = np.abs(np.fft.rfft(sig))
            spec[:2] = 0
            k = int(np.argmax(spec)) or 1
            period = max(3.0, min(12.0, sig.size / k)) if k else p["stitch_spc"]
            # the raw index->period on a scrambled 1-D scan is only a hint; clamp to a sane band
            out["stitch_spc"] = float(np.clip(period if 3 <= period <= 12 else 6.0, 3.0, 12.0))
        # twill period: dominant fine period inside the band interior
        inband = region & (d > 4)
        iv = np.abs(hp[inband])
        if iv.size > 64:
            s2 = iv - iv.mean(); sp2 = np.abs(np.fft.rfft(s2)); sp2[:3] = 0
            k2 = int(np.argmax(sp2)) or 1
            out["twill_prd"] = float(np.clip(s2.size / k2 if k2 else 3.0, 2.0, 6.0))
    except Exception:
        pass
    return out

def _fit(color: np.ndarray, target_hw) -> np.ndarray:
    """Resize a colour array to (H,W) if needed (nearest keeps stripe edges crisp)."""
    H, W = target_hw
    if color.shape[:2] == (H, W):
        return color
    return np.asarray(Image.fromarray(color.astype(np.uint8)).resize((W, H), Image.NEAREST))

# ─────────────────────────────────────────────────────────────────────────────
#  GUI
# ─────────────────────────────────────────────────────────────────────────────
def open_stitcher(parent=None, game_dir=None, extract_root=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    standalone = parent is None
    root = tk.Tk() if standalone else tk.Toplevel(parent)
    root.title("NHL 2K10 — Jersey Normal Stitcher")
    root.geometry("1180x820")

    state = dict(orig_color=None, orig_normal=None, new_color=None,
                 game_dir=game_dir, extract_root=extract_root,
                 iff=None, normal_rec=None, base_rec=None,
                 new_from_edit=False, result=None, preview_imgtk=None)
    params = dict(DEFAULTS)
    sliders = {}

    # ---- layout: left = controls, right = preview ----
    left = ttk.Frame(root, padding=10); left.pack(side="left", fill="y")
    right = ttk.Frame(root, padding=10); right.pack(side="right", fill="both", expand=True)

    ttk.Label(left, text="Jersey Normal Stitcher", font=("Segoe UI", 13, "bold")).pack(anchor="w")
    ttk.Label(left, text="Regenerate the sewn-stripe relief so it follows your new striping.",
              wraplength=330, foreground="#555").pack(anchor="w", pady=(0, 8))

    status = tk.StringVar(value="Load a stock normal + colour, and your new colour.")

    # ── Source group ──────────────────────────────────────────────────────────
    src = ttk.LabelFrame(left, text="Sources", padding=8); src.pack(fill="x", pady=4)

    # Game load row
    grow = ttk.Frame(src); grow.pack(fill="x", pady=2)
    ttk.Label(grow, text="Team").grid(row=0, column=0, sticky="w")
    team_v = tk.StringVar(value="van"); kit_v = tk.StringVar(value="home")
    ttk.Combobox(grow, textvariable=team_v, values=TEAMS, width=6, state="readonly").grid(row=0, column=1, padx=3)
    ttk.Combobox(grow, textvariable=kit_v, values=KITS, width=6, state="readonly").grid(row=0, column=2, padx=3)

    def pick_game_dir():
        from tkinter import filedialog
        d = filedialog.askdirectory(title="Game files folder (with 0A/0B/1A/1B)")
        if d:
            state["game_dir"] = d; status.set(f"Game dir: {d}")
    ttk.Button(grow, text="Game folder…", command=pick_game_dir).grid(row=0, column=3, padx=3)

    def load_from_game():
        if archtex is None:
            messagebox.showerror("Unavailable", "archive_textures not importable — use the file loaders below.")
            return
        if not state.get("game_dir"):
            pick_game_dir()
            if not state.get("game_dir"):
                return
        iff = f"uniform_base_{team_v.get()}_{kit_v.get()}.iff"
        try:
            recs = archtex.list_textures(iff, state["game_dir"])
            base_rec = next(r for r in recs if r.get("label") == "base")
            norm_rec = next(r for r in recs if r.get("label") == "base_normal")
            oc = np.asarray(archtex.decode_record(iff, base_rec, state["game_dir"]).convert("RGB"))
            on = np.asarray(archtex.decode_record(iff, norm_rec, state["game_dir"]).convert("RGB"))
        except StopIteration:
            messagebox.showerror("Not found", f"{iff}: couldn't find base/base_normal records."); return
        except Exception as e:
            messagebox.showerror("Load failed", f"{iff}\n{e}"); return
        state.update(orig_color=oc, orig_normal=on, iff=iff, normal_rec=norm_rec, base_rec=base_rec)
        # Auto-load YOUR base colour straight from the game: the modified base texture if you've
        # edited it, otherwise the stock base.  No manual "New colour…" browse needed.
        nc, src_note, from_edit = oc.copy(), "stock base", False
        er = state.get("extract_root")
        if er and archtex is not None:
            try:
                ef = archtex.find_any_edit(er, iff, base_rec)
                if ef:
                    nc = np.asarray(Image.open(ef).convert("RGB"))
                    src_note = "your edited base"; from_edit = True
            except Exception:
                pass
        state["new_color"] = nc; state["new_from_edit"] = from_edit
        status.set(f"Loaded stock {iff}  (normal {on.shape[1]}×{on.shape[0]}) — new colour: {src_note}")
        do_sample(); regen()
    ttk.Button(src, text="⤓  Load stock from game", command=load_from_game).pack(fill="x", pady=(4, 2))

    def _load_img(kind):
        from tkinter import filedialog
        f = filedialog.askopenfilename(title=f"Choose {kind}",
                                       filetypes=[("Images", "*.png *.dds *.jpg *.bmp"), ("All", "*.*")])
        if not f:
            return None
        try:
            return np.asarray(Image.open(f).convert("RGB"))
        except Exception as e:
            messagebox.showerror("Open failed", str(e)); return None

    frow = ttk.Frame(src); frow.pack(fill="x", pady=2)
    def load_orig_normal():
        a = _load_img("stock base_normal (PNG)")
        if a is not None: state["orig_normal"] = a; status.set("Loaded stock normal (file)."); regen()
    def load_orig_color():
        a = _load_img("stock base colour (PNG)")
        if a is not None: state["orig_color"] = a; status.set("Loaded stock colour (file)."); regen()
    def load_new_color():
        a = _load_img("YOUR new base colour (PNG)")
        if a is not None:
            state["new_color"] = a; state["new_from_edit"] = False
            status.set("Loaded new colour (file)."); regen()
    ttk.Button(frow, text="Stock normal…", command=load_orig_normal).grid(row=0, column=0, sticky="ew", padx=1)
    ttk.Button(frow, text="Stock colour…", command=load_orig_color).grid(row=0, column=1, sticky="ew", padx=1)
    ttk.Button(frow, text="New colour…", command=load_new_color).grid(row=0, column=2, sticky="ew", padx=1)
    frow.columnconfigure((0, 1, 2), weight=1)

    heal_v = tk.BooleanVar(value=True)
    ttk.Checkbutton(src, text="Heal stock stitching first (recommended)", variable=heal_v,
                    command=lambda: regen()).pack(anchor="w", pady=(4, 0))

    # ── Sliders ───────────────────────────────────────────────────────────────
    sf = ttk.LabelFrame(left, text="Stitch style", padding=8); sf.pack(fill="x", pady=6)
    SPECS = [
        ("ridge_h", "Edge ridge height", 0.0, 1.5, 0.01),
        ("ridge_w", "Edge ridge width", 0.5, 5.0, 0.1),
        ("stitch_h", "Stitch height", 0.0, 1.2, 0.01),
        ("stitch_spc", "Stitch spacing", 3.0, 14.0, 0.5),
        ("dash_len", "Stitch dash length", 1.0, 10.0, 0.5),
        ("stitch_off", "Stitch inset", 0.0, 6.0, 0.5),
        ("twill_h", "Twill depth", 0.0, 0.3, 0.005),
        ("twill_prd", "Twill period", 1.5, 8.0, 0.25),
        ("twill_ang", "Twill angle", 0.0, 180.0, 5.0),
        ("strength", "Overall strength", 0.3, 4.0, 0.1),
        ("color_thr", "Stripe threshold", 10.0, 120.0, 2.0),
        ("edge_k", "Edge sensitivity", 0.5, 6.0, 0.25),
        ("heal_blur", "Heal blur", 1.0, 10.0, 0.5),
    ]
    _debounce = {"id": None}
    def _queue_regen():
        if _debounce["id"]:
            root.after_cancel(_debounce["id"])
        _debounce["id"] = root.after(180, regen)

    for key, lbl, lo, hi, step in SPECS:
        r = ttk.Frame(sf); r.pack(fill="x", pady=1)
        ttk.Label(r, text=lbl, width=16).pack(side="left")
        val = tk.DoubleVar(value=params[key])
        vlbl = ttk.Label(r, width=5, text=f"{params[key]:.2f}")
        vlbl.pack(side="right")
        def mk(k=key, v=val, vl=vlbl):
            def _cb(_=None):
                params[k] = float(v.get()); vl.config(text=f"{params[k]:.2f}"); _queue_regen()
            return _cb
        s = ttk.Scale(r, from_=lo, to=hi, variable=val, command=mk())
        s.pack(side="left", fill="x", expand=True, padx=4)
        sliders[key] = (val, vlbl)

    def do_sample():
        if state["orig_color"] is None or state["orig_normal"] is None:
            messagebox.showinfo("Sample", "Load the stock colour + stock normal first."); return
        oc = state["orig_color"]; on = _fit(state["orig_normal"], oc.shape[:2]) if False else state["orig_normal"]
        sm = sample_style(state["orig_color"], state["orig_normal"], params)
        for k, (v, vl) in sliders.items():
            if k in sm:
                params[k] = sm[k]; v.set(sm[k]); vl.config(text=f"{sm[k]:.2f}")
        status.set("Sampled stitch style from stock normal → defaults updated.")

    def reset_defaults():
        for k, (v, vl) in sliders.items():
            params[k] = DEFAULTS[k]; v.set(DEFAULTS[k]); vl.config(text=f"{DEFAULTS[k]:.2f}")
        regen()

    brow = ttk.Frame(sf); brow.pack(fill="x", pady=(6, 0))
    ttk.Button(brow, text="Sample from stock", command=lambda: (do_sample(), regen())).pack(side="left", expand=True, fill="x", padx=1)
    ttk.Button(brow, text="Reset", command=reset_defaults).pack(side="left", expand=True, fill="x", padx=1)

    # ── Output ────────────────────────────────────────────────────────────────
    of = ttk.LabelFrame(left, text="Output", padding=8); of.pack(fill="x", pady=6)
    def export_png():
        if state["result"] is None:
            messagebox.showinfo("Export", "Nothing generated yet."); return
        from tkinter import filedialog
        f = filedialog.asksaveasfilename(defaultextension=".png",
                                         filetypes=[("PNG", "*.png")], title="Save new base_normal")
        if f:
            Image.fromarray(state["result"]).save(f); status.set(f"Saved {f}")
    def _extract_root():
        r = state.get("extract_root")
        if r:
            return r
        gd = state.get("game_dir")                       # standalone fallback: <game>/NHL2k10_Extracted_Files
        return os.path.join(gd, "NHL2k10_Extracted_Files") if gd else None

    def apply_to_game():
        iff = state.get("iff")
        if archtex is None or not iff or state.get("normal_rec") is None:
            messagebox.showerror("Apply", "Use 'Load stock from game' first so the tool knows which IFF/slot to write."); return
        if state["result"] is None:
            messagebox.showinfo("Apply", "Nothing generated yet."); return
        root = _extract_root()
        if not root:
            messagebox.showerror("Apply",
                "No Extracted files folder — open the tool from the launcher, or pick a Game folder."); return
        nrec = state["normal_rec"]
        try:
            # 1) Write the generated normal as THIS jersey's base_normal EDIT file in the Extracted
            #    folder (so it shows up in the launcher's IFF tab and re-applies with Apply-All).
            edir = archtex.edit_dirs(root, iff)[0]        # …/Textures/Extracted/Uniform/<TEAM>/<KIT>
            os.makedirs(edir, exist_ok=True)
            stem = os.path.splitext(archtex.texture_filename(iff, nrec))[0]   # "base_normal"
            npath = os.path.join(edir, stem + ".png")
            Image.fromarray(state["result"]).save(npath)
            # 2) Apply ONLY this one jersey's IFF from the Extracted folder: reset it to clean, then
            #    splice back EVERY edited record it has (your base striping + our new normal together
            #    — never just the normal, so the base colour is preserved). prefer_lossless=False so
            #    the DXN normal stays DXN in-place (uniform_base is load-assigned — must not grow).
            recs = archtex.list_textures(iff, state["game_dir"])
            edits = []
            for rec in recs:
                p = archtex.find_any_edit(root, iff, rec)
                if p and rec.get("fmt") in archtex.REPLACE_FORMATS:
                    edits.append({**rec, "path": str(p)})
            logs = []
            archtex.ensure_clean(iff, state["game_dir"], logs.append)
            msg = archtex.replace_many(iff, edits, state["game_dir"], logs.append, prefer_lossless=False)
            labels = ", ".join(e.get("label", "t%d" % e["index"]) for e in edits)
            status.set(f"Applied {iff}: {msg}")
            messagebox.showinfo("Applied",
                f"{iff} updated from the Extracted folder.\n"
                f"Records re-applied: {labels}\n\n"
                f"New normal saved as an edit: {npath}\n\n{msg}")
        except Exception as e:
            messagebox.showerror("Apply failed", f"{e}\n\n{traceback.format_exc()}")
    ttk.Button(of, text="💾  Export normal PNG", command=export_png).pack(fill="x", pady=1)
    ttk.Button(of, text="▶  Save normal + apply this jersey", command=apply_to_game).pack(fill="x", pady=1)

    ttk.Label(left, textvariable=status, wraplength=330, foreground="#0a6").pack(anchor="w", pady=(8, 0))

    # ── Preview pane ──────────────────────────────────────────────────────────
    ttk.Label(right, text="Preview", font=("Segoe UI", 11, "bold")).pack(anchor="w")
    prow = ttk.Frame(right); prow.pack(fill="x", pady=2)
    view_v = tk.StringVar(value="Result normal")
    ttk.Combobox(prow, textvariable=view_v, state="readonly", width=22,
                 values=["Result normal", "Overlay: colour on normal",
                         "New colour", "Clean base", "Stitch height"]
                 ).pack(side="left")
    view_v.trace_add("write", lambda *_: show())
    # Alpha slider — how strongly the base colour shows over the normal (overlay view only),
    # so you can check the stitching lines up with your striping.
    ttk.Label(prow, text="  colour α").pack(side="left")
    overlay_a = tk.DoubleVar(value=0.5)
    ttk.Scale(prow, from_=0.0, to=1.0, variable=overlay_a, length=140,
              command=lambda *_: show() if view_v.get().startswith("Overlay") else None
              ).pack(side="left", padx=4)
    canvas = ttk.Label(right, relief="sunken"); canvas.pack(fill="both", expand=True, pady=4)

    PREV = 560  # preview working resolution (fast); export/apply run full-res

    def _prev_arrays():
        """Return small-res arrays for a responsive preview."""
        on, oc, nc = state["orig_normal"], state["orig_color"], state["new_color"]
        if on is None or nc is None:
            return None
        H, W = on.shape[:2]
        oc = _fit(oc, (H, W)) if oc is not None else nc
        nc = _fit(nc, (H, W))
        sc = min(1.0, PREV / max(H, W))
        if sc < 1.0:
            nh, nw = int(H * sc), int(W * sc)
            rs = lambda a: np.asarray(Image.fromarray(a.astype(np.uint8)).resize((nw, nh), Image.NEAREST))
            on, oc, nc = rs(on), rs(oc), rs(nc)
        # px-scale params for the downsampled preview so stitch density looks right
        pp = dict(params)
        for k in ("ridge_w", "stitch_off", "stitch_w", "stitch_spc", "dash_len", "twill_prd", "heal_blur"):
            pp[k] = max(0.3, params[k] * sc)
        return on, oc, nc, pp, sc

    def regen(*_):
        pa = _prev_arrays()
        if pa is None:
            return
        on, oc, nc, pp, sc = pa
        try:
            state["_clean"] = n_to_rgb(clean_base_normal(on, oc, base_fabric_rgb(oc), pp)
                                       if heal_v.get() else rgb_to_n(on))
            h, _, _ = synth_stitch_height(nc, base_fabric_rgb(nc), pp)
            state["_height"] = h
            state["_prev_result"] = generate_normal(on, oc, nc, pp, heal=heal_v.get())
            state["_prev_newcolor"] = nc
            # full-res result (for export/apply) — cheap enough on demand, but keep preview snappy:
            state["result"] = None  # computed lazily on export/apply via _full_result()
        except Exception as e:
            status.set(f"Preview error: {e}"); return
        show()

    def _full_result():
        on, oc, nc = state["orig_normal"], state["orig_color"], state["new_color"]
        H, W = on.shape[:2]
        oc = _fit(oc, (H, W)) if oc is not None else nc
        nc = _fit(nc, (H, W))
        return generate_normal(on, oc, nc, params, heal=heal_v.get())

    # export/apply need full-res: wrap them
    _orig_export = export_png; _orig_apply = apply_to_game
    def export_png_full():
        if state.get("_prev_result") is None:
            messagebox.showinfo("Export", "Load sources first."); return
        state["result"] = _full_result(); _orig_export()
    def apply_full():
        if state.get("_prev_result") is None:
            messagebox.showinfo("Apply", "Load sources first."); return
        state["result"] = _full_result(); _orig_apply()

    def show():
        v = view_v.get()
        arr = None
        if v == "Result normal":
            arr = state.get("_prev_result")
        elif v.startswith("Overlay"):
            nrm = state.get("_prev_result"); col = state.get("_prev_newcolor")
            if nrm is not None and col is not None:
                a = float(overlay_a.get())
                nf = nrm.astype(np.float32); cf = col.astype(np.float32)
                arr = np.clip(nf * (1.0 - a) + cf * a, 0, 255).astype(np.uint8)
        elif v == "New colour":
            arr = state.get("_prev_newcolor")
        elif v == "Clean base":
            arr = state.get("_clean")
        elif v == "Stitch height":
            h = state.get("_height")
            if h is not None:
                arr = (np.clip((h - h.min()) / (np.ptp(h) + 1e-6), 0, 1) * 255).astype(np.uint8)
        if arr is None:
            return
        im = Image.fromarray(arr)
        im.thumbnail((720, 720))
        state["preview_imgtk"] = ImageTk.PhotoImage(im)
        canvas.config(image=state["preview_imgtk"])

    # rebind the output buttons to the full-res wrappers (case-insensitive; the apply button label
    # is "Save normal + apply this jersey")
    for w in of.winfo_children():
        t = w.cget("text").lower()
        if "export" in t: w.config(command=export_png_full)
        elif "apply" in t: w.config(command=apply_full)

    if standalone:
        root.mainloop()
    return root


if __name__ == "__main__":
    open_stitcher(None)
