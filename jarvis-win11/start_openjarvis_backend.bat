@echo off
setlocal
set "APP_DIR=%~dp0"
set "UV_EXE=%APP_DIR%.venv\Scripts\uv.exe"
set "SOURCE_DIR=%LOCALAPPDATA%\JarvisAssistant\OpenJarvisSource"
set "OPENJARVIS_HOME=%APPDATA%\JarvisAssistant\OpenJarvisHome"

if not exist "%UV_EXE%" (
    echo uv не найден. Сначала запустите setup_openjarvis_backend.bat.
    pause
    exit /b 1
)
if not exist "%SOURCE_DIR%\pyproject.toml" (
    echo OpenJarvis не установлен. Сначала запустите setup_openjarvis_backend.bat.
    pause
    exit /b 1
)

cd /d "%SOURCE_DIR%"
echo Запускаю локальный OpenJarvis API только на этом компьютере: 127.0.0.1:8765
echo Инструменты ограничены staging-папкой; shell-команды отключены.
"%UV_EXE%" run --extra server --python 3.11 jarvis serve --host 127.0.0.1 --port 8765 --model qwen3:8b --agent orchestrator
if errorlevel 1 (
    echo OpenJarvis остановился с ошибкой. Проверьте Ollama и модель qwen3:8b.
    pause
)
