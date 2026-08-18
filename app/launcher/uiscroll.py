"""
uiscroll.py — "am I seeing all of this?", answered by the widget itself.

Tk will happily lay out a column of panels taller than the window and then just clip it. Nothing
errors, nothing scrolls; the buttons at the bottom simply are not there. That is what the head
editor does on a short screen, and six or seven other places in this launcher had already grown
their own hand-rolled Canvas + scrollregion block to dodge it. None of those detect anything —
they scroll always, so short content gets a dead scrollbar sitting next to it.

`ScrollHost` is the one implementation, and the detection is the point of it:

  · IT MEASURES.  `body.winfo_reqheight()` is what the content ASKED for; the canvas height is
    what it GOT. Bigger asked than got means something is off-screen, and only then does the
    scrollbar appear. Same test horizontally. So a panel that fits looks exactly as it did.

  · IT DOES NOT COLLAPSE `expand=True`.  This is the trap in every naive version. A frame inside
    a canvas window is sized to its REQUEST, so a Treeview packed `fill=BOTH, expand=True` stops
    filling its tab the moment you wrap it. Here the window item is stretched to the viewport
    whenever the content fits, and only shrinks back to the requested size when it genuinely
    overflows — so wrapping a tab that already fitted changes nothing about how it lays out.

  · THE WHEEL GOES TO THE RIGHT WIDGET.  A Listbox or Text under the pointer that can still
    scroll itself keeps the wheel; the outer view only takes it when the inner one is at its end
    or has nothing to scroll. Otherwise wrapping a panel would break every list inside it.

`fit_to_screen` is the other half of the same complaint. A window that asks for 760 px of height
on a 768 px laptop loses its bottom edge behind the taskbar before any of this can help, so a
Toplevel is clamped to the desktop WORK AREA (the screen minus the taskbar, read from Windows
where possible) instead of to the raw screen size.
"""
import time
import tkinter as tk
from tkinter import ttk

__all__ = ["ScrollHost", "scrollable", "fit_to_screen", "work_area"]

_PAD = 2                      # slack, in px, before content counts as overflowing


def work_area(widget):
    """Usable desktop, taskbar excluded. -> (w, h).

    Windows can say exactly (SPI_GETWORKAREA); anywhere else, and if that call fails, fall back
    to the raw screen less a margin that is about what a taskbar and a title bar cost.
    """
    try:
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            w, h = r.right - r.left, r.bottom - r.top
            if w > 200 and h > 200:
                return int(w), int(h)
    except Exception:
        pass
    return int(widget.winfo_screenwidth() * 0.95), int(widget.winfo_screenheight() * 0.90)


def fit_to_screen(top, width, height, minimum=(640, 400)):
    """Size `top` to width x height, or to as much of that as the desktop actually has.

    Returns the geometry string used. Also sets a minsize, so the window can be dragged small
    without the layout fighting back — the ScrollHosts inside it are what make that survivable.
    """
    aw, ah = work_area(top)
    w = max(minimum[0], min(int(width), aw))
    h = max(minimum[1], min(int(height), ah - 40))       # -40: title bar is outside the client area
    geo = f"{w}x{h}"
    try:
        top.geometry(geo)
        top.minsize(minimum[0], minimum[1])
    except Exception:
        pass
    return geo


def _can_scroll(w, dy):
    """Does widget `w` have its own vertical scroll left to give in direction `dy`? -> bool."""
    try:
        first, last = w.yview()
    except Exception:
        return False
    if last - first >= 0.999:                            # everything already visible
        return False
    return (first > 0.0005) if dy > 0 else (last < 0.9995)


# ── wheel routing ────────────────────────────────────────────────────────────
# ONE handler per interpreter over a registry of live hosts, rather than each host binding and
# unbinding `<MouseWheel>` on the "all" tag as the pointer crosses it. Tk's only removal API for
# that tag is unbind_all, which deletes EVERY binding on the sequence — so the enter/leave version
# has one host tearing down another's binding on the way past, and every closed head editor leaves
# a handler behind pointing at a destroyed widget. Routing from a registry has neither problem, and
# it makes nesting well-defined: the host that acts is the INNERMOST one under the pointer, which
# is decided by walking up from the widget the pointer is actually over, not by binding order.
_HOSTS = []


