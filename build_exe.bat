@echo off
REM SPDX-License-Identifier: MIT
REM Build a standalone MountFile.exe on Windows 10/11 with Python 3.10+.
REM Usage: build_exe.bat --no-pause (for CI/noninteractive execution)
setlocal
cd /d "%~dp0"
set "NOPAUSE="
if /i "%~1"=="--no-pause" set "NOPAUSE=1"
set "EXTRA=--hidden-import mountfile_dfir_tools"
set "ICONARG="
python -c "from PyQt6 import QtCore, QtGui, QtWidgets; import mountfile"
if errorlevel 1 goto failed
python -c "import pytsk3" >nul 2>&1
if not errorlevel 1 (
    set "EXTRA=%EXTRA% --collect-submodules pytsk3"
) else (
    echo Note: pytsk3 is absent; native readers remain available, legacy fallback is not bundled.
)
if exist "mountfile\data\icon.ico" set "ICONARG=--icon mountfile\data\icon.ico"
python -c "import PyInstaller" >nul 2>&1
if not errorlevel 1 goto build
python -m pip install "pyinstaller>=6.22.3"
if errorlevel 1 goto failed
:build
python -m PyInstaller --noconfirm --clean --onefile --windowed --name MountFile ^
    --specpath build\spec %EXTRA% %ICONARG% --collect-data mountfile main.py
if errorlevel 1 goto failed
if not exist "dist\MountFile.exe" goto failed
for %%F in ("dist\MountFile.exe") do if %%~zF==0 goto failed
echo Done: dist\MountFile.exe
if not defined NOPAUSE pause
exit /b 0
:failed
echo Build failed; an old file in dist does not mean this build succeeded. >&2
if not defined NOPAUSE pause
exit /b 1
