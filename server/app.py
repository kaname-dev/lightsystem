"""FastAPI entry: REST + WebSocket + static Web UI."""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.core.light_controller import PROJECT_DIR, UPLOAD_DIR, get_controller

WEB_DIR = ROOT / "web"

app = FastAPI(title="Light System Web")
ctrl = get_controller()


class ChannelsBody(BaseModel):
    channels: list[int]


class ConnectDmxBody(BaseModel):
    port: str
    base_addr: int = 1


class MotionBody(BaseModel):
    index: int | None = None
    speed: float | None = None


class BleConnectBody(BaseModel):
    address: str


class BleColorBody(BaseModel):
    r: int
    g: int
    b: int
    brightness: float = 1.0


class AiBody(BaseModel):
    sensitivity: float = 1.4
    mode: str | None = None


class AiDeviceBody(BaseModel):
    device_id: int | None = None
    label: str = ""


class AiModeBody(BaseModel):
    mode: str


class AutoConnectBody(BaseModel):
    enabled: bool
    address: str | None = None
    port: str | None = None


class BlePowerBody(BaseModel):
    on: bool = True


class BleModeBody(BaseModel):
    mode_id: int
    speed: int = 50


class Esp32ConfigBody(BaseModel):
    host: str
    universe: int = 0
    pixel_count: int = 720
    auto_connect: bool | None = None


class Esp32EnableBody(BaseModel):
    enabled: bool


class Esp32PatternBody(BaseModel):
    pattern_id: str
    speed: float = 1.0
    brightness: float | None = None
    r: int | None = None
    g: int | None = None
    b: int | None = None
    r2: int | None = None
    g2: int | None = None
    b2: int | None = None
    segment_indices: list[int] | None = None
    ranges: list[list[float]] | None = None
    others_off: bool = True


class Esp32SegmentCountBody(BaseModel):
    count: int = 8


class Esp32SegmentBody(BaseModel):
    index: int
    r: int | None = None
    g: int | None = None
    b: int | None = None
    on: bool | None = None
    brightness: float | None = None


class Esp32SegmentsFillBody(BaseModel):
    r: int = 255
    g: int = 80
    b: int = 40
    on: bool = True
    brightness: float | None = None


class Esp32SelectionBody(BaseModel):
    indices: list[int] = []


class Esp32PaintBody(BaseModel):
    indices: list[int] = []
    ranges: list[list[float]] | None = None
    spans: list[list[int]] | None = None
    segment_count: int | None = None
    r: int = 255
    g: int = 80
    b: int = 40
    on: bool = True
    brightness: float | None = None
    others_off: bool = False


class Esp32GroupBody(BaseModel):
    name: str = "グループ"
    indices: list[int] = []
    spans: list[list[int]] | None = None
    group_id: str | None = None
    segment_count: int | None = None
    pixel_count: int | None = None


class Esp32GroupIdBody(BaseModel):
    group_id: str


class HeightBody(BaseModel):
    lo_pct: float
    hi_pct: float


class BoolBody(BaseModel):
    enabled: bool = True


class IntBody(BaseModel):
    value: int


class IndexBody(BaseModel):
    index: int


class TileBody(BaseModel):
    index: int | None = None
    tile: dict[str, Any]


class CaptureTileBody(BaseModel):
    title: str | None = None
    apply_laser: bool = True
    apply_led: bool = False


class HoldBody(BaseModel):
    source: str
    tile_index: int | None = None
    tile: dict[str, Any] | None = None


class SeekBody(BaseModel):
    t: float


class VolumeBody(BaseModel):
    volume: float


class ZoomBody(BaseModel):
    zoom: float
    view_start: float | None = None


class FlagsBody(BaseModel):
    record: bool | None = None
    autoplay: bool | None = None


class DeleteCuesBody(BaseModel):
    cue_ids: list[str] = Field(default_factory=list)


class ProjectNameBody(BaseModel):
    name: str | None = None


class ProjectPathBody(BaseModel):
    path: str


def _ok_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    return ctrl.snapshot()


@app.get("/api/dmx/ports")
def api_dmx_ports() -> list[dict[str, str]]:
    return ctrl.list_ports()


@app.post("/api/dmx/connect")
def api_dmx_connect(body: ConnectDmxBody) -> dict[str, Any]:
    _ok_call(ctrl.dmx_connect, body.port, body.base_addr)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/dmx/disconnect")
def api_dmx_disconnect() -> dict[str, Any]:
    ctrl.dmx_disconnect()
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/dmx/auto-connect")
def api_dmx_auto_connect(body: AutoConnectBody) -> dict[str, Any]:
    if body.port:
        ctrl.dmx_port = body.port.strip()
        try:
            from app_settings import update_settings

            update_settings(dmx_com_port=ctrl.dmx_port)
        except Exception:
            pass
    ctrl.set_dmx_auto_connect(body.enabled)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/dmx/channels")
