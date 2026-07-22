@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt -q
cd app
python main.py
pause
