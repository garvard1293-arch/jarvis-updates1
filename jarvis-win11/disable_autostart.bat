@echo off
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Join-Path ([Environment]::GetFolderPath('Startup')) 'JARVIS.lnk'; if (Test-Path $p) { Remove-Item $p -Force; Write-Host 'Автозапуск JARVIS отключен.' } else { Write-Host 'Ярлык JARVIS не найден.' }"
pause
