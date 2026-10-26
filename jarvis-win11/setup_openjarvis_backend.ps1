$ErrorActionPreference = 'Stop'

$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $appDir '.venv\Scripts\python.exe'
$uvExe = Join-Path $appDir '.venv\Scripts\uv.exe'
$sourceDir = Join-Path $env:LOCALAPPDATA 'JarvisAssistant\OpenJarvisSource'
$homeDir = Join-Path $env:APPDATA 'JarvisAssistant\OpenJarvisHome'
$stagingDir = Join-Path $env:APPDATA 'JarvisAssistant\AgentStaging'
$commit = '40092da25efb2288f2650d0380d5224d3c46b9a1'

if (-not (Test-Path $venvPython)) {
    throw "Не найдено окружение JARVIS: $venvPython. Сначала выполните install.bat."
}

if (-not (Test-Path $uvExe)) {
    Write-Host 'Устанавливаю uv в виртуальное окружение JARVIS…'
    & $venvPython -m pip install 'uv>=0.6,<1'
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить uv.' }
}

if (-not (Test-Path (Join-Path $sourceDir 'pyproject.toml'))) {
    if (Test-Path $sourceDir) {
        throw "Папка $sourceDir уже существует, но это не установленный OpenJarvis. Ничего не удалено; выберите другую папку или переименуйте её вручную."
    }
    Write-Host "Загружаю OpenJarvis commit $commit (без запуска установщика из интернета)…"
    $tempRoot = Join-Path $env:TEMP ('jarvis-openjarvis-' + [guid]::NewGuid().ToString('N'))
    $zipPath = Join-Path $tempRoot 'source.zip'
    $extractPath = Join-Path $tempRoot 'extract'
    New-Item -ItemType Directory -Force -Path $extractPath | Out-Null
    try {
        Invoke-WebRequest -Uri "https://codeload.github.com/open-jarvis/OpenJarvis/zip/$commit" -OutFile $zipPath
        Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
        $repoFolder = Get-ChildItem -Path $extractPath -Directory | Select-Object -First 1
        if (-not $repoFolder) { throw 'Архив OpenJarvis не содержит папку исходников.' }
        $parent = Split-Path -Parent $sourceDir
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        Move-Item -Path $repoFolder.FullName -Destination $sourceDir
    }
    finally {
        if (Test-Path $tempRoot) { Remove-Item -Recurse -Force $tempRoot }
    }
}

if (-not (Test-Path (Join-Path $sourceDir 'LICENSE'))) {
    throw 'В исходниках не найден файл LICENSE; установка остановлена.'
}
$projectMetadata = Get-Content -Raw (Join-Path $sourceDir 'pyproject.toml')
$licenseText = Get-Content -Raw (Join-Path $sourceDir 'LICENSE')
if ($projectMetadata -notmatch '(?m)^name\s*=\s*"OpenJarvis"') {
    throw 'Имя загруженного Python-проекта не совпадает с OpenJarvis; установка остановлена.'
}
if ($licenseText -notmatch 'Apache License') {
    throw 'В исходниках не подтверждена Apache License; установка остановлена.'
}

New-Item -ItemType Directory -Force -Path $homeDir, $stagingDir | Out-Null
$allowedPath = [System.IO.Path]::GetFullPath($stagingDir).Replace('\', '/').Replace('"', '\"')
$configPath = Join-Path $homeDir 'config.toml'
if (Test-Path $configPath) {
    $existingConfig = Get-Content -Raw $configPath
    if ($existingConfig -notmatch '# JARVIS managed OpenJarvis config') {
        throw "Файл $configPath уже существует и не помечен как конфигурация JARVIS. Он не перезаписан; сохраните его и выберите отдельную папку вручную."
    }
}
$config = @"
# JARVIS managed OpenJarvis config
[engine]
default = "ollama"

[intelligence]
default_model = "qwen3:8b"

[server]
host = "127.0.0.1"
port = 8765
model = "qwen3:8b"

[agent]
default_agent = "orchestrator"
max_turns = 8

[tools]
enabled = ["file_read", "file_write", "think"]

[security]
allowed_dirs = ["$allowedPath"]

[telemetry]
enabled = false
"@
[System.IO.File]::WriteAllText($configPath, $config, [System.Text.UTF8Encoding]::new($false))

Write-Host 'Устанавливаю зависимости API-сервера OpenJarvis. Это может занять несколько минут…'
$env:OPENJARVIS_HOME = $homeDir
Push-Location $sourceDir
try {
    & $uvExe sync --extra server --python 3.11
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости OpenJarvis.' }
}
finally {
    Pop-Location
}

Write-Host ''
Write-Host 'Готово. Безопасная конфигурация разрешает только file_read, file_write и think.'
Write-Host 'Shell-команды, исполнение Python и MCP-инструменты не включены.'
Write-Host 'Рабочая область OpenJarvis ограничена отдельной папкой AgentStaging.'
Write-Host 'Перед запуском установите Ollama и выполните: ollama pull qwen3:8b'
Write-Host 'Затем запустите start_openjarvis_backend.bat.'
