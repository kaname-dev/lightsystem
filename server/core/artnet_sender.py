"""Minimal Art-Net ArtDmx UDP sender for PixelCtl (ESP32-S3) and similar nodes."""

from __future__ import annotations

import socket
import threading
from typing import Iterable


ARTNET_PORT = 6454
_OP_DMX = 0x5000


class ArtNetSender:
    """Send solid-color (or raw channel) ArtDmx packets to a PixelCtl host."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self.host = ""
        self.port = ARTNET_PORT
        self.universe = 0  # Art-Net universe number (device artnetOffset=1 → Uni 0 = internal 1)
        self.pixel_count = 170
        self.enabled = False
        self._seq = 0
        self.last_rgb: tuple[int, int, int] = (0, 0, 0)
        self.last_error = ""

    def configure(
        self,
        *,
        host: str,
        universe: int = 0,
        pixel_count: int = 170,
        port: int = ARTNET_PORT,
        enabled: bool | None = None,
    ) -> None:
        with self._lock:
            self.host = (host or "").strip()
            self.universe = max(0, min(32767, int(universe)))
            self.pixel_count = max(1, min(170, int(pixel_count)))
            self.port = int(port) or ARTNET_PORT
            if enabled is not None:
                self.enabled = bool(enabled)
            if self._sock is None:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                except OSError:
                    pass
                self._sock = sock

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
            self.enabled = False

    def send_channels(self, channels: Iterable[int], *, universe: int | None = None) -> None:
        data = bytes(max(0, min(255, int(v))) for v in channels)
        if len(data) > 512:
            data = data[:512]
        if len(data) < 2:
            data = data + b"\x00" * (2 - len(data))
        # Art-Net length must be even
        if len(data) % 2:
            data = data + b"\x00"
        uni = self.universe if universe is None else max(0, min(32767, int(universe)))
        with self._lock:
            if not self.enabled or not self.host or self._sock is None:
                return
            self._seq = (self._seq + 1) & 0xFF
            seq = self._seq
            sock = self._sock
            host = self.host
            port = self.port
        packet = bytearray(18 + len(data))
        packet[0:7] = b"Art-Net"
        packet[7] = 0
        packet[8] = _OP_DMX & 0xFF
        packet[9] = (_OP_DMX >> 8) & 0xFF
        packet[10] = 0  # ProtVer Hi
        packet[11] = 14  # ProtVer Lo
        packet[12] = seq
        packet[13] = 0  # Physical
        packet[14] = uni & 0xFF
        packet[15] = (uni >> 8) & 0xFF
        packet[16] = (len(data) >> 8) & 0xFF
        packet[17] = len(data) & 0xFF
        packet[18:] = data
        try:
            sock.sendto(packet, (host, port))
            self.last_error = ""
        except OSError as e:
            self.last_error = str(e)

    def send_rgb_fill(
        self,
        r: int,
        g: int,
        b: int,
        *,
        brightness: float = 1.0,
        pixel_count: int | None = None,
        universe: int | None = None,
    ) -> None:
        """Fill N RGB pixels with one color (PixelCtl normal mapping)."""
        bri = max(0.0, min(1.0, float(brightness)))
        rr = max(0, min(255, int(round(int(r) * bri))))
        gg = max(0, min(255, int(round(int(g) * bri))))
        bb = max(0, min(255, int(round(int(b) * bri))))
        n = self.pixel_count if pixel_count is None else max(1, min(170, int(pixel_count)))
        ch: list[int] = []
        for _ in range(n):
            ch.extend((rr, gg, bb))
        self.last_rgb = (rr, gg, bb)
        self.send_channels(ch, universe=universe)

    def blackout(self) -> None:
        self.send_rgb_fill(0, 0, 0, brightness=1.0)
