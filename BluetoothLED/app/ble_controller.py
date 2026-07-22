"""BLE scan / connect / write for Happy Lighting compatible LED controllers."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice

from protocol import (
    detect_protocol_from_name,
    elk_brightness,
    elk_color,
    elk_effect,
    elk_power,
    is_likely_led_device,
    normalize_ble_address,
    triones_color,
    triones_mode,
    triones_power,
    triones_status_query,
    triones_white,
    ui_speed_to_triones,
)


@dataclass
class DiscoveredDevice:
    name: str
    address: str
    rssi: int
    device: BLEDevice

    @property
    def label(self) -> str:
        return f"{self.name}  ({self.address})  {self.rssi} dBm"


@dataclass
class ScanResult:
    devices: list[DiscoveredDevice]
    total_seen: int


def _uuid_key(uuid: str) -> str:
    return uuid.lower().split("-")[0][-4:]


class LedController:
    def __init__(self) -> None:
        self.client: BleakClient | None = None
        self.write_char: BleakGATTCharacteristic | None = None
        self.protocol: str = "triones"
        self.device_name: str = ""
        self.address: str = ""
        self._on_disconnect: Callable[[], None] | None = None

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    async def scan(self, timeout: float = 12.0, led_only: bool = True) -> ScanResult:
        """Active scan; keeps strongest advertisement per address."""
        seen: dict[str, DiscoveredDevice] = {}

        def _on_detect(device: BLEDevice, adv) -> None:
            name = device.name or adv.local_name or ""
            rssi = adv.rssi if adv.rssi is not None else -999
            prev = seen.get(device.address)
            if prev is None or rssi > prev.rssi or (not prev.name.startswith("Triones") and name):
                seen[device.address] = DiscoveredDevice(
                    name=name or "(無名)",
                    address=device.address,
                    rssi=rssi,
                    device=device,
                )

        scanner = BleakScanner(detection_callback=_on_detect)
        await scanner.start()
        try:
            await asyncio.sleep(timeout)
        finally:
            await scanner.stop()

        total = len(seen)
        devices = list(seen.values())
        if led_only:
            devices = [d for d in devices if is_likely_led_device(d.name, d.address)]
        devices.sort(key=lambda d: (0 if "triones" in d.name.lower() else 1, -d.rssi))
        return ScanResult(devices=devices, total_seen=total)

    async def find_by_address(self, address: str, timeout: float = 12.0) -> BLEDevice:
        target = normalize_ble_address(address)
        target_compact = target.replace(":", "").upper()

        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        for addr, (device, adv) in found.items():
            compact = addr.replace(":", "").upper()
            name = device.name or adv.local_name or ""
            if compact == target_compact:
                return device
            # Name embeds MAC: Triones:C67045841BD8
            if target_compact in name.replace(":", "").upper():
                return device

        # Direct connect attempt (Windows can resolve known peripherals)
        return await BleakScanner.find_device_by_address(target, timeout=timeout)  # type: ignore[return-value]

    async def connect(
        self,
        device: BLEDevice,
        on_disconnect: Callable[[], None] | None = None,
        preferred_name: str | None = None,
    ) -> None:
        await self.disconnect()
        self._on_disconnect = on_disconnect
        self.device_name = preferred_name or device.name or ""
        self.address = device.address
        self.protocol = detect_protocol_from_name(self.device_name) or "triones"

        client = BleakClient(device, disconnected_callback=self._handle_disconnect)
        await client.connect()

        write_char = self._pick_write_characteristic(client)
        if write_char is None:
            await client.disconnect()
            raise RuntimeError("書き込み用GATTキャラクタリスティックが見つかりませんでした")

        self.client = client
        self.write_char = write_char

        key = _uuid_key(write_char.uuid)
        if key in {"fff3", "fff5", "fff7", "fff9"} and self.protocol == "triones":
            if self.device_name.upper().startswith(("ELK", "MELK", "LEDBLE", "LED-")):
                self.protocol = "elk"

    async def connect_address(
        self,
        address: str,
        on_disconnect: Callable[[], None] | None = None,
        timeout: float = 12.0,
    ) -> None:
        device = await self.find_by_address(address, timeout=timeout)
        if device is None:
            raise RuntimeError(
                f"{address} が見つかりません。\n"
                "・スマホの Happy Lighting を切断する\n"
                "・LEDコントローラの電源を入れ直す\n"
                "・PCのBluetoothがONか確認する"
            )
        await self.connect(device, on_disconnect=on_disconnect, preferred_name=device.name)

    def _handle_disconnect(self, _client: BleakClient) -> None:
        self.client = None
        self.write_char = None
        if self._on_disconnect:
            self._on_disconnect()

    def _pick_write_characteristic(self, client: BleakClient) -> BleakGATTCharacteristic | None:
        order = ["ffd9", "ffe1", "fff3", "fff5", "fff7", "fff9"]
        preferred = set(order)
        candidates: list[tuple[int, BleakGATTCharacteristic]] = []

        for service in client.services:
            for char in service.characteristics:
                props = set(char.properties)
                if "write" not in props and "write-without-response" not in props:
                    continue
                key = _uuid_key(char.uuid)
                if key in preferred:
                    candidates.append((order.index(key), char))

        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1]

        for service in client.services:
            for char in service.characteristics:
                props = set(char.properties)
                if "write-without-response" in props or "write" in props:
                    uuid = char.uuid.lower()
                    if uuid.startswith("0000ff") or "vendor" in (service.description or "").lower():
                        return char
        return None

    async def disconnect(self) -> None:
        if self.client:
            try:
                if self.client.is_connected:
                    await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self.write_char = None

    async def _write(self, payload: bytes) -> None:
        if not self.client or not self.write_char or not self.client.is_connected:
            raise RuntimeError("デバイスに接続されていません")
        without_response = "write-without-response" in self.write_char.properties
        await self.client.write_gatt_char(self.write_char, payload, response=not without_response)

    async def set_power(self, on: bool) -> None:
        if self.protocol == "elk":
            await self._write(elk_power(on))
        else:
            await self._write(triones_power(on))

    async def set_color(self, r: int, g: int, b: int, brightness: float = 1.0) -> None:
        brightness = max(0.0, min(1.0, float(brightness)))
        r = int(r * brightness)
        g = int(g * brightness)
        b = int(b * brightness)
        if self.protocol == "elk":
            await self._write(elk_color(r, g, b))
            await self._write(elk_brightness(int(brightness * 100)))
        else:
            await self._write(triones_color(r, g, b))

    async def set_white(self, intensity: int) -> None:
        if self.protocol == "elk":
            await self._write(elk_color(intensity, intensity, intensity))
        else:
            await self._write(triones_white(intensity))

    async def set_mode(self, mode: int, ui_speed: int) -> None:
        if self.protocol == "elk":
            await self._write(elk_effect(mode, ui_speed))
        else:
            await self._write(triones_mode(mode, ui_speed_to_triones(ui_speed)))

    def set_protocol(self, name: str) -> None:
        if name in {"triones", "elk"}:
            self.protocol = name

    async def query_status(self) -> None:
        if self.protocol == "triones":
            await self._write(triones_status_query())


class AsyncWorker:
    """Run asyncio coroutines from a tkinter thread via a dedicated loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()

    def start_in_thread(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def wait_ready(self, timeout: float = 5.0) -> None:
        self._ready.wait(timeout=timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)
