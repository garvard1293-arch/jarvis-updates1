@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Сначала запустите install.bat
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$shell=New-Object -ComObject WScript.Shell; $startup=[Environment]::GetFolderPath('Startup'); $shortcut=$shell.CreateShortcut((Join-Path $startup 'JARVIS.lnk')); $shortcut.TargetPath='%CD%\.venv\Scripts\pythonw.exe'; $shortcut.Arguments='"%CD%\jarvis_app.py"'; $shortcut.WorkingDirectory='%CD%'; $shortcut.Description='JARVIS voice assistant'; $shortcut.Save()"
echo Ярлык добавлен в автозагрузку пользователя Windows.
pause