def _dispatch(event):
    dy = -1 if getattr(event, "num", 0) == 5 else 1 if getattr(event, "num", 0) == 4 \
        else (1 if getattr(event, "delta", 0) > 0 else -1)
    w = getattr(event, "widget", None)
    if not isinstance(w, tk.Misc):
        return
    try:
        under = w.winfo_containing(event.x_root, event.y_root) or w
    except Exception:
        under = w
    live = [h for h in _HOSTS if h.winfo_exists()]
    if len(live) != len(_HOSTS):
        _HOSTS[:] = live
    while isinstance(under, tk.Misc):
        # The host's OWN canvas is reached before the host is, and a canvas answers yview() — so
        # without this the `_can_scroll` test below hands the wheel to the canvas, which has no
        # binding, and the view never moves. The tag is what makes the canvas mean "the host".
        owner = getattr(under, "_uiscroll_owner", None)
        if owner is not None:
            return owner.scroll_by(dy)
        if isinstance(under, ScrollHost):
            return under.scroll_by(dy)
        if _can_scroll(under, dy):                       # a Listbox/Text keeps its own wheel
            return
        try:
            under = under.master
        except Exception:
            return


def _ensure_dispatcher(widget):
    root = widget.winfo_toplevel()
    try:
        root = root.nametowidget(".")
    except Exception:
        pass
    if getattr(root, "_uiscroll_wheel", False):
        return
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        widget.bind_all(seq, _dispatch, add="+")
    try:
        root._uiscroll_wheel = True
    except Exception:
        pass
    _poll(root)


# ── watching for content that grows after layout ─────────────────────────────
# `<Configure>` is not enough on its own, and the reason is the stretch above. Pinning the body to
# the viewport while it fits means the body's ALLOCATED size stops changing — so when a builder
# adds another panel, or a list fills in, Tk has nothing to report and the bar never appears. What
# changed is the REQUESTED size, which fires no event at all. So the registry is swept on a timer
# and a host is only re-measured when its request actually moved: two winfo calls per host per
# quarter-second, against a bug that otherwise only shows up as silently clipped content.
_POLL_MS = 250
_LAST_POLL = [0.0]


def _ensure_poll(widget):
    """Restart the sweep if it has stopped.

    It does stop: the launcher cancels EVERY pending after() when it rebuilds its front end for a
    mode switch (`after info` -> after_cancel, so that deferred tab loads cannot land on destroyed
    widgets), and this chain goes with them. Nothing announces that, so instead of trusting the
    chain to live forever, any host that is about to re-measure checks whether the sweep has gone
    quiet and starts it again.
    """
    if time.monotonic() - _LAST_POLL[0] < _POLL_MS / 1000.0 * 3:
        return
    try:
        _poll(widget.winfo_toplevel().nametowidget("."))
    except Exception:
        pass


def _poll(root):
    _LAST_POLL[0] = time.monotonic()
    live = []
    for h in _HOSTS:
        try:
            if not h.winfo_exists():
                continue
            live.append(h)
            req = (h.body.winfo_reqwidth(), h.body.winfo_reqheight())
            if req != h._req:
                h._req = req
                h._queue()
        except tk.TclError:
            continue
    _HOSTS[:] = live
    try:
        root.after(_POLL_MS, lambda: _poll(root))
    except tk.TclError:
        pass