def api_dmx_channels(body: ChannelsBody) -> dict[str, Any]:
    ctrl.set_channels(body.channels, push=True)
    return {"ok": True}


@app.post("/api/dmx/motion/start")
def api_dmx_motion_start(body: MotionBody) -> dict[str, Any]:
    if body.speed is not None:
        ctrl.motion_speed = float(body.speed)
    ctrl.start_motion(body.index)
    return {"ok": True}


@app.post("/api/dmx/motion/stop")
def api_dmx_motion_stop() -> dict[str, Any]:
    ctrl.stop_motion()
    return {"ok": True}


@app.post("/api/dmx/dot-base")
def api_dmx_dot_base(body: BoolBody) -> dict[str, Any]:
    ctrl.set_apply_dot_base(body.enabled)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/dmx/height")
def api_dmx_height(body: HeightBody) -> dict[str, Any]:
    ctrl.set_height_range(body.lo_pct, body.hi_pct)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/dmx/ch9")
def api_dmx_ch9(body: IntBody) -> dict[str, Any]:
    ctrl.set_club_dot_ch9(body.value)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/dmx/dot-position")
def api_dmx_dot_position(body: IndexBody) -> dict[str, Any]:
    _ok_call(ctrl.apply_dot_position, body.index)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/ble/scan")
def api_ble_scan() -> dict[str, Any]:
    devices = _ok_call(ctrl.ble_scan, 8.0)
    return {"devices": devices}


@app.post("/api/ble/connect")
def api_ble_connect(body: BleConnectBody) -> dict[str, Any]:
    _ok_call(ctrl.ble_connect, body.address)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/ble/disconnect")
def api_ble_disconnect() -> dict[str, Any]:
    ctrl.ble_disconnect()
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/ble/auto-connect")
def api_ble_auto_connect(body: AutoConnectBody) -> dict[str, Any]:
    if body.address:
        ctrl.ble_address = body.address.strip()
    ctrl.set_ble_auto_connect(body.enabled)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/ble/power")
def api_ble_power(body: BlePowerBody) -> dict[str, Any]:
    _ok_call(ctrl.ble_power, body.on)
    return {"ok": True}


@app.post("/api/ble/mode")
def api_ble_mode(body: BleModeBody) -> dict[str, Any]:
    _ok_call(ctrl.ble_mode, body.mode_id, body.speed)
    return {"ok": True}


@app.post("/api/ble/color")
def api_ble_color(body: BleColorBody) -> dict[str, Any]:
    _ok_call(ctrl.ble_color, body.r, body.g, body.b, body.brightness)
    return {"ok": True}


@app.post("/api/esp32/config")
def api_esp32_config(body: Esp32ConfigBody) -> dict[str, Any]:
    _ok_call(
        ctrl.esp32_configure,
        body.host,
        universe=body.universe,
        pixel_count=body.pixel_count,
        auto_connect=body.auto_connect,
    )
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/esp32/enable")
def api_esp32_enable(body: Esp32EnableBody) -> dict[str, Any]:
    _ok_call(ctrl.esp32_enable, body.enabled)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/esp32/color")
def api_esp32_color(body: BleColorBody) -> dict[str, Any]:
    _ok_call(ctrl.esp32_color, body.r, body.g, body.b, body.brightness)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/esp32/probe")
def api_esp32_probe() -> dict[str, Any]:
    result = _ok_call(ctrl.esp32_probe)
    return {**result, "state": ctrl.snapshot()}


@app.post("/api/esp32/blackout")
def api_esp32_blackout() -> dict[str, Any]:
    _ok_call(ctrl.esp32_pattern_stop)
    _ok_call(ctrl.esp32_color, 0, 0, 0, 1.0)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/esp32/pattern")
def api_esp32_pattern(body: Esp32PatternBody) -> dict[str, Any]:
    _ok_call(
        ctrl.esp32_pattern_start,
        body.pattern_id,
        speed=body.speed,
        brightness=body.brightness,
        r=body.r,
        g=body.g,
        b=body.b,
        r2=body.r2,
        g2=body.g2,
        b2=body.b2,
        segment_indices=body.segment_indices,
        ranges=body.ranges,
        others_off=body.others_off,
    )
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/esp32/pattern/stop")
def api_esp32_pattern_stop() -> dict[str, Any]:
    _ok_call(ctrl.esp32_pattern_stop)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/esp32/segments/count")
def api_esp32_segments_count(body: Esp32SegmentCountBody) -> dict[str, Any]:
    _ok_call(ctrl.esp32_set_segment_count, body.count)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/esp32/segment")
