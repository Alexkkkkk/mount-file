@echo off
REM SPDX-License-Identifier: MIT
REM Double-click to install Python, a virtual environment and all runtime components.
REM Options use PowerShell names: -DryRun, -Minimal, -NoSys, -SkipExt, -SkipImDisk.
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-windows.ps1" %*
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="3010" (
    echo Restart Windows yourself, then run install.bat again to finish WSL setup.
) else if not "%RESULT%"=="0" (
    echo Installation failed. Read the error above. Exit code: %RESULT%
) else (
    echo Installation verified. Start MountFile using run.bat.
)
pause
exit /b %RESULT%