class ScrollHost(ttk.Frame):
    """A frame whose contents go in `.body` and which grows a scrollbar only when it needs one."""

    def __init__(self, parent, padding=0, hscroll=True, background=None, fit_width=False, **kw):
        """`fit_width` makes the host ASK its parent for the content's natural width.

        A tab must not do that — the notebook would then demand a window as wide as its widest
        tab. A fixed side column must: the pane has to open at the width the panels were designed
        for, or wrapping it just trades a clipped bottom for a clipped right edge.
        """
        super().__init__(parent, **kw)
        if background is None:
            try:
                background = ttk.Style().lookup("TFrame", "background") or None
            except Exception:
                background = None

        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, takefocus=0,
                                **({"background": background} if background else {}))
        self.vbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.hbar = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vbar.set, xscrollcommand=self.hbar.set)

        self.canvas._uiscroll_owner = self               # see _dispatch
        self.body = ttk.Frame(self.canvas, padding=padding)
        self._win = self.canvas.create_window(0, 0, window=self.body, anchor="nw")

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self._hscroll = bool(hscroll)
        self._fit_width = bool(fit_width)
        self._vis = [False, False]                       # (vbar shown, hbar shown)
        self._last = (-1, -1)                            # last window-item size we set
        self._req = (-1, -1)                             # last requested size the poller saw
        self._pending = False
        self._chain = 0                                  # re-checks since the last settled state

        self.body.bind("<Configure>", self._queue)
        self.canvas.bind("<Configure>", self._queue)
        _HOSTS.append(self)
        _ensure_dispatcher(self)

    # ── measurement ──────────────────────────────────────────────────────────
    def _queue(self, _event=None):
        """Coalesce the storm of <Configure> events a relayout produces into one recount."""
        _ensure_poll(self)
        if not self._pending:
            self._pending = True
            self.after_idle(self._sync)

    def resync(self):
        """Re-measure now. For a caller that has just finished putting content in."""
        self._req = (-1, -1)
        self._chain = 0
        self._queue()

    def _sync(self):
        self._pending = False
        if not self.winfo_exists():
            return
        try:
            need_w, need_h = self.body.winfo_reqwidth(), self.body.winfo_reqheight()
            view_w, view_h = self.canvas.winfo_width(), self.canvas.winfo_height()
        except tk.TclError:
            return
        if view_w <= 1 or view_h <= 1:                    # not mapped yet
            return

        if self._fit_width:
            want = min(need_w, work_area(self)[0] // 2)
            if self.canvas.winfo_reqwidth() != want:
                self.canvas.configure(width=want)

        was = tuple(self._vis)
        over_v = need_h > view_h + _PAD
        over_h = self._hscroll and need_w > view_w + _PAD
        # Each bar steals space from the other axis and can bring the other one into overflow.
        if over_v and self._hscroll and need_w > view_w - self.vbar.winfo_reqwidth() + _PAD:
            over_h = True
        if over_h and need_h > view_h - self.hbar.winfo_reqheight() + _PAD:
            over_v = True
        self._show(over_v, over_h)

        # THE POINT: stretch to the viewport while it fits, so `expand=True` children keep
        # filling exactly as they did unwrapped; only once it does not fit does the body fall
        # back to its own requested size and become something to scroll.
        w = need_w if over_h else max(need_w, self.canvas.winfo_width())
        h = need_h if over_v else max(need_h, self.canvas.winfo_height())
        moved = (w, h) != self._last
        if moved:
            self._last = (w, h)
            self.canvas.itemconfigure(self._win, width=w, height=h)
            self.canvas.configure(scrollregion=(0, 0, w, h))

        # Settling can take two passes: a horizontal bar costs the viewport 15 px of HEIGHT, which
        # is sometimes exactly what tips the content into needing a vertical one. Left to the
        # 250 ms poll that reads as a scrollbar arriving late, so a pass that changed anything
        # re-checks immediately. Bounded, so a layout that genuinely cannot settle flaps four
        # times and stops rather than spinning the event loop forever.
        if moved or (over_v, over_h) != was:
            self._chain += 1
            if self._chain <= 4:
                self._queue()
        else:
            self._chain = 0

    def _show(self, v, h):
        if v != self._vis[0]:
            if v:
                self.vbar.grid(row=0, column=1, sticky="ns")
            else:
                self.vbar.grid_forget()
            self._vis[0] = v
        if h != self._vis[1]:
            if h:
                self.hbar.grid(row=1, column=0, sticky="ew")
            else:
                self.hbar.grid_forget()
            self._vis[1] = h

    @property
    def overflowing(self):
        """True while some of the content is off-screen. -> bool."""
        return bool(self._vis[0] or self._vis[1])

    def scroll_by(self, dy):
        """Wheel notch, called by the module dispatcher. -> "break" if it was consumed."""
        if not self._vis[0]:
            return None
        try:
            self.canvas.yview_scroll(-dy * 2, "units")
        except tk.TclError:
            return None
        return "break"


def scrollable(parent, **kw):
    """`ScrollHost(parent)` — kept as a verb so call sites read as what they are doing."""
    return ScrollHost(parent, **kw)
