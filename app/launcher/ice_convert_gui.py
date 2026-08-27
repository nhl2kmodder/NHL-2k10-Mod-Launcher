"""ice_convert_gui.py — the Ice Conversion tab: thefaceoff.net sheet -> a team's two ice textures.

All of the image work is in `ice_convert` (pure, no UI, CLI-testable). This module is the tab
around it: pick a team, see the two 1024x4096 ice textures the game currently has for it
(Regular — stored as "playoffs" — and Finals), point at a reference sheet, Convert to preview
both variations, and Write to splice them into the archives.

Picking a team LOADS ITS LIVE TEXTURES — mods included — exactly like the Jersey Editor loads a
kit, so the preview always answers "what would I see in the game right now". Convert swaps the
previews for the converted pair (flagged "converted — not written"); changing team throws an
unwritten conversion away rather than letting a VAN conversion silently write over TOR.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

import tkinter as tk
from tkinter import (BOTH, LEFT, RIGHT, W, X, Y, StringVar,
                     filedialog, messagebox, ttk)

from PIL import Image, ImageTk

from . import archive_textures as archtex
from . import ice_convert as IC
from . import ice_fetch
from . import team_tag

# What the two game assets mean on screen. "playoffs" art doubles as the regular-season ice,
# so it is surfaced as "Regular" — same wording as the Textures tab's catalog labels.
VARIANT_LABEL = {"playoffs": "Regular  (ice_<team>_playoffs)", "finals": "Finals  (ice_<team>_finals)"}
PREVIEW_H = 720                     # on-screen height of one 1024x4096 strip


def build_tab(app, frame):
    tab = IceConvertTab(app, frame)
    app._ice_convert = tab
    return tab


class IceConvertTab:
    def __init__(self, app, frame):
        self.app = app
        self.frame = frame
        self.teams = {}             # display label -> lowercase asset code
        self.iffs = {}              # code -> {"playoffs": iff, "finals": iff}
        self.converted = None       # {"playoffs": Image, "finals": Image} awaiting a write
        self.reference = None       # Path of the picked reference sheet
        self._photo = {}            # variant -> ImageTk (must outlive the canvas draw)
        self._load_seq = 0          # stale-thread guard for team preview loads

        self.v_team = StringVar()
        self.v_ref = StringVar(value="no reference selected")
        self.v_status = StringVar(value="Pick a team.")
        self.v_src = {v: StringVar(value="") for v in IC.VARIANTS}

        top = ttk.Frame(frame, padding=(8, 8, 8, 4))
        top.pack(fill=X)
        ttk.Label(top, text="Team:").pack(side=LEFT)
        self.cb_team = ttk.Combobox(top, textvariable=self.v_team, state="readonly", width=34)
        self.cb_team.pack(side=LEFT, padx=(4, 16))
        self.cb_team.bind("<<ComboboxSelected>>", lambda e: self._team_changed())

        ttk.Label(top, text="Reference sheet:").pack(side=LEFT)
        ttk.Label(top, textvariable=self.v_ref, width=28, anchor=W,
                  relief="sunken", padding=(4, 2)).pack(side=LEFT, padx=4)
        ttk.Button(top, text="Browse…", command=self.pick_reference).pack(side=LEFT, padx=(0, 16))

        self.btn_convert = ttk.Button(top, text="Convert", command=self.convert,
                                      state="disabled")
        self.btn_convert.pack(side=LEFT, padx=(0, 6))
        self.btn_write = ttk.Button(top, text="Write to game files", command=self.write,
                                    state="disabled")
        self.btn_write.pack(side=LEFT)

        ttk.Separator(top, orient="vertical").pack(side=LEFT, fill=Y, padx=12)
        ttk.Button(top, text="Auto-Grab from thefaceoff.net…",
                   command=self.auto_grab).pack(side=LEFT)

        ttk.Label(frame, textvariable=self.v_status, padding=(8, 0)).pack(anchor=W)

        # Two portrait strips side by side, one per variant, each with a caption and a source
        # tag ("game files" / "converted — not written").
        row = ttk.Frame(frame, padding=8)
        row.pack(fill=BOTH, expand=True)
        self.canvas = {}
        pw = round(PREVIEW_H * 1024 / 4096)
        for v in IC.VARIANTS:
            col = ttk.Frame(row)
            col.pack(side=LEFT, padx=(0, 24), anchor="n")
            ttk.Label(col, text=VARIANT_LABEL[v]).pack(anchor=W)
            c = tk.Canvas(col, width=pw, height=PREVIEW_H, bg="#141414",
                          highlightthickness=1, highlightbackground="#333")
            c.pack()
            ttk.Label(col, textvariable=self.v_src[v], foreground="#888").pack(anchor=W)
            self.canvas[v] = c

        self.refresh_teams()

    # ── teams ─────────────────────────────────────────────────────────────────

    def refresh_teams(self):
        """Every team with ice assets on this install, from the catalog rather than a fixed
        list so expansion/relocated teams show up too (same dance as the Jersey Editor)."""
        if not archtex.EXTRA_TEAM_ROWS:
            try:
                self.app._iff_discover_extra_teams()
            except Exception as e:
                self.app._log_q.put(f"[ice] expansion-team scan failed: {e}")
        try:
            rows = [r for r in archtex.load_catalog() if r.get("category") == "ice"]
        except Exception as e:
            self.v_status.set(f"catalog unavailable: {e}")
            return
        self.iffs = {}
        for r in rows:
            code, iff = r.get("team", ""), r.get("iff", "")
            if not code or not iff:
                continue
            var = "finals" if "_finals" in iff else "playoffs"
            self.iffs.setdefault(code, {})[var] = iff
        self.teams = {}
        for code in sorted(self.iffs):
            name = team_tag.canon(code)
            label = f"{code.upper()} — {name}" if name and name.upper() != code.upper() \
                else code.upper()
            self.teams[label] = code
        self.cb_team["values"] = list(self.teams)

    def _team(self):
        return self.teams.get(self.v_team.get())

    def _team_changed(self):
        # An unwritten conversion belongs to the previous team; drop it loudly.
        if self.converted:
            self.app._log_q.put("[ice] unwritten conversion discarded (team changed)")
        self.converted = None
        self.btn_write.config(state="disabled")
        self.btn_convert.config(state="normal" if self.reference else "disabled")
        self.load_previews()

    # ── previews ──────────────────────────────────────────────────────────────

    def load_previews(self):
        """Both variants from the LIVE archives (so applied mods show), off-thread."""
        code = self._team()
        if not code:
            return
        if not self.app._get_game_root():
            self.v_status.set("Set the game-files folder in Settings first.")
            return
        self._load_seq += 1
        seq = self._load_seq
        pair = dict(self.iffs.get(code, {}))
        self.v_status.set(f"loading {code.upper()} ice from game files…")

        def work():
            imgs = {}
            for v, iff in pair.items():
                try:
                    imgs[v] = archtex.decode_preview_current(iff)
                except Exception as e:
                    self.app._log_q.put(f"[ice] {iff}: {e}")
                    imgs[v] = None
            self.frame.after(0, lambda: self._show_loaded(seq, code, imgs))
        threading.Thread(target=work, daemon=True).start()

    def _show_loaded(self, seq, code, imgs):
        if seq != self._load_seq or self.converted:      # stale load, or Convert won the race
            return
        for v in IC.VARIANTS:
            img = imgs.get(v)
            self._draw(v, img, "game files" if img is not None else "could not decode")
        self.v_status.set(f"{code.upper()} — current game-file ice shown.")

    def _draw(self, variant, img, tag):
        c = self.canvas[variant]
        c.delete("all")
        self.v_src[variant].set(tag)
        if img is None:
            return
        w = int(c["width"]); h = int(c["height"])
        p = ImageTk.PhotoImage(img.convert("RGB").resize((w, h), Image.LANCZOS))
        self._photo[variant] = p
        c.create_image(0, 0, image=p, anchor="nw")

    # ── convert ───────────────────────────────────────────────────────────────

    def pick_reference(self):
        p = filedialog.askopenfilename(
            title="thefaceoff.net reference sheet",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp"), ("All files", "*.*")])
        if not p:
            return
        self.reference = Path(p)
        self.v_ref.set(self.reference.name)
        if self._team():
            self.btn_convert.config(state="normal")

    def convert(self):
        if not (self.reference and self._team()):
            return
        ref, code = self.reference, self._team()
        self.btn_convert.config(state="disabled")
        self.v_status.set("converting…")

        def work():
            try:
                outs = IC.convert_both(ref)
            except Exception as e:
                self.app._log_q.put(f"[ice] convert failed: {e}")
                self.frame.after(0, lambda: (
                    self.v_status.set(f"convert failed: {e}"),
                    self.btn_convert.config(state="normal")))
                return
            self.frame.after(0, lambda: self._show_converted(code, outs))
        threading.Thread(target=work, daemon=True).start()

    def _show_converted(self, code, outs):
        if code != self._team():                         # team changed mid-convert
            self.btn_convert.config(state="normal")
            return
        self.converted = outs
        for v in IC.VARIANTS:
            self._draw(v, outs[v], "converted — not written")
        self.btn_convert.config(state="normal")
        self.btn_write.config(state="normal")
        self.v_status.set(f"{code.upper()} — converted preview. "
                          "'Write to game files' makes it real.")

    # ── write ─────────────────────────────────────────────────────────────────

    def write(self):
        code = self._team()
        if not (self.converted and code):
            return
        root = self.app._get_game_root()
        if not root:
            messagebox.showerror("Ice Conversion", "Set the game-files folder in Settings.")
            return
        if self.app._op_busy():
            return
        pair = self.iffs.get(code, {})
        if not messagebox.askyesno(
                "Write ice textures",
                f"Write BOTH converted ice sheets into the game files for {code.upper()}?\n\n"
                f"  {pair.get('playoffs')}  (regular season + playoffs)\n"
                f"  {pair.get('finals')}\n\n"
                "The originals are backed up on first edit (.orig)."):
            return
        outs = {v: img.copy() for v, img in self.converted.items()}
        self.app._run_in_thread(self._write_worker, code, pair, outs, root,
                                op_label="Writing ice textures…")

    def _splice(self, code, pair, outs, root, log):
        """Both variants of one team's converted ice into the live archives."""
        tmp = Path(tempfile.mkdtemp(prefix="iceconv_"))
        try:
            for v in IC.VARIANTS:
                iff = pair.get(v)
                if not iff:
                    log(f"[ice] no {v} asset for {code} — skipped")
                    continue
                p = tmp / f"ice_{code}_{v}.png"
                outs[v].save(p)
                log(f"[ice] {iff}:")
                archtex.ensure_clean(iff, root, log)
                log("  " + str(archtex.replace(iff, p, root, log)))
                self._mirror_to_extracted(iff, p, log)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _mirror_to_extracted(self, iff, png, log):
        """Copy the converted PNG into Textures/Extracted/Ice/<TEAM>/ — that tree is what the
        Textures-tab preview (MODIFIED), Apply All, and modpack export all key off; a direct
        archive splice alone is invisible to them. No mark_extracted(): the file must LOOK
        edited (no pristine hash) so is_edited() keeps it in modpacks and Apply All."""
        try:
            ex = self.app._root_ex_quiet()
            if ex is None:
                log(f"[ice] no extracted-files root set — {iff} edit not mirrored for modpacks")
                return
            d = archtex.extracted_root(ex) / archtex.asset_iff(iff)
            d.mkdir(parents=True, exist_ok=True)
            dst = (d / archtex.texture_filename(iff)).with_suffix(".png")
            shutil.copyfile(png, dst)
            old = dst.with_suffix(".dds")            # a stale extract would shadow nothing (PNG
            if old.exists():                         # wins), but clean it up so the folder is
                old.unlink()                         # unambiguous about which file is the edit
        except Exception as e:
            log(f"[ice] WARNING: could not mirror {iff} into Extracted/ ({e})")

    def _write_worker(self, code, pair, outs, root):
        log = self.app._log_q.put
        self._splice(code, pair, outs, root, log)
        log("[ice] done.")
        # Back on the UI thread: the write landed, so the live archives ARE the conversion now.
        def settle():
            self.converted = None
            self.btn_write.config(state="disabled")
            self.load_previews()
        self.frame.after(0, settle)

    # ── auto-grab from thefaceoff.net ─────────────────────────────────────────

    def auto_grab(self):
        """Pick teams, then download each one's newest sheet from thefaceoff.net,
        convert, and write straight into the archives — no preview round-trip."""
        root = self.app._get_game_root()
        if not root:
            messagebox.showerror("Ice Conversion", "Set the game-files folder in Settings.")
            return
        if self.app._op_busy():
            return

        dlg = tk.Toplevel(self.frame)
        dlg.title("Auto-Grab ice from thefaceoff.net")
        dlg.transient(self.frame.winfo_toplevel())
        dlg.grab_set()
        ttk.Label(dlg, padding=(10, 8, 10, 4), justify="left", text=(
            "Downloads each selected team's newest full-rink sheet from\n"
            "thefaceoff.net (2027 first, then 2026, and so on), converts it, and\n"
            "writes BOTH ice textures straight into the game files — no preview.\n"
            "Originals are backed up on first edit (.orig).")).pack(anchor=W)

        vars_ = {}
        grid = ttk.Frame(dlg, padding=(10, 4))
        grid.pack(fill=BOTH, expand=True)
        for i, (label, code) in enumerate(self.teams.items()):
            v = tk.BooleanVar(value=False)
            vars_[code] = v
            site = ice_fetch.SITE_TEAM.get(code)
            name = team_tag.canon(code)
            if site and name and site.lower() != name.lower():
                label += f"   ← {site}"       # relocated: show whose ice it gets
            ttk.Checkbutton(grid, text=label, variable=v).grid(
                row=i % 16, column=i // 16, sticky=W, padx=(0, 24))

        bar = ttk.Frame(dlg, padding=10)
        bar.pack(fill=X)
        ttk.Button(bar, text="All", width=6, command=lambda: [
            v.set(True) for v in vars_.values()]).pack(side=LEFT)
        ttk.Button(bar, text="None", width=6, command=lambda: [
            v.set(False) for v in vars_.values()]).pack(side=LEFT, padx=(4, 0))

        def start():
            codes = [c for c, v in vars_.items() if v.get()]
            if not codes:
                return
            if not messagebox.askyesno(
                    "Auto-Grab ice",
                    f"Download, convert, and WRITE ice for {len(codes)} team(s)?",
                    parent=dlg):
                return
            dlg.destroy()
            self.app._run_in_thread(self._grab_worker, codes, root,
                                    op_label="Auto-grabbing ice…")
        ttk.Button(bar, text="Grab && Apply", command=start).pack(side=RIGHT)
        ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side=RIGHT, padx=(0, 6))

    def _grab_worker(self, codes, root):
        log = self.app._log_q.put
        def status(s):
            self.frame.after(0, self.v_status.set, s)

        status("looking up the newest sheets on thefaceoff.net…")
        sheets = ice_fetch.find_sheets(codes, log=log)
        done, failed = [], [c for c in codes if c not in sheets]
        for c in failed:
            log(f"[ice] {c}: no sheet found on thefaceoff.net — skipped")
        for i, c in enumerate(sorted(sheets), 1):
            info = sheets[c]
            tag = f"[{i}/{len(sheets)}] {c.upper()} ({info['year']})"
            try:
                status(f"{tag} — downloading…")
                img = ice_fetch.fetch_image(info["url"])
                status(f"{tag} — converting…")
                outs = IC.convert_both(img)
                status(f"{tag} — writing…")
                self._splice(c, self.iffs.get(c, {}), outs, root, log)
                done.append(c)
            except Exception as e:
                log(f"[ice] {c}: {e}")
                failed.append(c)
        summary = f"auto-grab done: {len(done)} written" + \
                  (f", {len(failed)} failed ({', '.join(failed)})" if failed else "")
        log("[ice] " + summary)
        def settle():
            self.v_status.set(summary)
            if self._team() in done and not self.converted:
                self.load_previews()          # current team's ice just changed on disk
        self.frame.after(0, settle)
