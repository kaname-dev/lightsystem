@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python が PATH にありません。
  pause
  exit /b 1
)

echo 依存パッケージを確認しています...
python -m pip install -r "%~dp0requirements.txt" -q
if errorlevel 1 (
  echo pip install に失敗しました。
  pause
  exit /b 1
)

echo Web UI を起動します...
echo ブラウザで http://127.0.0.1:8787/ を開きます
python -m server
set ERR=%ERRORLEVEL%
if not "%ERR%"=="0" (
  echo.
  echo 終了コード: %ERR%
  pause
)
exit /b %ERR%
