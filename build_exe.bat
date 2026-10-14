@echo off
REM SPDX-License-Identifier: MIT
REM Copyright (c) 2026 Alexkkkkk
REM Licensed under the MIT License - see the LICENSE file for details.
REM Build a standalone MountFile.exe with PyInstaller.
REM Works on Windows 7 (Python 3.8 + PyQt5) and Windows 10/11 (Python 3.10+ + PyQt6).
REM
REM Usage:
REM   build_exe.bat          (uses the active Python environment)
REM   py -3.8 build_exe.bat  (Windows 7: force Python 3.8)

python -m PyInstaller --onefile --windowed --name MountFile ^
    --collect-submodules pytsk3 ^
    main.py

echo.
if exist dist\MountFile.exe (
    echo Done: dist\MountFile.exe
) else (
    echo Build failed, see messages above.
)
pause
