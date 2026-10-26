@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_openjarvis_backend.ps1"
if errorlevel 1 (
    echo.
    echo Установка OpenJarvis не завершилась. Проверьте сообщение об ошибке выше.
    pause
    exit /b 1
)
echo.
echo OpenJarvis backend установлен. Запустите start_openjarvis_backend.bat.
pause
