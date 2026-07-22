"""Full-spectrum HSV color palette for tkinter (no external deps)."""

from __future__ import annotations

import colorsys
import tkinter as tk
from typing import Callable


class ColorPalette(tk.Frame):
    """Single map with all hues: white → pure color → black (drag updates continuously)."""

    def __init__(
        self,
        master,
        *,
        initial: tuple[int, int, int] = (255, 80, 40),
        on_change: Callable[[int, int, int], None] | None = None,
        width: int = 240,
        height: int = 240,
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
        self.map_w = width
        self.map_h = height
        self._hue = 0.0
        self._sat = 1.0
        self._val = 1.0
        self._dragging = False

        self.configure(bg="#242830")
        self.canvas = tk.Canvas(
            self,
            width=width,
            height=height,
            highlightthickness=0,
            bd=0,
            cursor="crosshair",
            bg="#000000",
        )
        self.canvas.pack()

        self._image: tk.PhotoImage | None = None
        self._build_map()
        r, g, b = initial
        self.set_rgb(r, g, b, notify=False)
        self._bind()

    def _bind(self) -> None:
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)

    @staticmethod
    def _hsv_at(nx: float, ny: float) -> tuple[float, float, float]:
        """Map normalized (x,y) → HSV. Top=white, mid=pure hue, bottom=black."""
        hue = nx
        if ny < 0.5:
            sat = ny * 2.0
            val = 1.0
        else:
            sat = 1.0
            val = 1.0 - (ny - 0.5) * 2.0
        return hue, sat, val

    @staticmethod
    def _xy_from_hsv(h: float, s: float, v: float) -> tuple[float, float]:
        """Inverse of _hsv_at (approximate for white/gray where hue is undefined)."""
        nx = h
        if v >= 1.0 - 1e-6:
            ny = s * 0.5
        elif s >= 1.0 - 1e-6:
            ny = 0.5 + (1.0 - v) * 0.5
        else:
            # Desaturated dark: prefer value axis with scaled sat
            if s < 1e-6:
                ny = 1.0 - v * 0.5  # near bottom-ish gray line
            else:
                # Blend: treat as on the white→color→black column for this hue
                if v >= s:
                    # closer to white→color region
                    ny = s * 0.5
                else:
                    ny = 0.5 + (1.0 - v) * 0.5
        return nx, max(0.0, min(1.0, ny))

    def _build_map(self) -> None:
        w, h = self.map_w, self.map_h
        img = tk.PhotoImage(width=w, height=h)
        step = 2
        for y in range(0, h, step):
            ny = y / max(h - 1, 1)
            for x in range(0, w, step):
                nx = x / max(w - 1, 1)
                hue, sat, val = self._hsv_at(nx, ny)
                r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
                color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
                img.put(color, to=(x, y, min(x + step, w), min(y + step, h)))
        self._image = img
        self.canvas.delete("map")
        self.canvas.create_image(0, 0, anchor="nw", image=img, tags="map")
        self._draw_marker()

    def _draw_marker(self) -> None:
        nx, ny = self._xy_from_hsv(self._hue, self._sat, self._val)
        sx = nx * (self.map_w - 1)
        sy = ny * (self.map_h - 1)
        self.canvas.delete("marker")
        r = 7
        self.canvas.create_oval(
            sx - r, sy - r, sx + r, sy + r, outline="#ffffff", width=2, tags="marker"
        )
        self.canvas.create_oval(
            sx - r + 1,
            sy - r + 1,
            sx + r - 1,
            sy + r - 1,
            outline="#000000",
            width=1,
            tags="marker",
        )

    def get_rgb(self) -> tuple[int, int, int]:
        r, g, b = colorsys.hsv_to_rgb(self._hue, self._sat, self._val)
        return int(r * 255), int(g * 255), int(b * 255)

    def set_rgb(self, r: int, g: int, b: int, *, notify: bool = True) -> None:
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        self._hue, self._sat, self._val = h, s, v
        self._draw_marker()
        if notify:
            self._emit()

    def _emit(self) -> None:
        if self.on_change:
            r, g, b = self.get_rgb()
            self.on_change(r, g, b)

    def _from_event(self, event) -> None:
        x = max(0, min(self.map_w - 1, event.x))
        y = max(0, min(self.map_h - 1, event.y))
        nx = x / max(self.map_w - 1, 1)
        ny = y / max(self.map_h - 1, 1)
        self._hue, self._sat, self._val = self._hsv_at(nx, ny)
        self._draw_marker()
        self._emit()

    def _press(self, event) -> None:
        self._dragging = True
        self._from_event(event)

    def _drag(self, event) -> None:
        if self._dragging:
            self._from_event(event)

    def _release(self, _event) -> None:
        self._dragging = False
