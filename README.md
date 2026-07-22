# Light System 統合アプリ

Bluetooth LED と DMX レーザを一つの GUI で操作します。音楽解析は **BluetoothLED** 側のエンジンを共有します。

## 起動

```bat
run.bat
```

または:

```bat
python unified\main.py
```

## 使い方

1. 上部の「共有 AI リアクティブ」でマイク入力・感度・モードを設定して開始
2. **Bluetooth LED** タブで BLE 接続（スキャン or アドレス）
3. **DMX レーザー** タブで COM ポート接続・モーション操作

AI 開始中は同じ解析結果で LED の色／明るさとレーザーのモーション・CH9・速度が連動します。

## 個別起動（従来どおり）

- `BluetoothLED\run.bat`
- `DMX\start_laser_dmx.bat`（単体でも BluetoothLED 解析を使用）
