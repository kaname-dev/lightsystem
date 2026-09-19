@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM 既定起動は Web UI（Tk 統合アプリは unified\main.py）
call "%~dp0run_web.bat"
