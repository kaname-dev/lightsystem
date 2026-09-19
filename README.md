# Light System 統合アプリ（Web UI）

Bluetooth LED と DMX レーザを **ブラウザ** から操作します。ハードウェア制御は Python サーバ側で行います。

## 起動

```bat
run.bat
```

または:

```bat
run_web.bat
```

```bat
python -m server
```

ブラウザで [http://127.0.0.1:8787/](http://127.0.0.1:8787/) が開きます。

## 使い方

1. **タイムライン**タブで音楽を開き、Space で再生／停止
2. ポン出しタイルを押している間だけ LED／レーザーを発火（記録 ON でキュー化）
3. **プロジェクト保存**で曲＋キューをセット保存
4. **DMX** タブで COM 接続・モーション
5. **BLE LED** タブでスキャン／接続／色送信
6. **AI・接続** で共有マイク解析を開始（サーバ PC のマイク）

## 補足

- BLE／マイク／COM は **サーバを動かしている PC** 上のデバイスを使います
- 旧 Tk 統合 GUI は `python unified\main.py` で起動できます（非推奨）
- 個別起動（従来）: `BluetoothLED\run.bat` / `DMX\start_laser_dmx.bat`
