@echo off
REM SPDX-License-Identifier: MIT
REM Copyright (c) 2026 Alexkkkkk
REM Licensed under the MIT License - see the LICENSE file for details.
REM Build a standalone MountFile.exe with PyInstaller.
REM Supported on Windows 10/11 with Python 3.10+ and PyQt6.
REM
REM Usage:
REM   build_exe.bat                (uses the active Python environment)
REM   build_exe.bat --no-pause     (CI: do not wait for a key press)

setlocal
set NOPAUSE=
if /i "%~1"=="--no-pause" set NOPAUSE=1

REM pytsk3 is optional: without it the exe still browses and writes FAT32
REM images with the built-in engine, so the build must not fail on it.
set EXTRA=
python -c "import pytsk3" >nul 2>&1 && set EXTRA=--collect-submodules pytsk3
if not defined EXTRA echo Note: pytsk3 not installed - building with the built-in FAT32 engine only.

REM App icon from the bundled artwork: icon.ico (16..256) for Windows.
set ICON=mountfile\data\icon.ico
if exist "%ICON%" (set ICONARG=--icon %ICON%) else (echo Note: %ICON% not found - default icon used.)

python -m pip install --quiet --upgrade pyinstaller
python -m PyInstaller --onefile --windowed --name MountFile %EXTRA% %ICONARG% ^
    --collect-data mountfile main.py

echo.
if exist dist\MountFile.exe (
    echo Done: dist\MountFile.exe
) else (
    echo Build failed, see messages above.
    if not defined NOPAUSE pause
    exit /b 1
)
if not defined NOPAUSE pause