def api_esp32_segment(body: Esp32SegmentBody) -> dict[str, Any]:
    _ok_call(
        ctrl.esp32_set_segment,
        body.index,
        r=body.r,
        g=body.g,
        b=body.b,
        on=body.on,
        brightness=body.brightness,
    )
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/esp32/segments/fill")
def api_esp32_segments_fill(body: Esp32SegmentsFillBody) -> dict[str, Any]:
    _ok_call(
        ctrl.esp32_segments_apply_all,
        r=body.r,
        g=body.g,
        b=body.b,
        on=body.on,
        brightness=body.brightness,
    )
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/esp32/selection")
def api_esp32_selection(body: Esp32SelectionBody) -> dict[str, Any]:
    _ok_call(ctrl.esp32_set_selection, body.indices)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/esp32/paint")
def api_esp32_paint(body: Esp32PaintBody) -> dict[str, Any]:
    if body.spans:
        _ok_call(
            ctrl.esp32_paint_pixel_spans,
            body.spans,
            r=body.r,
            g=body.g,
            b=body.b,
            on=body.on,
            brightness=body.brightness,
            others_off=body.others_off,
        )
    elif body.ranges:
        _ok_call(
            ctrl.esp32_paint_ranges,
            body.ranges,
            r=body.r,
            g=body.g,
            b=body.b,
            on=body.on,
            brightness=body.brightness,
            others_off=body.others_off,
        )
    else:
        _ok_call(
            ctrl.esp32_paint_indices,
            body.indices,
            r=body.r,
            g=body.g,
            b=body.b,
            on=body.on,
            brightness=body.brightness,
            others_off=body.others_off,
            segment_count=body.segment_count,
        )
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/esp32/groups")
def api_esp32_groups_upsert(body: Esp32GroupBody) -> dict[str, Any]:
    group = _ok_call(
        ctrl.esp32_group_upsert,
        name=body.name,
        indices=body.indices,
        spans=body.spans,
        group_id=body.group_id,
        segment_count=body.segment_count,
        pixel_count=body.pixel_count,
    )
    return {"ok": True, "group": group, "state": ctrl.snapshot()}


@app.post("/api/esp32/groups/delete")
def api_esp32_groups_delete(body: Esp32GroupIdBody) -> dict[str, Any]:
    _ok_call(ctrl.esp32_group_delete, body.group_id)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/ai/start")
def api_ai_start(body: AiBody) -> dict[str, Any]:
    _ok_call(ctrl.ai_start, body.sensitivity, body.mode)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/ai/stop")
def api_ai_stop() -> dict[str, Any]:
    ctrl.ai_stop()
    return {"ok": True, "state": ctrl.snapshot()}


@app.get("/api/ai/devices")
def api_ai_devices() -> dict[str, Any]:
    devices = ctrl.refresh_audio_devices()
    return {"devices": devices, "state": ctrl.snapshot()}


@app.post("/api/ai/device")
def api_ai_device(body: AiDeviceBody) -> dict[str, Any]:
    ctrl.set_ai_device(body.device_id, body.label)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/ai/mode")
def api_ai_mode(body: AiModeBody) -> dict[str, Any]:
    ctrl.set_ai_mode(body.mode)
    return {"ok": True, "state": ctrl.snapshot()}


@app.get("/api/tiles")
def api_tiles() -> dict[str, Any]:
    ctrl.reload_tiles()
    return {"tiles": ctrl.tiles}


@app.post("/api/tiles/save")
def api_tiles_save(body: TileBody) -> dict[str, Any]:
    idx = _ok_call(ctrl.upsert_tile, body.index, body.tile)
    return {"ok": True, "index": idx, "tiles": ctrl.tiles, "state": ctrl.snapshot()}


@app.post("/api/tiles/delete")
def api_tiles_delete(body: IndexBody) -> dict[str, Any]:
    _ok_call(ctrl.delete_tile, body.index)
    return {"ok": True, "tiles": ctrl.tiles, "state": ctrl.snapshot()}


@app.post("/api/tiles/capture")
def api_tiles_capture(body: CaptureTileBody) -> dict[str, Any]:
    idx = _ok_call(
        ctrl.capture_tile,
        body.title,
        apply_laser=body.apply_laser,
        apply_led=body.apply_led,
    )
    return {"ok": True, "index": idx, "tiles": ctrl.tiles, "state": ctrl.snapshot()}


@app.post("/api/tiles/led-pack")
def api_tiles_led_pack() -> dict[str, Any]:
    added = _ok_call(ctrl.install_led_fx_pack)
    return {"ok": True, "added": added, "tiles": ctrl.tiles, "state": ctrl.snapshot()}


@app.post("/api/tiles/hold")
def api_tiles_hold(body: HoldBody) -> dict[str, Any]:
    tile = body.tile
    if tile is None and body.tile_index is not None:
        tile = ctrl._tile_by_index(body.tile_index)
    if tile is None:
        return {"ok": False, "error": "tile not found"}
    ctrl.hold_add(body.source, dict(tile), record=True)
    return {"ok": True}


