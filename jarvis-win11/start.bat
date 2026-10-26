@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Сначала запустите install.bat
  pause
  exit /b 1
)
start "JARVIS" ".venv\Scripts\pythonw.exe" "%~dp0jarvis_app.py"
