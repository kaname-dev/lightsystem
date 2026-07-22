@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python が PATH にありません。
  pause
  exit /b 1
)

python -c "import serial" >nul 2>&1
if errorlevel 1 (
  echo pyserial をインストールしています...
  python -m pip install -r "%~dp0requirements.txt" -q
  if errorlevel 1 (
    echo pip install に失敗しました。
    pause
    exit /b 1
  )
)

python "%~dp0laser_dmx_app.py"
if errorlevel 1 pause
exit /b %errorlevel%