@app.post("/api/tiles/release")
def api_tiles_release(body: HoldBody) -> dict[str, Any]:
    ctrl.hold_remove(body.source)
    return {"ok": True}


@app.post("/api/timeline/upload")
async def api_timeline_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / (file.filename or "audio.bin")
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        info = ctrl.load_audio_file(dest)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, **info, "state": ctrl.snapshot()}


@app.post("/api/timeline/play")
def api_timeline_play() -> dict[str, Any]:
    _ok_call(ctrl.start_playback)
    return {"ok": True}


@app.post("/api/timeline/stop")
def api_timeline_stop() -> dict[str, Any]:
    ctrl.stop_playback()
    return {"ok": True}


@app.post("/api/timeline/toggle")
def api_timeline_toggle() -> dict[str, Any]:
    _ok_call(ctrl.toggle_playback)
    return {"ok": True, "playing": ctrl.playing}


@app.post("/api/timeline/seek")
def api_timeline_seek(body: SeekBody) -> dict[str, Any]:
    ctrl.seek(body.t)
    return {"ok": True}


@app.post("/api/timeline/volume")
def api_timeline_volume(body: VolumeBody) -> dict[str, Any]:
    ctrl.set_volume(body.volume)
    return {"ok": True}


@app.post("/api/timeline/zoom")
def api_timeline_zoom(body: ZoomBody) -> dict[str, Any]:
    ctrl.set_zoom(body.zoom, body.view_start)
    return {"ok": True}


@app.post("/api/timeline/flags")
def api_timeline_flags(body: FlagsBody) -> dict[str, Any]:
    if body.record is not None:
        ctrl.punch_record = bool(body.record)
    if body.autoplay is not None:
        ctrl.punch_autoplay = bool(body.autoplay)
    return {"ok": True}


@app.post("/api/cues/delete")
def api_cues_delete(body: DeleteCuesBody) -> dict[str, Any]:
    ctrl.delete_cues(body.cue_ids)
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/cues/clear")
def api_cues_clear() -> dict[str, Any]:
    ctrl.clear_cues()
    return {"ok": True, "state": ctrl.snapshot()}


@app.post("/api/cues/undo")
def api_cues_undo() -> dict[str, Any]:
    ok = ctrl.undo_cues()
    return {"ok": ok, "state": ctrl.snapshot()}


@app.post("/api/cues/redo")
def api_cues_redo() -> dict[str, Any]:
    ok = ctrl.redo_cues()
    return {"ok": ok, "state": ctrl.snapshot()}


@app.post("/api/project/save")
def api_project_save(body: ProjectNameBody) -> dict[str, Any]:
    paths = _ok_call(ctrl.save_project, body.name)
    return {"ok": True, **paths}


@app.get("/api/project/list")
def api_project_list() -> dict[str, Any]:
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(PROJECT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {"projects": [{"name": p.name, "path": str(p)} for p in files]}


@app.post("/api/project/load")
def api_project_load(body: ProjectPathBody) -> dict[str, Any]:
    _ok_call(ctrl.load_project, body.path)
    return {"ok": True, "state": ctrl.snapshot()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
    loop = asyncio.get_running_loop()

    def on_event(msg: dict[str, Any]) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, msg)
        except Exception:
            pass

    ctrl.add_listener(on_event)
    await ws.send_json({"event": "hello", "state": ctrl.snapshot()})
    try:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=0.4)
                # tick は軽量化（フル state を毎回送らない）
                if msg.get("event") == "tick":
                    await ws.send_json(
                        {
                            "event": "tick",
                            "position": msg.get("position"),
                            "state": {
                                "timeline": {
                                    "playing": True,
                                    "position": msg.get("position"),
                                    "duration": ctrl.audio_duration,
                                }
                            },
                        }
                    )
                else:
                    await ws.send_json(msg)
            except asyncio.TimeoutError:
                if ctrl.playing:
                    await ws.send_json(
                        {
                            "event": "tick",
                            "position": ctrl._elapsed(),
                            "state": {
                                "timeline": {
                                    "playing": True,
                                    "position": ctrl._elapsed(),
                                    "duration": ctrl.audio_duration,
                                }
                            },
                        }
                    )
    except WebSocketDisconnect:
        pass
    finally:
        ctrl.remove_listener(on_event)


@app.get("/")
def index() -> FileResponse:
    resp = FileResponse(WEB_DIR / "index.html")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    response = await call_next(request)
    path = request.url.path or ""
    if path.startswith("/static/") or path == "/":
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def main() -> None:
    import uvicorn
    import webbrowser

    host = "127.0.0.1"
    port = 8787
    url = f"http://{host}:{port}/"
    print(f"Light System Web UI: {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
