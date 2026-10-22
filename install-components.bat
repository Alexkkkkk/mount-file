@echo off
REM SPDX-License-Identifier: MIT
REM No Python required. Does not mount, format or modify user images.
setlocal
cd /d "%~dp0"
set "SCRIPT=%~dp0install-windows-components.ps1"
if not exist "%SCRIPT%" set "SCRIPT=%~dp0scripts\install-windows-components.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="3010" (
    echo Restart Windows yourself, then run install-components.bat again to finish setup.
) else if not "%RESULT%"=="0" (
    echo Installation failed. Read the error above. Exit code: %RESULT%
) else (
    echo Components verified. Restart MountFile and open the image again.
    echo For NTFS/exFAT/FAT12/16 writes through ImDisk, run MountFile as Administrator.
)
pause
exit /b %RESULT%
