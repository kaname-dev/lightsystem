"""Fine-grained HSV color picker for tkinter (no external deps).

SV square + hue strip + RGB spinboxes for precise LED color selection.
"""

from __future__ import annotations

import colorsys
import tkinter as tk
from typing import Callable


class ColorPalette(tk.Frame):
    """Hue strip + saturation/value square + RGB entries."""

    def __init__(
        self,
        master,
        *,
        initial: tuple[int, int, int] = (255, 80, 40),
        on_change: Callable[[int, int, int], None] | None = None,
        width: int = 220,
        height: int = 180,
        **kwargs,
    ) -> None:
        # Backward-compatible aliases used by older callers
        if "square_size" in kwargs:
            size = int(kwargs.pop("square_size"))
            width = size
            height = size
        kwargs.pop("hue_height", None)

        super().__init__(master, **kwargs)
        self.on_change = on_change
        self.sq_w = max(80, int(width))
        self.sq_h = max(80, int(height))
        self.hue_w = 22
        self._hue = 0.0
        self._sat = 1.0
        self._val = 1.0
        self._dragging_sv = False
        self._dragging_hue = False
        self._syncing_spins = False
        self._sv_image: tk.PhotoImage | None = None
        self._hue_image: tk.PhotoImage | None = None

        self.configure(bg="#242830")

        maps = tk.Frame(self, bg="#242830")
        maps.pack(anchor="w")

        self.sv_canvas = tk.Canvas(
            maps,
            width=self.sq_w,
            height=self.sq_h,
            highlightthickness=0,
            bd=0,
            cursor="crosshair",
            bg="#000000",
        )
        self.sv_canvas.pack(side="left")

        self.hue_canvas = tk.Canvas(
            maps,
            width=self.hue_w,
            height=self.sq_h,
            highlightthickness=0,
            bd=0,
            cursor="sb_v_double_arrow",
            bg="#000000",
        )
        self.hue_canvas.pack(side="left", padx=(8, 0))

        rgb_row = tk.Frame(self, bg="#242830")
        rgb_row.pack(fill="x", pady=(8, 0))

        self._r_var = tk.IntVar(value=initial[0])
        self._g_var = tk.IntVar(value=initial[1])
        self._b_var = tk.IntVar(value=initial[2])
        self._spins: list[tk.Spinbox] = []
        for label, var in (("R", self._r_var), ("G", self._g_var), ("B", self._b_var)):
            tk.Label(rgb_row, text=label, fg="#c8ccd2", bg="#242830", width=2).pack(side="left")
            sp = tk.Spinbox(
                rgb_row,
                from_=0,
                to=255,
                width=4,
                textvariable=var,
                command=self._on_spin,
                justify="right",
            )
            sp.pack(side="left", padx=(0, 8))
            sp.bind("<Return>", lambda _e: self._on_spin())
            sp.bind("<FocusOut>", lambda _e: self._on_spin())
            self._spins.append(sp)

        self._build_hue_strip()
        self._build_sv_map()
        r, g, b = initial
        self.set_rgb(r, g, b, notify=False)
        self._bind()

    def _bind(self) -> None:
        self.sv_canvas.bind("<Button-1>", self._sv_press)
        self.sv_canvas.bind("<B1-Motion>", self._sv_drag)
        self.sv_canvas.bind("<ButtonRelease-1>", self._sv_release)
        self.hue_canvas.bind("<Button-1>", self._hue_press)
        self.hue_canvas.bind("<B1-Motion>", self._hue_drag)
        self.hue_canvas.bind("<ButtonRelease-1>", self._hue_release)

    def _build_hue_strip(self) -> None:
        w, h = self.hue_w, self.sq_h
        img = tk.PhotoImage(width=w, height=h)
        for y in range(h):
            hue = y / max(h - 1, 1)
            r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
            color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
            img.put(color, to=(0, y, w, y + 1))
        self._hue_image = img
        self.hue_canvas.delete("map")
        self.hue_canvas.create_image(0, 0, anchor="nw", image=img, tags="map")
        self._draw_hue_marker()

    def _build_sv_map(self) -> None:
        w, h = self.sq_w, self.sq_h
        img = tk.PhotoImage(width=w, height=h)
        hue = self._hue
        # step=1 だと遅いので 1px 精度に近い 1〜2
        step = 1 if max(w, h) <= 160 else 2
        for y in range(0, h, step):
            val = 1.0 - y / max(h - 1, 1)
            for x in range(0, w, step):
                sat = x / max(w - 1, 1)
                r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
                color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
                img.put(color, to=(x, y, min(x + step, w), min(y + step, h)))
        self._sv_image = img
        self.sv_canvas.delete("map")
        self.sv_canvas.create_image(0, 0, anchor="nw", image=img, tags="map")
        self._draw_sv_marker()

    def _draw_sv_marker(self) -> None:
        sx = self._sat * (self.sq_w - 1)
        sy = (1.0 - self._val) * (self.sq_h - 1)
        self.sv_canvas.delete("marker")
        r = 6
        self.sv_canvas.create_oval(
            sx - r, sy - r, sx + r, sy + r, outline="#ffffff", width=2, tags="marker"
        )
        self.sv_canvas.create_oval(
            sx - r + 1,
            sy - r + 1,
            sx + r - 1,
            sy + r - 1,
            outline="#000000",
            width=1,
            tags="marker",
        )

    def _draw_hue_marker(self) -> None:
        hy = self._hue * (self.sq_h - 1)
        self.hue_canvas.delete("marker")
        self.hue_canvas.create_polygon(
            2,
            hy,
            self.hue_w - 2,
            hy - 5,
            self.hue_w - 2,
            hy + 5,
            fill="#ffffff",
            outline="#000000",
            tags="marker",
        )

    def get_rgb(self) -> tuple[int, int, int]:
        r, g, b = colorsys.hsv_to_rgb(self._hue, self._sat, self._val)
        return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))

    def set_rgb(self, r: int, g: int, b: int, *, notify: bool = True) -> None:
        r = max(0, min(255, int(r)))
        g = max(0, min(255, int(g)))
        b = max(0, min(255, int(b)))
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        hue_changed = abs(h - self._hue) > 1e-6 and (s > 1e-4 or self._sat > 1e-4)
        self._hue, self._sat, self._val = h, s, v
        if hue_changed:
            self._build_sv_map()
        else:
            self._draw_sv_marker()
        self._draw_hue_marker()
        self._sync_spins(r, g, b)
        if notify:
            self._emit()

    def _sync_spins(self, r: int, g: int, b: int) -> None:
        self._syncing_spins = True
        try:
            self._r_var.set(r)
            self._g_var.set(g)
            self._b_var.set(b)
        finally:
            self._syncing_spins = False

    def _on_spin(self) -> None:
        if self._syncing_spins:
            return
        try:
            r = max(0, min(255, int(self._r_var.get())))
            g = max(0, min(255, int(self._g_var.get())))
            b = max(0, min(255, int(self._b_var.get())))
        except (tk.TclError, ValueError, TypeError):
            return
        self.set_rgb(r, g, b, notify=True)

    def _emit(self) -> None:
        r, g, b = self.get_rgb()
        self._sync_spins(r, g, b)
        if self.on_change:
            self.on_change(r, g, b)

    def _sv_from_event(self, event) -> None:
        x = max(0, min(self.sq_w - 1, event.x))
        y = max(0, min(self.sq_h - 1, event.y))
        self._sat = x / max(self.sq_w - 1, 1)
        self._val = 1.0 - y / max(self.sq_h - 1, 1)
        self._draw_sv_marker()
        self._emit()

    def _hue_from_event(self, event) -> None:
        y = max(0, min(self.sq_h - 1, event.y))
        self._hue = y / max(self.sq_h - 1, 1)
        self._draw_hue_marker()
        self._build_sv_map()
        self._emit()

    def _sv_press(self, event) -> None:
        self._dragging_sv = True
        self._sv_from_event(event)

    def _sv_drag(self, event) -> None:
        if self._dragging_sv:
            self._sv_from_event(event)

    def _sv_release(self, _event) -> None:
        self._dragging_sv = False

    def _hue_press(self, event) -> None:
        self._dragging_hue = True
        self._hue_from_event(event)

    def _hue_drag(self, event) -> None:
        if self._dragging_hue:
            self._hue_from_event(event)

    def _hue_release(self, _event) -> None:
        self._dragging_hue = False
