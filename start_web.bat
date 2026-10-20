@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Chua co .venv. Xem README.md de cai dat Python va dependencies.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" web_app.py
if errorlevel 1 pause
