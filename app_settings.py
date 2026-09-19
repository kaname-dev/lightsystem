#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""アプリ共通設定（前回 COM / BLE アドレス・起動時自動接続など）。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path(__file__).resolve().parent / "app_settings.json"

_DEFAULTS: dict[str, Any] = {
    "dmx_com_port": "",
    "dmx_base_addr": 1,
    "dmx_auto_connect": False,
    "ble_address": "C6:70:45:84:1B:D8",
    "ble_protocol": "triones",
    "ble_auto_connect": False,
    # 共有 AI の音声入力（前回選択）
    "audio_input_label": "",
    "audio_input_id": -1,
}

_lock = threading.Lock()


def default_settings() -> dict[str, Any]:
    return dict(_DEFAULTS)


def load_settings() -> dict[str, Any]:
    data = default_settings()
    path = SETTINGS_PATH
    if not path.is_file():
        return data
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return data
    if not isinstance(raw, dict):
        return data
    for key, default in _DEFAULTS.items():
        if key not in raw:
            continue
        val = raw[key]
        if isinstance(default, bool):
            data[key] = bool(val)
        elif isinstance(default, int):
            try:
                data[key] = int(val)
            except (TypeError, ValueError):
                pass
        else:
            data[key] = str(val) if val is not None else ""
    return data


def save_settings(data: dict[str, Any]) -> None:
    merged = default_settings()
    for key in _DEFAULTS:
        if key in data:
            merged[key] = data[key]
    with _lock:
        try:
            SETTINGS_PATH.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass


def update_settings(**kwargs: Any) -> dict[str, Any]:
    data = load_settings()
    for key, val in kwargs.items():
        if key in _DEFAULTS:
            data[key] = val
    save_settings(data)
    return data
