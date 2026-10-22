@echo off
REM SPDX-License-Identifier: MIT
REM Launcher for MountFile. The first source run installs required components.
REM Usage: run.bat [disk_image.img] or run.bat --diag
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" goto install
"%PYTHON%" -c "from PyQt6 import QtCore; import pytsk3" >nul 2>&1
if errorlevel 1 goto install
goto launch
:install
echo First run: installing MountFile dependencies. Windows may request administrator approval.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-windows.ps1"
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="3010" (
    echo Restart Windows yourself, then run run.bat again.
    pause
    exit /b 3010
)
if not "%RESULT%"=="0" (
    echo Installation failed. See the error above. Exit code: %RESULT%
    pause
    exit /b %RESULT%
)
:launch
"%PYTHON%" "%~dp0main.py" %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo.
    echo MountFile exited with an error. Exit code: %RESULT%
    echo Try: run.bat --diag
)
pause
exit /b %RESULT%
