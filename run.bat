@echo off
REM Launcher for Mount File (keeps the console open to show errors).
REM Usage:   run.bat [disk_image.img]
REM          run.bat --diag     environment diagnostics

cd /d "%~dp0"
python main.py %*
if errorlevel 1 (
    echo.
    echo Mount File exited with an error (code %errorlevel%).
    echo Try:  run.bat --diag
)
pause
