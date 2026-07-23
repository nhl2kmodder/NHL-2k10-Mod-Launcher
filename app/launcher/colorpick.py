"""colorpick.py — one colour prompt that takes a pasted hex OR the OS picker.

The picker alone is fine for eyeballing a colour but useless when you already HAVE the value
(a team's real hex from a brand guide, a value copied from another tool, or #FF1493 as a
marker). This gives an entry you can paste into, with the picker one click away, and a live
swatch so you can see what you typed before committing.

ask_color(parent, initial="#RRGGBB", title=...) -> "#RRGGBB" or None if cancelled.
"""
from __future__ import annotations
import re
from tkinter import Toplevel, StringVar, Entry, Label, END, X, LEFT, RIGHT, BOTH
from tkinter import ttk, colorchooser

_HEX_RX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def parse_hex(text: str) -> str | None:
    """'#1e90ff' / '1E90FF' / '#abc' / ' 1e90ff ' -> '#1E90FF'. None if it isn't a colour."""
    m = _HEX_RX.match((text or "").strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:                       # #abc -> #aabbcc
        h = "".join(c * 2 for c in h)
    return "#" + h.upper()


def to_rgb(hx: str) -> tuple[int, int, int]:
    h = parse_hex(hx) or "#000000"
    return (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16))


def ask_color(parent, initial: str = "#808080", title: str = "Colour") -> str | None:
    """Modal: paste/type a hex, or click Pick. Returns '#RRGGBB' or None."""
    start = parse_hex(initial) or "#808080"
    win = Toplevel(parent)
    win.title(title)
    win.resizable(False, False)
    win.transient(parent)
    result: dict[str, str | None] = {"v": None}

    body = ttk.Frame(win, padding=10)
    body.pack(fill=BOTH, expand=True)
    ttk.Label(body, text="Hex colour (paste or type):").pack(anchor="w")

    row = ttk.Frame(body); row.pack(fill=X, pady=(4, 2))
    var = StringVar(value=start)
    ent = Entry(row, textvariable=var, width=14, font=("Consolas", 11))
    ent.pack(side=LEFT)
    swatch = Label(row, text="   ", relief="solid", borderwidth=1, width=6)
    swatch.pack(side=LEFT, padx=8)
    msg = ttk.Label(body, text="", foreground="#c33", font=("Segoe UI", 8))
    msg.pack(anchor="w")

    def refresh(*_):
        hx = parse_hex(var.get())
        if hx:
            swatch.configure(background=hx)
            msg.configure(text="")
            ok.state(["!disabled"])
        else:
            msg.configure(text="Enter #RRGGBB (or #RGB)")
            ok.state(["disabled"])
        return True

    def pick():
        cur = parse_hex(var.get()) or start
        _, hx = colorchooser.askcolor(color=cur, title=title, parent=win)
        if hx:
            var.set(hx.upper())

    def commit(*_):
        hx = parse_hex(var.get())
        if not hx:
            return
        result["v"] = hx
        win.destroy()

    btns = ttk.Frame(body); btns.pack(fill=X, pady=(10, 0))
    ttk.Button(btns, text="Pick…", command=pick).pack(side=LEFT)
    ttk.Button(btns, text="Cancel", command=win.destroy).pack(side=RIGHT)
    ok = ttk.Button(btns, text="OK", style="Accent.TButton", command=commit)
    ok.pack(side=RIGHT, padx=6)

    var.trace_add("write", refresh)
    refresh()
    ent.focus_set(); ent.select_range(0, END)
    win.bind("<Return>", commit)
    win.bind("<Escape>", lambda _e: win.destroy())
    # centre on the parent so it doesn't land on another monitor
    win.update_idletasks()
    try:
        x = parent.winfo_rootx() + (parent.winfo_width() - win.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
    except Exception:
        pass
    win.grab_set()
    parent.wait_window(win)
    return result["v"]
