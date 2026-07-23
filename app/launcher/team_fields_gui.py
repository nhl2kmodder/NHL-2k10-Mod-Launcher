"""team_fields_gui.py — grid editor for every editable field of the NHL 2k10 team record.

Rows = the 30 teams, columns = the fields in team_fields.json. Double-click a cell to edit
(rgb cells open a colour picker). Nothing is written until "Save to Roster.ROS".

The point of this window is hands-on identification: change an `unidentified_N` column,
restart the game, see what moved, then hit "Edit field names (JSON)" to rename it — the
column re-labels itself on "Reload defs". No code change needed.
"""
from __future__ import annotations
import os, subprocess
from pathlib import Path
from tkinter import (Toplevel, StringVar, Entry, END, BOTH, X, Y, LEFT, RIGHT, W,
                     VERTICAL, BooleanVar, messagebox, Text, WORD)
from tkinter import ttk

import colorpick
import team_colors as TC
import team_fields as TF


def open_editor(app, ros_path):
    TeamFieldsEditor(app, ros_path)


class TeamFieldsEditor:
    def __init__(self, app, ros_path):
        self.app, self.ros = app, Path(ros_path)
        self.edits: dict[tuple[str, str], str] = {}     # (code, field) -> new text
        self.defs: list[dict] = []

        w = self.win = Toplevel(app)
        w.title(f"Team Record Fields — {self.ros.name}")
        w.geometry("1180x620")

        bar = ttk.Frame(w, padding=(6, 6, 6, 2)); bar.pack(fill=X)
        ttk.Button(bar, text="Reload defs", command=self.reload).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Edit field names (JSON)…",
                   command=self.open_defs).pack(side=LEFT, padx=2)
        ttk.Separator(bar, orient=VERTICAL).pack(side=LEFT, fill=Y, padx=6)
        self.v_led = BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Show arena-LED records (+30)", variable=self.v_led,
                        command=self.reload).pack(side=LEFT, padx=2)
        ttk.Separator(bar, orient=VERTICAL).pack(side=LEFT, fill=Y, padx=6)
        ttk.Button(bar, text="Hex dump of selected team…", command=self.hex_dump).pack(side=LEFT, padx=2)
        ttk.Separator(bar, orient=VERTICAL).pack(side=LEFT, fill=Y, padx=6)
        self.b_save = ttk.Button(bar, text="Save to Roster.ROS", style="Accent.TButton",
                                 command=self.save)
        self.b_save.pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Revert all (.colorbak)", command=self.revert).pack(side=LEFT, padx=2)

        self.v_status = StringVar(value="")
        ttk.Label(w, textvariable=self.v_status, foreground="#999",
                  font=("Segoe UI", 8)).pack(fill=X, padx=8)
        ttk.Label(w, foreground="#999", font=("Segoe UI", 8), justify=LEFT,
                  text="Double-click a cell to edit (rgb opens a colour picker). Edited cells turn "
                       "yellow; nothing is written until you Save. Colours/fields are cached at load, "
                       "so restart the game to see a change.\nIdentified an unidentified_N column? "
                       "Hit 'Edit field names (JSON)…', rename it, then 'Reload defs'."
                  ).pack(fill=X, padx=8, pady=(0, 4))

        wrap = ttk.Frame(w); wrap.pack(fill=BOTH, expand=True, padx=6, pady=4)
        self.tv = ttk.Treeview(wrap, show="headings", height=24)
        vs = ttk.Scrollbar(wrap, orient=VERTICAL, command=self.tv.yview)
        hs = ttk.Scrollbar(w, orient="horizontal", command=self.tv.xview)
        self.tv.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        vs.pack(side=RIGHT, fill=Y); self.tv.pack(side=LEFT, fill=BOTH, expand=True)
        hs.pack(fill=X, padx=6)
        self.tv.tag_configure("edited", background="#5a5320")
        self.tv.bind("<Double-1>", self.edit_cell)
        self.reload()

    # ── data ─────────────────────────────────────────────────────────────────
    def reload(self):
        if self.edits and not messagebox.askokcancel(
                "Reload", f"Discard {len(self.edits)} unsaved edit(s) and reload?", parent=self.win):
            return
        self.edits.clear()
        try:
            self.defs = TF.load_defs()
            rows = TF.load_rows(self.ros, led=self.v_led.get())
        except Exception as e:
            messagebox.showerror("Team Fields", f"Could not load:\n{e}", parent=self.win); return
        cols = ["code", "rec"] + [f["name"] for f in self.defs]
        self.tv.configure(columns=cols)
        self.tv.delete(*self.tv.get_children())
        for cid in cols:
            d = next((f for f in self.defs if f["name"] == cid), None)
            head = cid if d is None else f"{cid}  +0x{d['off']:03X}"
            self.tv.heading(cid, text=head)
            self.tv.column(cid, width=58 if cid in ("code", "rec") else
                                 max(88, min(190, len(head) * 7 + 16)), anchor=W, stretch=False)
        for r in rows:
            self.tv.insert("", END, iid=r["code"],
                           values=[r["code"], r["rec"]] + [r["values"][f["name"]] for f in self.defs])
        n_un = sum(1 for f in self.defs if f["name"].startswith("unidentified_"))
        self.v_status.set(f"{len(rows)} teams × {len(self.defs)} fields  ·  "
                          f"{len(self.defs) - n_un} named, {n_un} still unidentified  ·  "
                          f"defs: {TF.defs_path()}")

    def _fdef(self, name):
        return next((f for f in self.defs if f["name"] == name), None)

    # ── editing ──────────────────────────────────────────────────────────────
    def edit_cell(self, event):
        tv = self.tv
        if tv.identify("region", event.x, event.y) != "cell":
            return
        col, row = tv.identify_column(event.x), tv.identify_row(event.y)
        if not row:
            return
        idx = int(col[1:]) - 1
        cols = tv.cget("columns")
        cname = cols[idx]
        if cname in ("code", "rec"):
            return
        f = self._fdef(cname)
        if f is None:
            return
        cur = tv.set(row, cname)

        if f["type"] == "rgb":
            init = cur if cur.startswith("#") else "#808080"
            hx = colorpick.ask_color(self.win, init, f"{row} — {cname}")
            if hx:
                self._commit(row, cname, hx)
            return

        x, y, wd, ht = tv.bbox(row, col)
        e = Entry(tv); e.place(x=x, y=y, width=wd, height=ht)
        e.insert(0, cur); e.focus_set(); e.select_range(0, END)

        def commit(_=None):
            val = e.get(); e.destroy()
            if val != cur:
                self._commit(row, cname, val)
        e.bind("<Return>", commit); e.bind("<FocusOut>", commit)
        e.bind("<Escape>", lambda _: e.destroy())

    def _commit(self, code, cname, val):
        f = self._fdef(cname)
        try:                                            # validate now, not at save time
            TF.encode(val, f["type"], f["size"])
        except ValueError as e:
            messagebox.showerror("Team Fields", f"{cname}: {e}", parent=self.win); return
        self.tv.set(code, cname, val)
        self.edits[(code, cname)] = val
        self.tv.item(code, tags=("edited",))
        self.b_save.configure(text=f"Save to Roster.ROS ({len(self.edits)})")

    # ── actions ──────────────────────────────────────────────────────────────
    def save(self):
        if not self.edits:
            messagebox.showinfo("Team Fields", "No edits to save.", parent=self.win); return
        if self.v_led.get() and not messagebox.askokcancel(
                "Team Fields", "You are editing the arena-LED records (+30), not the team colours. "
                               "Save anyway?", parent=self.win):
            return
        try:
            edits = [(c, f, v) for (c, f), v in self.edits.items()]
            n = TF.save_rows(self.ros, edits, led=self.v_led.get(), log=self.app._log)
        except Exception as e:
            messagebox.showerror("Team Fields", f"Nothing was written:\n{e}", parent=self.win); return
        self.edits.clear()
        self.b_save.configure(text="Save to Roster.ROS")
        for i in self.tv.get_children():
            self.tv.item(i, tags=())
        self.app._log(f"[team fields] wrote {n} field(s) to {self.ros.name}")
        messagebox.showinfo("Team Fields", f"Wrote {n} field(s).\n\nRestart the game to see them.",
                            parent=self.win)
        if hasattr(self.app, "_prompt_restart_if_running"):
            self.app._prompt_restart_if_running()

    def revert(self):
        if not messagebox.askokcancel("Revert", "Restore the whole roster from its .colorbak "
                                                "backup? Every colour/field edit is undone.",
                                      parent=self.win):
            return
        if TC.revert(self.ros, log=self.app._log):
            self.reload()
            messagebox.showinfo("Revert", "Roster restored from .colorbak.", parent=self.win)
        else:
            messagebox.showwarning("Revert", "No .colorbak backup exists yet.", parent=self.win)

    def open_defs(self):
        p = TF.defs_path()
        try:
            os.startfile(str(p))
        except Exception:
            subprocess.Popen(["notepad.exe", str(p)])
        messagebox.showinfo("Field names",
                            f"Editing:\n{p}\n\nRename a field's \"name\" (and put what you learned in "
                            "\"note\"), save the file, then click 'Reload defs'.", parent=self.win)

    def hex_dump(self):
        sel = self.tv.selection()
        if not sel:
            messagebox.showinfo("Hex", "Select a team row first.", parent=self.win); return
        try:
            txt = TF.record_hex(self.ros, sel[0], led=self.v_led.get())
        except Exception as e:
            messagebox.showerror("Hex", str(e), parent=self.win); return
        w = Toplevel(self.win); w.title(f"{sel[0]} — raw record"); w.geometry("640x560")
        t = Text(w, wrap=WORD, font=("Consolas", 9)); t.pack(fill=BOTH, expand=True)
        t.insert("1.0", txt); t.configure(state="disabled")
