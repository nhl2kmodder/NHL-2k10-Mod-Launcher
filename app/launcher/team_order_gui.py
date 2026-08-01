"""team_order_gui.py — reorder the 30 NHL teams as they appear in every in-game menu list.

A plain reorderable list: pick a team, move it, Apply. The write itself (and the reason a
reorder is a physical record permutation rather than an index table) lives in team_order.py.

Nothing is written until Apply, and Apply takes a one-time <roster>.ROS.orderbak first.
The game reads the roster once at load, so a change only shows after a full restart.
"""
from __future__ import annotations
from pathlib import Path
from tkinter import (Toplevel, StringVar, Listbox, END, SINGLE, BOTH, X, Y, LEFT, RIGHT,
                     VERTICAL, messagebox)
from tkinter import ttk

try:
    from . import team_order as TO
except ImportError:
    import team_order as TO


def open_editor(app, ros_path):
    TeamOrderEditor(app, ros_path)


class TeamOrderEditor:
    def __init__(self, app, ros_path):
        self.app, self.ros = app, Path(ros_path)
        self.teams: list[dict] = []          # current on-screen order
        self.saved: list[str] = []           # codes as they are in the file right now

        w = self.win = Toplevel(app)
        w.title(f"Team Order — {self.ros.name}")
        w.geometry("560x640")

        bar = ttk.Frame(w, padding=(6, 6, 6, 2)); bar.pack(fill=X)
        ttk.Button(bar, text="Move up", command=lambda: self.move(-1)).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Move down", command=lambda: self.move(1)).pack(side=LEFT, padx=2)
        ttk.Separator(bar, orient=VERTICAL).pack(side=LEFT, fill=Y, padx=6)
        ttk.Button(bar, text="Sort A–Z", command=self.sort_az).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Reload", command=self.reload).pack(side=LEFT, padx=2)

        ttk.Label(w, foreground="#999", font=("Segoe UI", 8), justify=LEFT,
                  text="This is the order teams are listed in on the team-select screen and every "
                       "other menu list. Drag a team, or select it and use Move up / Move down.\n"
                       "Team IDs are not renumbered, so Default Matchup and everything else keeps "
                       "pointing at the same teams. Restart the game to see the new order."
                  ).pack(fill=X, padx=8, pady=(0, 4))

        body = ttk.Frame(w); body.pack(fill=BOTH, expand=True, padx=8)
        self.lb = Listbox(body, selectmode=SINGLE, activestyle="none",
                          font=("Consolas", 10), exportselection=False)
        sb = ttk.Scrollbar(body, command=self.lb.yview, orient=VERTICAL)
        self.lb.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y)
        self.lb.pack(side=LEFT, fill=BOTH, expand=True)
        self.lb.bind("<B1-Motion>", self.drag)

        self.v_status = StringVar(value="")
        ttk.Label(w, textvariable=self.v_status, foreground="#999",
                  font=("Segoe UI", 8)).pack(fill=X, padx=8, pady=(4, 0))

        foot = ttk.Frame(w, padding=(6, 4, 6, 8)); foot.pack(fill=X)
        self.b_apply = ttk.Button(foot, text="Apply to Roster.ROS", style="Accent.TButton",
                                  command=self.apply)
        self.b_apply.pack(side=LEFT, padx=2)
        ttk.Button(foot, text="Revert (.orderbak)", command=self.revert).pack(side=LEFT, padx=2)
        ttk.Button(foot, text="Close", command=w.destroy).pack(side=RIGHT, padx=2)

        self.reload()

    # ── data ──────────────────────────────────────────────────────────────────
    def reload(self):
        try:
            self.teams = TO.read_order(self.ros)
        except Exception as e:
            messagebox.showerror("Team Order", f"Could not read the team order:\n{e}", parent=self.win)
            self.win.destroy()
            return
        self.saved = [t["code"] for t in self.teams]
        self.redraw()

    def redraw(self, keep=None):
        self.lb.delete(0, END)
        for i, t in enumerate(self.teams):
            self.lb.insert(END, f"{i + 1:2d}.  {t['code']:<5s} {t['label']}")
        if keep is not None:
            self.lb.selection_set(keep)
            self.lb.see(keep)
        dirty = [t["code"] for t in self.teams] != self.saved
        self.v_status.set("unsaved changes — hit Apply" if dirty
                          else "matches the roster on disk")

    # ── reordering ────────────────────────────────────────────────────────────
    def _sel(self):
        s = self.lb.curselection()
        return s[0] if s else None

    def move(self, step):
        i = self._sel()
        if i is None:
            return
        j = i + step
        if not 0 <= j < len(self.teams):
            return
        self.teams[i], self.teams[j] = self.teams[j], self.teams[i]
        self.redraw(keep=j)

    def drag(self, ev):
        i = self._sel()
        j = self.lb.nearest(ev.y)
        if i is None or j == i or not 0 <= j < len(self.teams):
            return
        self.teams.insert(j, self.teams.pop(i))
        self.redraw(keep=j)

    def sort_az(self):
        self.teams.sort(key=lambda t: (t["city"] or t["label"]).lower())
        self.redraw()

    # ── writing ───────────────────────────────────────────────────────────────
    def apply(self):
        codes = [t["code"] for t in self.teams]
        if codes == self.saved:
            self.v_status.set("nothing to do — the roster is already in this order")
            return
        log = getattr(self.app, "log", print)
        try:
            moved = TO.apply_order(self.ros, codes, log=log)
        except Exception as e:
            messagebox.showerror("Team Order", f"The reorder was refused:\n{e}", parent=self.win)
            return
        self.reload()
        messagebox.showinfo("Team Order",
                            f"{moved} team(s) moved.\n\nRestart the game to see the new order.",
                            parent=self.win)

    def revert(self):
        if not messagebox.askyesno("Team Order",
                                   f"Restore {self.ros.name} from its .orderbak?\n\n"
                                   "This undoes the team order AND any other change made to the "
                                   "roster since the first reorder.", parent=self.win):
            return
        log = getattr(self.app, "log", print)
        if TO.revert(self.ros, log=log):
            self.reload()
        else:
            messagebox.showinfo("Team Order", "No .orderbak exists — nothing to revert to.",
                                parent=self.win)
