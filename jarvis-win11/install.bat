@echo off
setlocal
cd /d "%~dp0"
echo =============================================
echo   JARVIS - установка для Windows 11
echo =============================================
echo.
py -3.11 --version >nul 2>&1
if errorlevel 1 (
  echo Не найден Python 3.11. Установите Python 3.11 x64 с python.org и включите Add Python to PATH.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Создаю виртуальное окружение...
  py -3.11 -m venv .venv
  if errorlevel 1 goto :error
)
echo Устанавливаю библиотеки приложения...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error
if not exist "models\vosk-model-small-ru-0.22\am\final.mdl" (
  echo Загружаю русскую офлайн-модель распознавания речи Vosk, около 45 МБ...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $zip=Join-Path $env:TEMP 'vosk-model-small-ru-0.22.zip'; Invoke-WebRequest 'https://alphacephei.com/kaldi/models/vosk-model-small-ru-0.22.zip' -OutFile $zip; Expand-Archive -LiteralPath $zip -DestinationPath '%CD%\models' -Force; Remove-Item $zip -Force"
  if errorlevel 1 goto :error
)
echo.
echo Установка завершена. Запустите start.bat.
echo Для локального ИИ установите Ollama и выполните: ollama pull qwen3:8b
pause
exit /b 0
:error
echo.
echo Установка не завершилась. Проверьте подключение к интернету и текст ошибки выше.
pause
exit /b 1
