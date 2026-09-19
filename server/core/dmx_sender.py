"""Open DMX sender (headless, no Tk)."""

from __future__ import annotations

import threading
import time

import serial
import serial.tools.list_ports

DMX_CHANNELS = 512
OPEN_DMX_BAUD = 250_000
TARGET_FPS = 40


def list_com_ports() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in serial.tools.list_ports.comports():
        desc = p.description or ""
        out.append((p.device, f"{p.device} — {desc}"))
    return sorted(out, key=lambda x: x[0])


class OpenDMXSender(threading.Thread):
    def __init__(self, ser: serial.Serial) -> None:
        super().__init__(daemon=True)
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._universe = bytearray(DMX_CHANNELS)
        self._ser: serial.Serial = ser

    @staticmethod
    def try_open(port: str) -> serial.Serial:
        return serial.Serial(
            port=port,
            baudrate=OPEN_DMX_BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=0,
            write_timeout=None,
            rtscts=False,
            dsrdtr=False,
        )

    def stop(self) -> None:
        self._halt.set()

    def set_universe_snapshot(self, data: bytes) -> None:
        if len(data) != DMX_CHANNELS:
            raise ValueError("universe length must be 512")
        with self._lock:
            self._universe[:] = data

    def set_channels(self, base_addr: int, values: list[int] | tuple[int, ...]) -> None:
        base = max(1, min(DMX_CHANNELS - len(values) + 1, int(base_addr)))
        with self._lock:
            for i, v in enumerate(values):
                self._universe[base - 1 + i] = max(0, min(255, int(v)))

    def _send_frame(self) -> None:
        if not self._ser.is_open:
            return
        with self._lock:
            payload = bytes(self._universe)
        packet = bytes([0]) + payload
        try:
            self._ser.reset_output_buffer()
            self._ser.send_break(duration=0.002)
            time.sleep(0.000050)
            self._ser.write(packet)
            self._ser.flush()
        except serial.SerialException:
            pass

    def run(self) -> None:
        frame_gap = max(1.0 / TARGET_FPS - 0.003, 0.001)
        while not self._halt.is_set():
            t0 = time.perf_counter()
            self._send_frame()
            dt = time.perf_counter() - t0
            time.sleep(max(frame_gap - dt, 0))
        try:
            if self._ser.is_open:
                self._ser.close()
        except serial.SerialException:
            pass
